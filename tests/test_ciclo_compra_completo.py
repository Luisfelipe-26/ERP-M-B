"""Ciclo de compra de punta a punta: OC -> aprobar -> recibir -> facturar -> pagar.

Cada paso se probó por separado; esta prueba verifica que se encadenen: que lo que
deja un paso sea exactamente lo que el siguiente espera, y que presupuesto,
inventario, CxP y contabilidad terminen cuadrados entre sí. Sigue el modelo de
Dynamics 365: la recepción queda contra la cuenta puente y la factura la liquida.
"""
import datetime as dt

import pytest

import models
import schemas
from conftest import ANIO, cuentas_compra, periodo_abierto, presupuestar, saldo_cuenta
from routers.compras import (FacturaDatos, FacturaPayload, RecepcionLinea, RecepcionPayload,
                             aprobar_oc, cerrar_oc, create_oc, recibir_oc, registrar_factura_oc)
from routers.contabilidad import anular_cxp, registrar_pago, validar_cxp

ENERO = dt.date(ANIO, 1, 15)


def _periodo_de(d: dt.date) -> models.PeriodoContable:
    import calendar
    fin = dt.date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])
    return models.PeriodoContable(anio=d.year, mes=d.month, nombre=f"{d:%b-%Y}".upper(),
                                  estado="abierto", fecha_inicio=dt.date(d.year, d.month, 1), fecha_fin=fin)


@pytest.fixture
def escenario(db, config, proveedor):
    """Plan de cuentas, reglas (la compra se presupuesta en 6.1.01), período abierto y presupuesto."""
    ctas = {}
    for cod, nom, nat in [("1.1.01", "Banco", "deudora"), ("6.1.01", "Insumos agrícolas", "deudora")]:
        c = models.CuentaContable(codigo=cod, nombre=nom, naturaleza=nat, tipo="gasto")
        db.add(c)
        ctas[cod] = c
    db.flush()
    ctas.update(cuentas_compra(db, cuenta_compra_id=ctas["6.1.01"].id))
    ctas["1.1.06"] = ctas["1.1.02.03"]           # alias del ITBIS crédito fiscal
    ctas["2.1.01"] = ctas["2.1.01.01"]           # alias de CxP proveedores
    db.add_all([
        models.ReglaContabilizacion(evento="pago", concepto="pago_proveedor", activo=True,
                                    cuenta_debe_id=ctas["2.1.01.01"].id, cuenta_haber_id=ctas["1.1.01"].id),
        models.Producto(id_prod="P1", producto="Urea", unidad="kg", costo_promedio=0, stock_actual=0,
                        es_inventariable=True, activo=True, impuesto_compra="itbis_18"),
    ])
    periodo_abierto(db, ENERO)
    # La recepción se fecha hoy por defecto: hace falta el período de hoy.
    periodo_abierto(db, dt.date.today())
    presupuestar(db, ctas["6.1.01"].id, 100_000)
    db.commit()
    return ctas


def _movs(db, tipo):
    return [m for m in db.query(models.MovimientoPresupuestario).all() if m.tipo == tipo]


def _oc(db, user, proveedor, cantidad, precio, descuento=0):
    r = create_oc(schemas.OrdenCompraCreate(
        proveedor_id=proveedor.id, fecha=dt.datetime.combine(ENERO, dt.time()),
        lineas=[schemas.OCLineaCreate(producto_id="P1", cantidad=cantidad, precio_unitario=precio,
                                      descuento_pct=descuento)]),
        db=db, current_user=user)
    return r.oc_id


