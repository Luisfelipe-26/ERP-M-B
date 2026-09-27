"""Compras frente al resto del sistema: presupuesto, inventario, contabilidad, OT y DGII.

Cada prueba reproduce un fallo encontrado en la revisión de septiembre de 2026 con la
configuración por defecto de producción, donde la regla de compra debita Inventario.
"""
import datetime as dt
from decimal import Decimal

import pytest
from fastapi import HTTPException

import models
import schemas
from conftest import ANIO, cuentas_compra, periodo_abierto, presupuestar
from routers.compras import (DevolucionLinea, DevolucionPayload, FacturaDatos, FacturaPayload,
                             RecepcionLinea, RecepcionPayload, aprobar_oc, create_oc, delete_oc,
                             devolver_oc, recibir_oc, registrar_factura_oc, reporte_por_facturar,
                             update_oc, update_oc_estado)
from routers.contabilidad import crear_cxp, dgii_606, registrar_pago
from routers.ordenes import _lineas_cierre_ot

ENERO = dt.date(ANIO, 1, 15)


@pytest.fixture
def e(db, config, proveedor):
    """Plan de cuentas con reglas por defecto: la compra debita Inventario (clase 1)."""
    c = cuentas_compra(db)
    for cod, nom, nat, tipo in [
        ("1.1.01", "Banco", "deudora", "activo"),
        ("1.1.03.02", "Inventario combustibles", "deudora", "activo"),
        ("5.1.02", "Costo insumos", "deudora", "costo"),
        ("5.1.04", "Servicios contratados", "deudora", "costo"),
    ]:
        c[cod] = models.CuentaContable(codigo=cod, nombre=nom, naturaleza=nat, tipo=tipo)
        db.add(c[cod])
    db.flush()
    db.add_all([
        models.ReglaContabilizacion(evento="pago", concepto="pago_proveedor", activo=True,
                                    cuenta_debe_id=c["2.1.01.01"].id, cuenta_haber_id=c["1.1.01"].id),
        models.ReglaContabilizacion(evento="consumo_ot", concepto="salida_insumo", activo=True,
                                    cuenta_debe_id=c["5.1.02"].id, cuenta_haber_id=c["1.1.03.01"].id),
        # Urea: sin cuenta de inventario propia (usa la de la regla); su costo va a 5.1.02
        models.Producto(id_prod="P1", producto="Urea", unidad="kg", costo_promedio=0, stock_actual=0,
                        es_inventariable=True, activo=True, impuesto_compra="itbis_18",
                        cuenta_costo_id=c["5.1.02"].id),
        # Gasoil: inventario propio
        models.Producto(id_prod="P2", producto="Gasoil", unidad="gl", costo_promedio=0, stock_actual=0,
                        es_inventariable=True, activo=True, impuesto_compra="itbis_18",
                        cuenta_inventario_id=c["1.1.03.02"].id, cuenta_costo_id=c["5.1.02"].id),
        # Fumigación aérea: servicio, no lleva stock
        models.Producto(id_prod="S1", producto="Fumigación aérea", unidad="servicio", costo_promedio=0,
                        stock_actual=0, es_inventariable=False, activo=True, impuesto_compra="itbis_18",
                        cuenta_costo_id=c["5.1.04"].id),
    ])
    periodo_abierto(db, ENERO)
    periodo_abierto(db, dt.date.today())
    presupuestar(db, c["5.1.02"].id, 100_000)
    db.commit()
    return c


def _oc(db, user, proveedor, *lineas, aprobar=True):
    r = create_oc(schemas.OrdenCompraCreate(
        proveedor_id=proveedor.id, fecha=dt.datetime.combine(ENERO, dt.time()),
        lineas=[schemas.OCLineaCreate(producto_id=p, cantidad=q, precio_unitario=pu) for p, q, pu in lineas]),
        db=db, current_user=user)
    if aprobar:
        aprobar_oc(r.oc_id, override=False, db=db, current_user=user)
    return r.oc_id, db.query(models.OrdenCompraLinea).filter_by(oc_id=r.oc_id).order_by(models.OrdenCompraLinea.id).all()


