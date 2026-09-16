"""Ciclo de compra de punta a punta: OC -> aprobar -> recibir -> validar factura -> pagar.

Cada paso se probó por separado; esta prueba verifica que se encadenen: que lo que
deja un paso sea exactamente lo que el siguiente espera, y que presupuesto,
inventario, CxP y contabilidad terminen cuadrados entre sí.
"""
import datetime as dt
from decimal import Decimal

import pytest

import models
import schemas
from conftest import ANIO, presupuestar
from routers.compras import (RecepcionLinea, RecepcionPayload, aprobar_oc, cerrar_oc,
                             create_oc, recibir_oc)
from routers.contabilidad import registrar_pago, validar_cxp

ENERO = dt.date(ANIO, 1, 15)


def _periodo_de(d: dt.date) -> models.PeriodoContable:
    import calendar
    fin = dt.date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])
    return models.PeriodoContable(anio=d.year, mes=d.month, nombre=f"{d:%b-%Y}".upper(),
                                  estado="abierto", fecha_inicio=dt.date(d.year, d.month, 1), fecha_fin=fin)


@pytest.fixture
def escenario(db, config, proveedor):
    """Plan de cuentas mínimo, reglas de contabilización, período abierto y presupuesto."""
    ctas = {}
    for cod, nom, nat in [("1.1.03", "Inventario", "deudora"), ("2.1.01", "CxP proveedores", "acreedora"),
                          ("1.1.06", "ITBIS crédito", "deudora"), ("1.1.01", "Banco", "deudora"),
                          ("6.1.01", "Insumos agrícolas", "deudora")]:
        c = models.CuentaContable(codigo=cod, nombre=nom, naturaleza=nat, tipo="gasto")
        db.add(c)
        ctas[cod] = c
    db.flush()
    db.add_all([
        models.ReglaContabilizacion(evento="compra", concepto="factura_proveedor", activo=True,
                                    cuenta_debe_id=ctas["6.1.01"].id, cuenta_haber_id=ctas["2.1.01"].id),
        models.ReglaContabilizacion(evento="compra", concepto="itbis_compra", activo=True,
                                    cuenta_debe_id=ctas["1.1.06"].id, cuenta_haber_id=ctas["2.1.01"].id),
        models.ReglaContabilizacion(evento="pago", concepto="pago_proveedor", activo=True,
                                    cuenta_debe_id=ctas["2.1.01"].id, cuenta_haber_id=ctas["1.1.01"].id),
        models.PeriodoContable(anio=ANIO, mes=1, nombre=f"ENE-{ANIO}", estado="abierto",
                               fecha_inicio=dt.date(ANIO, 1, 1), fecha_fin=dt.date(ANIO, 1, 31)),
        # La recepción se fecha con datetime.now(): hace falta el período de hoy.
        _periodo_de(dt.date.today()),
        models.Producto(id_prod="P1", producto="Urea", unidad="kg", costo_promedio=0, stock_actual=0,
                        es_inventariable=True, activo=True, impuesto_compra="itbis_18"),
    ])
    presupuestar(db, ctas["6.1.01"].id, 100_000)
    db.commit()
    return ctas


def _movs(db, tipo):
    return [m for m in db.query(models.MovimientoPresupuestario).all() if m.tipo == tipo]


