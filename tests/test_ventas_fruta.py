"""Venta de fruta por calibre: cosecha → despacho → liquidación del cliente → factura → cobro.

El cliente clasifica en su planta y liquida kg por calibre y rechazo; la factura sale de esa
liquidación en US$ y el costo del despacho se reparte entre costo de venta y merma.
"""
import datetime as dt
from decimal import Decimal

import pytest
from fastapi import HTTPException

import models
import schemas
from conftest import periodo_abierto, saldo_cuenta
from routers.contabilidad import registrar_cobro
from routers.cosecha import CosechaIn, CosechaLineaIn, create_cosecha
from routers.ventas import (DespachoIn, DespachoLineaIn, LiquidacionIn, LiquidacionLineaIn,
                            anular_despacho, anular_liquidacion, crear_despacho, despachos_pendientes,
                            liquidar_despacho, rentabilidad)

HOY = dt.date.today()
TEMP = str(HOY.year)


@pytest.fixture
def f(db):
    c = {}
    for cod, nom, nat, tipo in [
        ("1.1.02.01", "CxC clientes", "deudora", "activo"),
        ("1.1.03.03", "Cosecha terminada", "deudora", "activo"),
        ("1.1.03.08", "Fruta despachada por liquidar", "deudora", "activo"),
        ("4.1.01", "Venta de aguacate", "acreedora", "ingreso"),
        ("5.1.06", "Producción agrícola", "acreedora", "costo"),
        ("5.1.11", "Costo de venta aguacate", "deudora", "costo"),
        ("5.2.03", "Merma y faltantes", "deudora", "costo"),
        ("1.1.01.03", "Banco", "deudora", "activo"),
        ("4.2.01", "Ingreso por Diferencia Cambiaria", "acreedora", "ingreso"),
        ("6.2.03", "Pérdida por Diferencia Cambiaria", "deudora", "gasto"),
    ]:
        c[cod] = models.CuentaContable(codigo=cod, nombre=nom, naturaleza=nat, tipo=tipo)
        db.add(c[cod])
    db.flush()
    for ev, con, debe, haber in [
        ("cosecha", "produccion", "1.1.03.03", "5.1.06"),
        ("venta", "factura_cliente", "1.1.02.01", "4.1.01"),
        ("venta", "despacho_por_liquidar", "1.1.03.08", "1.1.03.03"),
        ("venta", "costo_venta", "5.1.11", "1.1.03.03"),
        ("inventario", "ajuste", "5.2.03", "1.1.03.03"),
        ("cobro", "cobro_cliente", "1.1.01.03", "1.1.02.01"),
    ]:
        db.add(models.ReglaContabilizacion(evento=ev, concepto=con, activo=True,
                                           cuenta_debe_id=c[debe].id, cuenta_haber_id=c[haber].id))
    for pid, nombre, costo in [("F18", "Aguacate Cal 18", 40), ("F22", "Aguacate Cal 22", 35)]:
        db.add(models.Producto(id_prod=pid, producto=nombre, unidad="kg", costo_unitario=costo,
                               costo_promedio=0, stock_actual=0, es_inventariable=True, activo=True,
                               cuenta_inventario_id=c["1.1.03.03"].id, cuenta_costo_id=c["5.1.11"].id))
    db.add(models.Campo(id_campo="C01", nombre="Lote Norte", area_ha=5, variedad="Hass", activo=True))
    db.add(models.Cliente(id_cliente="CL1", nombre="Exportadora del Cibao", activo=True, condicion_pago_dias=30))
    db.flush()
    c["cal18"] = models.Calibre(nombre="Cal 18", orden=1, producto_id="F18")
    c["cal22"] = models.Calibre(nombre="Cal 22", orden=2, producto_id="F22")
    db.add_all([c["cal18"], c["cal22"]])
    db.flush()
    c["cliente"] = db.query(models.Cliente).one()
    for cal, precio in ((c["cal18"], "1.60"), (c["cal22"], "1.40")):
        db.add(models.PrecioCalibre(cliente_id=c["cliente"].id, calibre_id=cal.id, moneda="USD",
                                    precio=Decimal(precio), fecha_desde=HOY - dt.timedelta(days=30)))
    periodo_abierto(db, HOY)
    db.commit()
    return c


def _cosechar(db, user, f, kg18=1000, kg22=500):
    create_cosecha(CosechaIn(fecha=HOY, campo_id="C01", lineas=[
        CosechaLineaIn(calibre_id=f["cal18"].id, kg=kg18), CosechaLineaIn(calibre_id=f["cal22"].id, kg=kg22)]),
        db=db, current_user=user)