def _recibir(db, user, oc_id, *pares, fecha=None, ncf=None):
    return recibir_oc(oc_id, RecepcionPayload(fecha=fecha, lineas=[RecepcionLinea(linea_id=l.id, cantidad_recibida=q)
                                                                   for l, q in pares],
                                              factura=FacturaDatos(ncf=ncf) if ncf else None),
                      db=db, current_user=user)


def _saldo(db, cuenta):
    return round(sum(float(l.debe or 0) - float(l.haber or 0)
                     for l in db.query(models.LineaAsiento).filter_by(cuenta_id=cuenta.id).all()), 2)


def _comprometido(db):
    return sum(float(m.monto) for m in db.query(models.MovimientoPresupuestario).all()
               if m.tipo in ("COMPROMISO", "LIBERACION"))


# ── Presupuesto ──────────────────────────────────────────────────────────────

def test_la_oc_compromete_la_cuenta_de_costo_del_producto_y_el_presupuesto_la_controla(db, user, proveedor, e):
    """Con la regla apuntando a Inventario (clase 1) el control se saltaba toda OC."""
    oc_id, (l,) = _oc(db, user, proveedor, ("P1", 10, 1_000))
    assert l.cuenta_contable_id == e["5.1.02"].id, "la línea guarda su cuenta desde que se crea"
    comp = db.query(models.CompromisoPresupuestario).filter_by(origen_id=oc_id).one()
    assert comp.cuenta_id == e["5.1.02"].id

    oc2, _ = _oc(db, user, proveedor, ("P1", 100, 1_000), aprobar=False)   # 100.000 + 10.000 > 100.000
    with pytest.raises(HTTPException) as ex:
        aprobar_oc(oc2, override=False, db=db, current_user=user)
    assert ex.value.detail["requiere_override"] is True


def test_aprobar_sin_proveedor_registrado_se_rechaza(db, user, e):
    r = create_oc(schemas.OrdenCompraCreate(proveedor="Ferretería de la esquina", fecha=dt.datetime.combine(ENERO, dt.time()),
                                            lineas=[schemas.OCLineaCreate(producto_id="P1", cantidad=1, precio_unitario=100)]),
                  db=db, current_user=user)
    with pytest.raises(HTTPException) as ex:
        aprobar_oc(r.oc_id, override=False, db=db, current_user=user)
    assert "proveedor registrado" in ex.value.detail


# ── Editar y borrar ──────────────────────────────────────────────────────────

def test_editar_una_oc_aprobada_la_devuelve_a_borrador_y_libera_el_compromiso(db, user, proveedor, e):
    oc_id, _ = _oc(db, user, proveedor, ("P1", 10, 1_000))
    assert _comprometido(db) == 10_000

    r = update_oc(oc_id, schemas.OrdenCompraCreate(
        proveedor_id=proveedor.id, fecha=dt.datetime.combine(ENERO, dt.time()),
        lineas=[schemas.OCLineaCreate(producto_id="P1", cantidad=500, precio_unitario=1_000)]),
        db=db, current_user=user)

    assert r["requiere_aprobacion"] is True and r["estado"] == "Borrador"
    assert _comprometido(db) == 0, "el compromiso viejo se libera"
    with pytest.raises(HTTPException):                       # 500.000 no caben en el presupuesto
        aprobar_oc(oc_id, override=False, db=db, current_user=user)


def test_una_oc_con_recepciones_no_se_edita_ni_se_borra(db, user, proveedor, e):
    oc_id, (l,) = _oc(db, user, proveedor, ("P1", 10, 1_000))
    _recibir(db, user, oc_id, (l, 4))
    body = schemas.OrdenCompraCreate(proveedor_id=proveedor.id,
                                     lineas=[schemas.OCLineaCreate(producto_id="P1", cantidad=10, precio_unitario=2_000)])
    with pytest.raises(HTTPException):
        update_oc(oc_id, body, db=db, current_user=user)
    db.rollback()
    with pytest.raises(HTTPException):
        delete_oc(oc_id, db=db, current_user=user)
    db.rollback()
    assert db.query(models.MovimientoInventario).filter_by(oc_referencia=oc_id).count() == 1