def test_ciclo_completo(db, user, proveedor, escenario):
    gasto = escenario["6.1.01"]

    # ── 1. Crear: 10 kg a 5.000 con 10% de descuento = 45.000 netos ──
    r = create_oc(schemas.OrdenCompraCreate(
        proveedor_id=proveedor.id, fecha=dt.datetime.combine(ENERO, dt.time()),
        lineas=[schemas.OCLineaCreate(producto_id="P1", cantidad=10, precio_unitario=5_000,
                                      descuento_pct=10)]),
        db=db, current_user=user)
    oc_id = r.oc_id
    oc = db.query(models.OrdenCompra).filter_by(oc_id=oc_id).one()
    assert oc.estado == "Borrador"
    assert float(oc.total_estimado) == 45_000

    # ── 2. Aprobar: compromete 45.000 en la cuenta de gasto ──
    aprobar_oc(oc_id, override=False, db=db, current_user=user)
    db.refresh(oc)
    assert oc.estado == "Aprobada"
    comps = db.query(models.CompromisoPresupuestario).filter_by(origen_id=oc_id).all()
    assert len(comps) == 1 and comps[0].estado == "activo"
    assert float(comps[0].monto) == 45_000
    assert comps[0].cuenta_id == gasto.id
    assert sum(float(m.monto) for m in _movs(db, "COMPROMISO")) == 45_000

    # ── 3. Recibir todo: GR al precio neto, CxP con ITBIS, asiento ──
    linea = db.query(models.OrdenCompraLinea).filter_by(oc_id=oc_id).one()
    rec = recibir_oc(oc_id, RecepcionPayload(num_factura="FAC-777",
                                             lineas=[RecepcionLinea(linea_id=linea.id, cantidad_recibida=10)]),
                     db=db, current_user=user)
    db.refresh(oc)
    assert oc.estado == "Recibida"
    assert float(oc.total_recibido) == 45_000

    prod = db.query(models.Producto).filter_by(id_prod="P1").one()
    assert float(prod.stock_actual) == 10
    assert float(prod.costo_promedio) == 4_500, "entra al precio neto, no al bruto"

    gr = db.query(models.MovimientoInventario).filter_by(oc_referencia=oc_id, tipo_doc="GR").one()
    assert float(gr.costo_unitario) == 4_500 and float(gr.stock_post) == 10

    cxp = db.query(models.CuentaPorPagar).filter_by(oc_id=oc_id).one()
    assert float(cxp.subtotal) == 45_000
    assert float(cxp.itbis) == 8_100                      # 18% de 45.000
    assert float(cxp.retencion_itbis) == 2_430            # 30% del ITBIS, proveedor jurídico formal
    assert float(cxp.total) == 45_000 + 8_100 - 2_430
    assert cxp.estado == "pendiente" and cxp.num_factura_proveedor == "FAC-777"
    assert rec["cxp"] == cxp.numero
    assert cxp.asiento_id is not None, "la recepción debe generar asiento"

    # ── 4. Validar (three-way match): libera compromiso, devenga ──
    v = validar_cxp(cxp.id, db=db, user=user)
    assert v["ok"] is True, v
    db.refresh(comps[0])
    assert comps[0].estado == "ejecutado"
    assert float(comps[0].monto_ejecutado) == 45_000
    assert sum(float(m.monto) for m in _movs(db, "LIBERACION")) == -45_000
    assert sum(float(m.monto) for m in _movs(db, "DEVENGADO")) == 45_000
    neto_comprometido = sum(float(m.monto) for m in db.query(models.MovimientoPresupuestario).all()
                            if m.tipo in ("COMPROMISO", "LIBERACION"))
    assert neto_comprometido == 0, "tras la factura no queda nada comprometido"

    # ── 5. Pagar parcial y luego el resto ──
    p1 = registrar_pago(schemas.PagoCreate(cxp_id=cxp.id, fecha=dt.date(ANIO, 1, 20), monto=20_000),
                        db=db, user=user)
    db.refresh(cxp)
    assert cxp.estado == "parcial" and p1["saldo_restante"] == pytest.approx(float(cxp.total) - 20_000)
    assert p1["asiento"] is not None

    registrar_pago(schemas.PagoCreate(cxp_id=cxp.id, fecha=dt.date(ANIO, 1, 25), monto=float(cxp.saldo_pendiente)),
                   db=db, user=user)
    db.refresh(cxp)
    assert cxp.estado == "pagada" and float(cxp.saldo_pendiente) == 0
    assert sum(float(m.monto) for m in _movs(db, "PAGADO")) == pytest.approx(float(cxp.total))

    # ── 6. Cerrar la OC: no queda remanente que liberar ──
    cerrar_oc(oc_id, db=db, current_user=user)
    db.refresh(oc)
    assert oc.estado == "Cerrada"
    assert sum(float(m.monto) for m in _movs(db, "LIBERACION")) == -45_000, "el cierre no libera dos veces"

    # ── Cuadre final: cada asiento balancea ──
    for a in db.query(models.AsientoContable).all():
        assert float(a.total_debe) == pytest.approx(float(a.total_haber)), f"asiento {a.numero} descuadrado"