def _despachar(db, user, f, kg18=800, kg22=400):
    return crear_despacho(DespachoIn(cliente_id=f["cliente"].id, fecha=HOY, campo_id="C01", conduce="CD-100",
                                     lineas=[DespachoLineaIn(calibre_id=f["cal18"].id, kg=kg18),
                                             DespachoLineaIn(calibre_id=f["cal22"].id, kg=kg22)]),
                          db=db, current_user=user)


def _liquidar(db, user, f, despacho_id, kg18=700, kg22=420, rechazo=60, ncf="E310000000101", tasa=60, **kw):
    return liquidar_despacho(despacho_id, LiquidacionIn(
        fecha=HOY, ncf=ncf, tasa_cambio=tasa, kg_rechazo=rechazo, referencia_cliente="LQ-77",
        lineas=[LiquidacionLineaIn(calibre_id=f["cal18"].id, kg=kg18),
                LiquidacionLineaIn(calibre_id=f["cal22"].id, kg=kg22)], **kw),
        db=db, current_user=user)


def _stock(db, pid):
    return float(db.query(models.Producto).filter_by(id_prod=pid).one().stock_actual)


def test_ciclo_completo_de_venta(db, user, f):
    _cosechar(db, user, f)
    assert saldo_cuenta(db, f["1.1.03.03"]) == 1000 * 40 + 500 * 35

    # ── Despacho: sale del inventario al costo y queda por liquidar ──
    d = _despachar(db, user, f)
    assert d["kg_total"] == 1200 and d["costo_total"] == 46_000
    assert _stock(db, "F18") == 200 and _stock(db, "F22") == 100
    assert saldo_cuenta(db, f["1.1.03.08"]) == 46_000
    assert saldo_cuenta(db, f["1.1.03.03"]) == 57_500 - 46_000
    p = despachos_pendientes(db=db, _=user)
    assert p["costo"] == 46_000 and p["diferencia"] == 0

    # ── Liquidación: el cliente reclasifica, rechaza 60 kg y hay 20 kg de merma de peso ──
    liq = _liquidar(db, user, f, d["id"])
    assert liq["kg_liquidados"] == 1120 and liq["kg_rechazo"] == 60 and liq["kg_merma"] == 20
    assert liq["subtotal"] == 700 * 1.60 + 420 * 1.40                    # precios del libro
    assert liq["venta_dop"] == pytest.approx(1708 * 60)
    assert liq["costo_total"] == 46_000
    assert liq["margen_dop"] == pytest.approx(102_480 - 46_000)
    assert [x["precio_libro"] for x in liq["lineas"]] == [1.6, 1.4]

    cxc = db.query(models.CuentaPorCobrar).get(liq["cxc_id"])
    assert cxc.moneda == "USD" and float(cxc.total) == 1708 and cxc.ncf == "E310000000101"
    assert saldo_cuenta(db, f["1.1.02.01"]) == 102_480
    assert saldo_cuenta(db, f["4.1.01"]) == -102_480
    assert saldo_cuenta(db, f["1.1.03.08"]) == 0, "la liquidación vacía la cuenta de despachado"
    costo_venta = round(46_000 * 1120 / 1200, 2)
    assert saldo_cuenta(db, f["5.1.11"]) == pytest.approx(costo_venta, abs=0.02)
    assert saldo_cuenta(db, f["5.2.03"]) == pytest.approx(46_000 - costo_venta, abs=0.02)
    assert db.query(models.DespachoFruta).get(d["id"]).estado == "liquidado"

    # ── Rentabilidad de la temporada ──
    r = rentabilidad(temporada=TEMP, db=db, _=user)
    c01 = r["por_campo"][0]
    assert (c01["kg_cosechados"], c01["kg_despachados"], c01["kg_liquidados"]) == (1500, 1200, 1120)
    assert c01["pct_rechazo"] == 5.0 and c01["margen_dop"] == pytest.approx(56_480)
    cal18 = next(k for k in r["por_calibre"] if k["calibre"] == "Cal 18")
    assert cal18["kg"] == 700 and cal18["precio_promedio"] == 1.6

    # ── Cobro en US$ con diferencia cambiaria ──
    registrar_cobro(schemas.CobroCreate(cxc_id=cxc.id, fecha=HOY, monto=1708, tasa_cambio=60.5), db=db, user=user)
    assert saldo_cuenta(db, f["1.1.02.01"]) == 0
    assert saldo_cuenta(db, f["4.2.01"]) == pytest.approx(-854), "1.708 x 0,50 de ganancia cambiaria"