def test_ciclo_completo(db, user, proveedor, escenario):
    gasto = escenario["6.1.01"]

    # ── 1. Crear: 10 kg a 5.000 con 10% de descuento = 45.000 netos ──
    oc_id = _oc(db, user, proveedor, 10, 5_000, descuento=10)
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

    # ── 3. Recibir y facturar en un paso: GR al precio neto, factura con NCF, ITBIS y retención ──
    linea = db.query(models.OrdenCompraLinea).filter_by(oc_id=oc_id).one()
    rec = recibir_oc(oc_id, RecepcionPayload(
        lineas=[RecepcionLinea(linea_id=linea.id, cantidad_recibida=10)],
        factura=FacturaDatos(ncf="B0100000777", num_factura="FAC-777")),
        db=db, current_user=user)
    db.refresh(oc)
    assert oc.estado == "Recibida"
    assert float(oc.total_recibido) == 45_000

    prod = db.query(models.Producto).filter_by(id_prod="P1").one()
    assert float(prod.stock_actual) == 10
    assert float(prod.costo_promedio) == 4_500, "entra al precio neto, no al bruto"

    cxp = db.query(models.CuentaPorPagar).filter_by(oc_id=oc_id).one()
    assert float(cxp.subtotal) == 45_000
    assert float(cxp.itbis) == 8_100                      # 18% de 45.000
    assert float(cxp.retencion_itbis) == 2_430            # 30% del ITBIS, proveedor jurídico formal
    assert float(cxp.total) == 45_000 + 8_100 - 2_430
    assert cxp.ncf == "B0100000777" and cxp.tipo_ncf == "B01"
    assert cxp.estado == "pendiente" and cxp.num_factura_proveedor == "FAC-777"
    assert rec["cxp"] == cxp.numero

    # El mayor dice lo mismo que los documentos
    assert saldo_cuenta(db, escenario["2.1.01.01"]) == -float(cxp.total), "mayor de CxP = auxiliar"
    assert saldo_cuenta(db, escenario["1.1.02.03"]) == 8_100, "el ITBIS entra como crédito fiscal"
    assert saldo_cuenta(db, escenario["2.1.02.04"]) == -2_430, "la retención queda por pagar"
    assert saldo_cuenta(db, escenario["2.1.01.04"]) == 0, "la factura liquidó la cuenta puente"

    # ── 4. La factura ya devengó: validar de nuevo no duplica ──
    db.refresh(comps[0])
    assert comps[0].estado == "ejecutado"
    assert float(comps[0].monto_ejecutado) == 45_000
    v = validar_cxp(cxp.id, db=db, user=user)
    assert v["ok"] is True
    assert sum(float(m.monto) for m in _movs(db, "LIBERACION")) == -45_000
    assert sum(float(m.monto) for m in _movs(db, "DEVENGADO")) == 45_000, "validar no devenga dos veces"

    # ── 5. Pagar parcial y luego el resto ──
    p1 = registrar_pago(schemas.PagoCreate(cxp_id=cxp.id, fecha=dt.date(ANIO, 1, 20), monto=20_000),
                        db=db, user=user)
    db.refresh(cxp)
    assert cxp.estado == "parcial" and p1["saldo_restante"] == pytest.approx(float(cxp.total) - 20_000)
    registrar_pago(schemas.PagoCreate(cxp_id=cxp.id, fecha=dt.date(ANIO, 1, 25), monto=float(cxp.saldo_pendiente)),
                   db=db, user=user)
    db.refresh(cxp)
    assert cxp.estado == "pagada" and float(cxp.saldo_pendiente) == 0
    assert sum(float(m.monto) for m in _movs(db, "PAGADO")) == pytest.approx(float(cxp.total))
    assert saldo_cuenta(db, escenario["2.1.01.01"]) == 0, "pagada la factura, el mayor de CxP queda en cero"

    # ── 6. Cerrar la OC: no queda remanente que liberar ──
    cerrar_oc(oc_id, db=db, current_user=user)
    db.refresh(oc)
    assert oc.estado == "Cerrada"
    assert sum(float(m.monto) for m in _movs(db, "LIBERACION")) == -45_000, "el cierre no libera dos veces"

    for a in db.query(models.AsientoContable).all():
        assert float(a.total_debe) == pytest.approx(float(a.total_haber)), f"asiento {a.numero} descuadrado"