def test_recepcion_parcial_deja_compromiso_y_oc_abiertos(db, user, proveedor, escenario):
    r = create_oc(schemas.OrdenCompraCreate(
        proveedor_id=proveedor.id, fecha=dt.datetime.combine(ENERO, dt.time()),
        lineas=[schemas.OCLineaCreate(producto_id="P1", cantidad=10, precio_unitario=1_000)]),
        db=db, current_user=user)
    aprobar_oc(r.oc_id, override=False, db=db, current_user=user)
    linea = db.query(models.OrdenCompraLinea).filter_by(oc_id=r.oc_id).one()

    recibir_oc(r.oc_id, RecepcionPayload(lineas=[RecepcionLinea(linea_id=linea.id, cantidad_recibida=4)]),
               db=db, current_user=user)
    oc = db.query(models.OrdenCompra).filter_by(oc_id=r.oc_id).one()
    assert oc.estado == "Parcial"

    cxp = db.query(models.CuentaPorPagar).filter_by(oc_id=r.oc_id).one()
    validar_cxp(cxp.id, db=db, user=user)

    comp = db.query(models.CompromisoPresupuestario).filter_by(origen_id=r.oc_id).one()
    assert comp.estado == "activo", "quedan 6 unidades por recibir: sigue comprometido"
    assert float(comp.monto_ejecutado) == 4_000
    assert float(comp.monto) - float(comp.monto_ejecutado) == 6_000

    cerrar_oc(r.oc_id, db=db, current_user=user)
    db.refresh(comp)
    assert comp.estado == "cancelado"
    liberado = sum(float(m.monto) for m in _movs(db, "LIBERACION"))
    assert liberado == -10_000, "4.000 por la factura + 6.000 por el cierre"


def test_bloqueo_presupuestario_impide_aprobar(db, user, proveedor, escenario):
    r = create_oc(schemas.OrdenCompraCreate(
        proveedor_id=proveedor.id, fecha=dt.datetime.combine(ENERO, dt.time()),
        lineas=[schemas.OCLineaCreate(producto_id="P1", cantidad=100, precio_unitario=2_000)]),
        db=db, current_user=user)

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        aprobar_oc(r.oc_id, override=False, db=db, current_user=user)
    assert e.value.status_code == 400
    assert e.value.detail["requiere_override"] is True

    oc = db.query(models.OrdenCompra).filter_by(oc_id=r.oc_id).one()
    assert oc.estado == "Borrador", "un bloqueo no debe dejar la OC a medias"
    assert db.query(models.CompromisoPresupuestario).count() == 0

    aprobar_oc(r.oc_id, override=True, db=db, current_user=user)
    db.refresh(oc)
    assert oc.estado == "Aprobada"


def test_recibir_sin_periodo_contable_aborta_sin_dejar_rastro(db, user, proveedor, escenario):
    """Sin período abierto para hoy, la recepción no puede generar asiento: debe fallar
    entera, no subir el stock ni crear la CxP sin contabilidad detrás."""
    from fastapi import HTTPException
    hoy = dt.date.today()
    db.query(models.PeriodoContable).filter_by(anio=hoy.year, mes=hoy.month).delete()
    db.commit()

    r = create_oc(schemas.OrdenCompraCreate(
        proveedor_id=proveedor.id, fecha=dt.datetime.combine(ENERO, dt.time()),
        lineas=[schemas.OCLineaCreate(producto_id="P1", cantidad=10, precio_unitario=1_000)]),
        db=db, current_user=user)
    aprobar_oc(r.oc_id, override=False, db=db, current_user=user)
    linea = db.query(models.OrdenCompraLinea).filter_by(oc_id=r.oc_id).one()

    with pytest.raises(HTTPException) as e:
        recibir_oc(r.oc_id, RecepcionPayload(lineas=[RecepcionLinea(linea_id=linea.id, cantidad_recibida=10)]),
                   db=db, current_user=user)
    assert e.value.status_code == 400
    assert "período contable" in e.value.detail

    db.expire_all()
    oc = db.query(models.OrdenCompra).filter_by(oc_id=r.oc_id).one()
    prod = db.query(models.Producto).filter_by(id_prod="P1").one()
    assert oc.estado == "Aprobada", "la OC no debe quedar como recibida"
    assert float(prod.stock_actual) == 0, "el stock no debe moverse"
    assert db.query(models.CuentaPorPagar).filter_by(oc_id=r.oc_id).count() == 0
    assert db.query(models.MovimientoInventario).filter_by(oc_referencia=r.oc_id).count() == 0