def test_borrar_solo_borradores_o_canceladas(db, user, proveedor, e):
    oc_id, _ = _oc(db, user, proveedor, ("P1", 10, 1_000))
    with pytest.raises(HTTPException):
        delete_oc(oc_id, db=db, current_user=user)
    db.rollback()
    update_oc_estado(oc_id, estado="Cancelada", db=db, current_user=user)
    delete_oc(oc_id, db=db, current_user=user)
    assert _comprometido(db) == 0, "cancelar liberó; borrar no deja compromiso colgado"


# ── Recepción ────────────────────────────────────────────────────────────────

def test_cada_linea_se_contabiliza_en_su_cuenta(db, user, proveedor, e):
    """Inventario del producto si lleva stock; gasto si es un servicio."""
    oc_id, (urea, gasoil, fumig) = _oc(db, user, proveedor, ("P1", 10, 1_000), ("P2", 100, 50), ("S1", 1, 20_000))
    _recibir(db, user, oc_id, (urea, 10), (gasoil, 100), (fumig, 1))

    assert _saldo(db, e["1.1.03.01"]) == 10_000
    assert _saldo(db, e["1.1.03.02"]) == 5_000
    assert _saldo(db, e["5.1.04"]) == 20_000, "el servicio no entra al inventario"
    assert _saldo(db, e["2.1.01.04"]) == -35_000, "queda recibido por facturar"
    assert _saldo(db, e["2.1.01.01"]) == 0, "sin factura no hay deuda con el proveedor"
    assert float(db.query(models.Producto).filter_by(id_prod="S1").one().stock_actual) == 0


def test_no_se_recibe_mas_de_lo_pedido(db, user, proveedor, e):
    oc_id, (l,) = _oc(db, user, proveedor, ("P1", 10, 1_000))
    _recibir(db, user, oc_id, (l, 8))
    with pytest.raises(HTTPException) as ex:
        _recibir(db, user, oc_id, (l, 3))
    assert "quedan 2 por recibir" in ex.value.detail
    db.rollback()
    assert float(db.query(models.Producto).filter_by(id_prod="P1").one().stock_actual) == 8


def test_una_linea_de_otra_oc_se_rechaza(db, user, proveedor, e):
    oc_a, _ = _oc(db, user, proveedor, ("P1", 10, 1_000))
    _, (l_b,) = _oc(db, user, proveedor, ("P1", 10, 1_000))
    with pytest.raises(HTTPException):
        _recibir(db, user, oc_a, (l_b, 1))


def test_recepcion_con_fecha_atrasada(db, user, proveedor, e):
    oc_id, (l,) = _oc(db, user, proveedor, ("P1", 10, 1_000))
    ayer = dt.date.today() - dt.timedelta(days=1)
    periodo_abierto(db, ayer)
    _recibir(db, user, oc_id, (l, 10), fecha=ayer)

    assert db.query(models.MovimientoInventario).filter_by(oc_referencia=oc_id).one().fecha.date() == ayer
    assert db.query(models.AsientoContable).filter_by(origen="GR", referencia_id=oc_id).one().fecha == ayer
    registrar_factura_oc(oc_id, FacturaPayload(ncf="B0100000321", fecha_factura=ayer), db=db, current_user=user)
    assert db.query(models.CuentaPorPagar).filter_by(oc_id=oc_id).one().fecha_factura == ayer

    oc2, (l2,) = _oc(db, user, proveedor, ("P1", 1, 1_000))
    with pytest.raises(HTTPException):
        _recibir(db, user, oc2, (l2, 1), fecha=dt.date.today() + dt.timedelta(days=1))


def test_un_producto_desactivado_despues_de_la_oc_igual_entra_al_inventario(db, user, proveedor, e):
    oc_id, (l,) = _oc(db, user, proveedor, ("P1", 10, 1_000))
    db.query(models.Producto).filter_by(id_prod="P1").one().activo = False
    db.commit()
    _recibir(db, user, oc_id, (l, 10))
    assert float(db.query(models.Producto).filter_by(id_prod="P1").one().stock_actual) == 10


# ── Devolución ───────────────────────────────────────────────────────────────