def test_recibir_hoy_y_facturar_despues(db, user, proveedor, escenario):
    """La mercancía llega sin factura: queda en la cuenta puente hasta que el proveedor factura."""
    oc_id = _oc(db, user, proveedor, 10, 1_000)
    aprobar_oc(oc_id, override=False, db=db, current_user=user)
    linea = db.query(models.OrdenCompraLinea).filter_by(oc_id=oc_id).one()

    rec = recibir_oc(oc_id, RecepcionPayload(lineas=[RecepcionLinea(linea_id=linea.id, cantidad_recibida=10)]),
                     db=db, current_user=user)
    assert rec["cxp"] is None and rec["por_facturar"] is True
    assert db.query(models.CuentaPorPagar).count() == 0, "sin factura no hay deuda registrada"
    assert saldo_cuenta(db, escenario["2.1.01.04"]) == -10_000
    comp = db.query(models.CompromisoPresupuestario).filter_by(origen_id=oc_id).one()
    assert comp.estado == "activo", "el presupuesto sigue comprometido hasta la factura"

    # La factura trae 1% más por unidad: dentro de tolerancia, la diferencia va al costo
    r = registrar_factura_oc(oc_id, FacturaPayload(ncf="E310000000055", fecha_factura=dt.date.today(),
                                                   lineas=[{"linea_id": linea.id, "cantidad": 10,
                                                            "precio_unitario": 1_010}]),
                             db=db, current_user=user)
    assert r["subtotal"] == 10_100 and r["itbis"] == 1_818
    assert saldo_cuenta(db, escenario["2.1.01.04"]) == 0
    assert saldo_cuenta(db, escenario["2.1.01.01"]) == -r["total"]
    db.refresh(linea)
    assert float(linea.cantidad_facturada) == 10

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:          # ya no queda nada por facturar
        registrar_factura_oc(oc_id, FacturaPayload(ncf="B0100000056"), db=db, current_user=user)
    assert "pendiente de facturar" in e.value.detail


def test_la_factura_no_pasa_mas_de_lo_recibido_ni_un_precio_distinto_ni_un_ncf_repetido(db, user, proveedor, escenario):
    from fastapi import HTTPException
    oc_id = _oc(db, user, proveedor, 10, 1_000)
    aprobar_oc(oc_id, override=False, db=db, current_user=user)
    linea = db.query(models.OrdenCompraLinea).filter_by(oc_id=oc_id).one()
    recibir_oc(oc_id, RecepcionPayload(lineas=[RecepcionLinea(linea_id=linea.id, cantidad_recibida=6)]),
               db=db, current_user=user)

    casos = [
        (FacturaPayload(ncf="B0100000001", lineas=[{"linea_id": linea.id, "cantidad": 8}]), "hay 6 recibidas"),
        (FacturaPayload(ncf="B0100000001", lineas=[{"linea_id": linea.id, "cantidad": 6, "precio_unitario": 1_100}]),
         "más de 2%"),
        (FacturaPayload(ncf="B01-123"), "inválido"),
        (FacturaPayload(ncf="B0400000001"), "no corresponde"),
    ]
    for payload, msg in casos:
        with pytest.raises(HTTPException) as e:
            registrar_factura_oc(oc_id, payload, db=db, current_user=user)
        assert msg in e.value.detail, e.value.detail
        db.rollback()

    registrar_factura_oc(oc_id, FacturaPayload(ncf="B0100000001", lineas=[{"linea_id": linea.id, "cantidad": 3}]),
                         db=db, current_user=user)
    with pytest.raises(HTTPException) as e:
        registrar_factura_oc(oc_id, FacturaPayload(ncf="B0100000001"), db=db, current_user=user)
    assert "ya está registrado" in e.value.detail


def test_anular_una_factura_la_devuelve_a_por_facturar(db, user, proveedor, escenario):
    oc_id = _oc(db, user, proveedor, 10, 1_000)
    aprobar_oc(oc_id, override=False, db=db, current_user=user)
    linea = db.query(models.OrdenCompraLinea).filter_by(oc_id=oc_id).one()
    recibir_oc(oc_id, RecepcionPayload(lineas=[RecepcionLinea(linea_id=linea.id, cantidad_recibida=10)],
                                       factura=FacturaDatos(ncf="B0100000009")),
               db=db, current_user=user)
    cxp = db.query(models.CuentaPorPagar).filter_by(oc_id=oc_id).one()

    anular_cxp(cxp.id, motivo="NCF equivocado", db=db, user=user)

    db.refresh(cxp)
    db.refresh(linea)
    assert cxp.estado == "anulada"
    assert float(linea.cantidad_facturada) == 0
    assert saldo_cuenta(db, escenario["2.1.01.01"]) == 0
    assert saldo_cuenta(db, escenario["1.1.02.03"]) == 0
    assert saldo_cuenta(db, escenario["2.1.01.04"]) == -10_000, "vuelve a estar recibido sin facturar"
    comp = db.query(models.CompromisoPresupuestario).filter_by(origen_id=oc_id).one()
    assert comp.estado == "activo" and float(comp.monto_ejecutado) == 0
    neto = sum(float(m.monto) for m in db.query(models.MovimientoPresupuestario).all())
    assert neto == 10_000, "vuelve a estar comprometido, ya no devengado"

    # Se puede volver a facturar con el NCF correcto
    registrar_factura_oc(oc_id, FacturaPayload(ncf="B0100000010"), db=db, current_user=user)