def test_anular_la_liquidacion_y_luego_el_despacho_deja_todo_como_antes(db, user, f):
    _cosechar(db, user, f)
    d = _despachar(db, user, f)
    liq = _liquidar(db, user, f, d["id"])

    with pytest.raises(HTTPException) as e:
        anular_despacho(d["id"], motivo="Error de carga", db=db, current_user=user)
    assert "anule primero su liquidación" in e.value.detail
    db.rollback()

    anular_liquidacion(liq["id"], motivo="Liquidación equivocada", db=db, current_user=user)
    assert db.query(models.DespachoFruta).get(d["id"]).estado == "despachado"
    assert db.query(models.CuentaPorCobrar).get(liq["cxc_id"]).estado == "anulada"
    assert saldo_cuenta(db, f["1.1.02.01"]) == 0 and saldo_cuenta(db, f["4.1.01"]) == 0
    assert saldo_cuenta(db, f["1.1.03.08"]) == 46_000, "vuelve a estar por liquidar"

    anular_despacho(d["id"], motivo="Error de carga", db=db, current_user=user)
    assert _stock(db, "F18") == 1000 and _stock(db, "F22") == 500
    assert saldo_cuenta(db, f["1.1.03.08"]) == 0
    assert saldo_cuenta(db, f["1.1.03.03"]) == 57_500


def test_no_se_anula_una_liquidacion_con_cobros(db, user, f):
    _cosechar(db, user, f)
    d = _despachar(db, user, f)
    liq = _liquidar(db, user, f, d["id"])
    registrar_cobro(schemas.CobroCreate(cxc_id=liq["cxc_id"], fecha=HOY, monto=100, tasa_cambio=60),
                    db=db, user=user)
    with pytest.raises(HTTPException) as e:
        anular_liquidacion(liq["id"], motivo="Probar bloqueo", db=db, current_user=user)
    assert "cobros" in e.value.detail


def test_no_se_despacha_mas_de_lo_que_hay(db, user, f):
    _cosechar(db, user, f, kg18=100, kg22=50)
    with pytest.raises(HTTPException) as e:
        _despachar(db, user, f, kg18=150, kg22=10)
    assert "hay 100" in e.value.detail
    db.rollback()
    assert _stock(db, "F18") == 100, "nada se movió"


@pytest.mark.parametrize("kw,msg", [
    ({"kg18": 1000, "kg22": 300, "rechazo": 0}, "tolerancia"),              # 1.300 > 1.200 + 2%
    ({"tasa": 1}, "tasa de cambio"),
    ({"ncf": "123"}, "NCF"),
])
def test_validaciones_de_la_liquidacion(db, user, f, kw, msg):
    _cosechar(db, user, f)
    d = _despachar(db, user, f)
    with pytest.raises(HTTPException) as e:
        _liquidar(db, user, f, d["id"], **kw)
    assert msg in e.value.detail
    db.rollback()
    assert db.query(models.DespachoFruta).get(d["id"]).estado == "despachado"
    assert db.query(models.CuentaPorCobrar).count() == 0


def test_sin_precio_en_el_libro_hay_que_indicarlo(db, user, f):
    _cosechar(db, user, f)
    d = _despachar(db, user, f)
    db.query(models.PrecioCalibre).filter_by(calibre_id=f["cal22"].id).delete()
    db.commit()
    with pytest.raises(HTTPException) as e:
        _liquidar(db, user, f, d["id"])
    assert "no hay precio en el libro" in e.value.detail
    db.rollback()

    liq = liquidar_despacho(d["id"], LiquidacionIn(
        fecha=HOY, ncf="E310000000102", tasa_cambio=60, kg_rechazo=0,
        lineas=[LiquidacionLineaIn(calibre_id=f["cal18"].id, kg=800),
                LiquidacionLineaIn(calibre_id=f["cal22"].id, kg=400, precio=1.35)]),
        db=db, current_user=user)
    linea22 = next(x for x in liq["lineas"] if x["calibre"] == "Cal 22")
    assert linea22["precio"] == 1.35 and linea22["precio_libro"] is None
    assert liq["costo_rechazo"] == 0, "sin rechazo ni merma, todo el costo es de venta"


def test_la_cosecha_con_valor_exige_cuenta_de_inventario(db, user, f):
    db.query(models.Producto).filter_by(id_prod="F18").one().cuenta_inventario_id = None
    db.commit()
    with pytest.raises(HTTPException) as e:
        _cosechar(db, user, f)
    assert "cuenta de inventario" in e.value.detail