def test_dos_devoluciones_parciales_y_costo_promedio(db, user, proveedor, e):
    oc_id, (l,) = _oc(db, user, proveedor, ("P1", 10, 1_000))
    _recibir(db, user, oc_id, (l, 10))
    p1 = db.query(models.Producto).filter_by(id_prod="P1").one()
    p1.stock_actual, p1.costo_promedio = 20, 800        # había 10 más a 600: promedio 800
    db.commit()

    devolver_oc(oc_id, DevolucionPayload(lineas=[DevolucionLinea(linea_id=l.id, cantidad_devuelta=5)]),
                db=db, current_user=user)
    r = devolver_oc(oc_id, DevolucionPayload(lineas=[DevolucionLinea(linea_id=l.id, cantidad_devuelta=3)]),
                    db=db, current_user=user)

    db.refresh(p1)
    db.refresh(l)
    assert float(l.cantidad_recibida) == 2
    assert float(p1.stock_actual) == 12
    # Valor: 20 x 800 = 16.000, menos 8 x 1.000 devueltos = 8.000 para 12 unidades
    assert float(p1.costo_promedio) == pytest.approx(666.6667, abs=1e-3)
    assert r["asiento"] is not None
    assert _saldo(db, e["1.1.03.01"]) == 10_000 - 8_000, "el inventario del mayor baja lo mismo que la valuación"
    assert db.query(models.OrdenCompra).filter_by(oc_id=oc_id).one().estado == "Parcial"


def test_no_se_devuelve_lo_que_ya_se_consumio(db, user, proveedor, e):
    oc_id, (l,) = _oc(db, user, proveedor, ("P1", 10, 1_000))
    _recibir(db, user, oc_id, (l, 10))
    db.query(models.Producto).filter_by(id_prod="P1").one().stock_actual = 3
    db.commit()
    with pytest.raises(HTTPException) as ex:
        devolver_oc(oc_id, DevolucionPayload(lineas=[DevolucionLinea(linea_id=l.id, cantidad_devuelta=5)]),
                    db=db, current_user=user)
    assert "solo hay 3" in ex.value.detail


def test_devolver_una_factura_ya_pagada_no_pierde_el_credito(db, user, proveedor, e):
    oc_id, (l,) = _oc(db, user, proveedor, ("P1", 10, 1_000))
    _recibir(db, user, oc_id, (l, 10), ncf="B0100000001")
    cxp = db.query(models.CuentaPorPagar).filter_by(oc_id=oc_id).one()
    registrar_pago(schemas.PagoCreate(cxp_id=cxp.id, fecha=dt.date.today(), monto=float(cxp.saldo_pendiente)),
                   db=db, user=user)

    with pytest.raises(HTTPException) as ex:
        devolver_oc(oc_id, DevolucionPayload(lineas=[DevolucionLinea(linea_id=l.id, cantidad_devuelta=2)]),
                    db=db, current_user=user)
    assert "saldo a favor" in ex.value.detail
    db.rollback()
    assert float(db.query(models.Producto).filter_by(id_prod="P1").one().stock_actual) == 10, "nada se movió"
    assert db.query(models.NotaCredito).count() == 0


# ── Orden de trabajo ─────────────────────────────────────────────────────────

def test_la_ot_saca_cada_insumo_de_la_cuenta_por_la_que_entro(db, user, e):
    db.add(models.OrdenTrabajo(ot_id=90, campo_id="C01", actividad_id="A1", estado="Cerrada",
                               costo_insumos=1_500, costo_mano_obra=0, costo_equipo=0))
    db.add_all([
        models.OTDetalle(ot_id=90, producto_id="P1", cantidad_usada=1, costo_unitario=1_000, costo_real=1_000),
        models.OTDetalle(ot_id=90, producto_id="P2", cantidad_usada=10, costo_unitario=50, costo_real=500),
    ])
    db.commit()
    orden = db.query(models.OrdenTrabajo).filter_by(ot_id=90).one()

    haberes = {l["cuenta_id"]: float(l["haber"]) for l in _lineas_cierre_ot(db, orden) if l["haber"]}
    assert haberes == {e["1.1.03.01"].id: 1_000, e["1.1.03.02"].id: 500}