def test_recepcion_parcial_deja_compromiso_y_oc_abiertos(db, user, proveedor, escenario):
    oc_id = _oc(db, user, proveedor, 10, 1_000)
    aprobar_oc(oc_id, override=False, db=db, current_user=user)
    linea = db.query(models.OrdenCompraLinea).filter_by(oc_id=oc_id).one()

    recibir_oc(oc_id, RecepcionPayload(lineas=[RecepcionLinea(linea_id=linea.id, cantidad_recibida=4)],
                                       factura=FacturaDatos(ncf="B0100000004")),
               db=db, current_user=user)
    oc = db.query(models.OrdenCompra).filter_by(oc_id=oc_id).one()
    assert oc.estado == "Parcial"

    comp = db.query(models.CompromisoPresupuestario).filter_by(origen_id=oc_id).one()
    assert comp.estado == "activo", "quedan 6 unidades por recibir: sigue comprometido"
    assert float(comp.monto_ejecutado) == 4_000
    assert float(comp.monto) - float(comp.monto_ejecutado) == 6_000

    cerrar_oc(oc_id, db=db, current_user=user)
    db.refresh(comp)
    assert comp.estado == "cancelado"
    liberado = sum(float(m.monto) for m in _movs(db, "LIBERACION"))
    assert liberado == -10_000, "4.000 por la factura + 6.000 por el cierre"


def test_bloqueo_presupuestario_impide_aprobar(db, user, proveedor, escenario):
    oc_id = _oc(db, user, proveedor, 100, 2_000)

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        aprobar_oc(oc_id, override=False, db=db, current_user=user)
    assert e.value.status_code == 400
    assert e.value.detail["requiere_override"] is True

    oc = db.query(models.OrdenCompra).filter_by(oc_id=oc_id).one()
    assert oc.estado == "Borrador", "un bloqueo no debe dejar la OC a medias"
    assert db.query(models.CompromisoPresupuestario).count() == 0

    aprobar_oc(oc_id, override=True, db=db, current_user=user)
    db.refresh(oc)
    assert oc.estado == "Aprobada"


def test_recibir_sin_periodo_contable_aborta_sin_dejar_rastro(db, user, proveedor, escenario):
    """Sin período abierto para hoy, la recepción no puede generar asiento: debe fallar
    entera, no subir el stock ni crear la CxP sin contabilidad detrás."""
    from fastapi import HTTPException
    hoy = dt.date.today()
    db.query(models.PeriodoContable).filter_by(anio=hoy.year, mes=hoy.month).delete()
    db.commit()

    oc_id = _oc(db, user, proveedor, 10, 1_000)
    aprobar_oc(oc_id, override=False, db=db, current_user=user)
    linea = db.query(models.OrdenCompraLinea).filter_by(oc_id=oc_id).one()

    with pytest.raises(HTTPException) as e:
        recibir_oc(oc_id, RecepcionPayload(lineas=[RecepcionLinea(linea_id=linea.id, cantidad_recibida=10)],
                                           factura=FacturaDatos(ncf="B0100000001")),
                   db=db, current_user=user)
    assert e.value.status_code == 400
    assert "período contable" in e.value.detail

    db.expire_all()
    oc = db.query(models.OrdenCompra).filter_by(oc_id=oc_id).one()
    prod = db.query(models.Producto).filter_by(id_prod="P1").one()
    assert oc.estado == "Aprobada", "la OC no debe quedar como recibida"
    assert float(prod.stock_actual) == 0, "el stock no debe moverse"
    assert db.query(models.CuentaPorPagar).filter_by(oc_id=oc_id).count() == 0
    assert db.query(models.MovimientoInventario).filter_by(oc_referencia=oc_id).count() == 0