# ── DGII 606 ─────────────────────────────────────────────────────────────────

def test_el_606_trae_itbis_retenido_fecha_de_pago_y_avisa_sin_ncf(db, user, proveedor, e):
    proveedor.retencion_itbis_pct = 30
    db.commit()
    oc_id, (l,) = _oc(db, user, proveedor, ("P1", 10, 1_000))
    _recibir(db, user, oc_id, (l, 10), ncf="B0100000606")
    cxp = db.query(models.CuentaPorPagar).filter_by(oc_id=oc_id).one()
    registrar_pago(schemas.PagoCreate(cxp_id=cxp.id, fecha=dt.date.today(), monto=float(cxp.saldo_pendiente)),
                   db=db, user=user)
    hoy = dt.date.today()
    sin_ncf = crear_cxp(schemas.CuentaPorPagarCreate(proveedor_id=proveedor.id, fecha_factura=hoy,
                                                     subtotal=500, itbis=0),
                        override=False, db=db, user=user)

    r = dgii_606(anio=hoy.year, mes=hoy.month, db=db, user=user)
    fila = next(f for f in r["registros"] if f["cxp"] == cxp.numero)
    assert fila["ncf"] == "B0100000606"
    assert fila["itbis_retenido"] == 540            # 30% de 1.800
    assert fila["fecha_pago"] == str(hoy)
    assert r["sin_ncf"] == [sin_ncf["numero"]] and r["alertas"]


# ── Factura separada de la recepción (fase 2) ────────────────────────────────

def test_devolver_parte_facturada_emite_nota_de_credito_con_itbis(db, user, proveedor, e):
    """Recibe 10 y factura 6: al devolver 7, 4 salen sin facturar y 3 con nota de crédito."""
    proveedor.retencion_itbis_pct = 0
    db.commit()
    oc_id, (l,) = _oc(db, user, proveedor, ("P1", 10, 1_000))
    _recibir(db, user, oc_id, (l, 10))
    registrar_factura_oc(oc_id, FacturaPayload(ncf="B0100000070", lineas=[{"linea_id": l.id, "cantidad": 6}]),
                         db=db, current_user=user)

    r = devolver_oc(oc_id, DevolucionPayload(ncf="B0400000001",
                                             lineas=[DevolucionLinea(linea_id=l.id, cantidad_devuelta=7)]),
                    db=db, current_user=user)

    nc = db.query(models.NotaCredito).filter_by(numero=r["nc_numero"]).one()
    assert float(nc.subtotal) == 3_000 and float(nc.itbis) == 540 and nc.ncf == "B0400000001"
    db.refresh(l)
    assert float(l.cantidad_recibida) == 3 and float(l.cantidad_facturada) == 3
    cxp = db.query(models.CuentaPorPagar).filter_by(oc_id=oc_id).one()
    assert float(cxp.saldo_pendiente) == 6_000 + 1_080 - 3_540
    assert _saldo(db, e["2.1.01.04"]) == 0, "lo no facturado salió de la cuenta puente"
    assert _saldo(db, e["1.1.02.03"]) == 1_080 - 540, "la NC revierte su ITBIS"
    assert _saldo(db, e["2.1.01.01"]) == -float(cxp.saldo_pendiente), "mayor de CxP = auxiliar"
    assert _saldo(db, e["1.1.03.01"]) == 3_000


def test_el_reporte_por_facturar_cuadra_con_la_cuenta_puente(db, user, proveedor, e):
    oc_a, (la,) = _oc(db, user, proveedor, ("P1", 10, 1_000))
    oc_b, (lb,) = _oc(db, user, proveedor, ("P2", 20, 50))
    _recibir(db, user, oc_a, (la, 10))
    _recibir(db, user, oc_b, (lb, 20))
    registrar_factura_oc(oc_a, FacturaPayload(ncf="B0100000080", lineas=[{"linea_id": la.id, "cantidad": 4}]),
                         db=db, current_user=user)

    r = reporte_por_facturar(db=db, _=user)
    assert r["total"] == 6_000 + 1_000
    assert r["saldo_mayor"] == 7_000 and r["diferencia"] == 0


def test_una_cxp_manual_no_puede_colgarse_de_una_oc(db, user, proveedor, e):
    with pytest.raises(HTTPException) as ex:
        crear_cxp(schemas.CuentaPorPagarCreate(proveedor_id=proveedor.id, fecha_factura=dt.date.today(),
                                               subtotal=1_000, itbis=180, oc_id="OC-0001"),
                  override=False, db=db, user=user)
    assert "desde la orden de compra" in ex.value.detail


def test_factura_de_consumo_su_itbis_es_costo(db, user, proveedor, e):
    """Una B02 no da crédito fiscal: su ITBIS va al costo y consume presupuesto."""
    proveedor.retencion_itbis_pct = 0
    db.commit()
    oc_id, (l,) = _oc(db, user, proveedor, ("P1", 10, 1_000))
    _recibir(db, user, oc_id, (l, 10), ncf="B0200000001")
    assert _saldo(db, e["1.1.02.03"]) == 0
    assert _saldo(db, e["5.1.02"]) == 1_800
    devengado = sum(float(m.monto) for m in db.query(models.MovimientoPresupuestario).all() if m.tipo == "DEVENGADO")
    assert devengado == 11_800


def test_ajuste_de_recepciones_anteriores_cuadra_el_mayor(db, user, proveedor, e):
    """Una CxP creada por la recepción vieja (asiento solo por el subtotal) se complementa."""
    from routers.compras import ajuste_asientos_recepciones
    from routers.contabilidad import _crear_asiento_auto
    proveedor.retencion_itbis_pct = 30
    db.commit()
    hoy = dt.date.today()
    a = _crear_asiento_auto(db, hoy, "GR", "OC-VIEJA", "Recepción vieja", [
        {"cuenta_id": e["1.1.03.01"].id, "debe": Decimal("10000"), "haber": 0},
        {"cuenta_id": e["2.1.01.01"].id, "debe": 0, "haber": Decimal("10000")}], "test", requerido=True)
    db.add(models.CuentaPorPagar(numero="CXP-V1", proveedor_id=proveedor.id, oc_id="OC-VIEJA", fecha_factura=hoy,
                                 subtotal=10_000, itbis=1_800, retencion_isr=0, retencion_itbis=540,
                                 total=11_260, saldo_pendiente=11_260, estado="pendiente", asiento_id=a.id))
    db.commit()
    assert _saldo(db, e["2.1.01.01"]) == -10_000

    prueba = ajuste_asientos_recepciones(dry_run=True, fecha=None, db=db, current_user=user)
    assert prueba["facturas"] == 1 and prueba["ajuste_cxp"] == 1_260
    assert _saldo(db, e["2.1.01.01"]) == -10_000, "el modo prueba no toca nada"

    ajuste_asientos_recepciones(dry_run=False, fecha=None, db=db, current_user=user)
    assert _saldo(db, e["2.1.01.01"]) == -11_260
    assert _saldo(db, e["1.1.02.03"]) == 1_800
    assert _saldo(db, e["2.1.02.04"]) == -540
    otra = ajuste_asientos_recepciones(dry_run=True, fecha=None, db=db, current_user=user)
    assert otra["facturas"] == 0, "no se ajusta dos veces"


def test_se_puede_registrar_el_ncf_de_una_factura_vieja(db, user, proveedor, e):
    from routers.contabilidad import DatosFiscalesCxP, datos_fiscales_cxp
    hoy = dt.date.today()
    r = crear_cxp(schemas.CuentaPorPagarCreate(proveedor_id=proveedor.id, fecha_factura=hoy, subtotal=500, itbis=0),
                  override=False, db=db, user=user)
    datos_fiscales_cxp(r["id"], DatosFiscalesCxP(ncf="e31-0000000123"), db=db, user=user)
    cxp = db.query(models.CuentaPorPagar).get(r["id"])
    assert cxp.ncf == "E310000000123" and cxp.tipo_ncf == "E31"
    assert dgii_606(anio=hoy.year, mes=hoy.month, db=db, user=user)["sin_ncf"] == []
