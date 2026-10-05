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


@pytest.fixture
def granel(db, f):
    """Lo normal: la finca no clasifica; cosecha y despacha a granel y el cliente pone el calibre."""
    db.add(models.Producto(id_prod="FG", producto="Aguacate Hass a granel", unidad="kg", costo_unitario=38,
                           costo_promedio=0, stock_actual=0, es_inventariable=True, activo=True,
                           cuenta_inventario_id=f["1.1.03.03"].id, cuenta_costo_id=f["5.1.11"].id))
    db.flush()
    cal = models.Calibre(nombre="A granel", orden=0, producto_id="FG", es_granel=True)
    db.add(cal)
    db.commit()
    return cal


def test_cosecha_y_despacho_a_granel_liquidados_por_calibre_del_cliente(db, user, f, granel):
    create_cosecha(CosechaIn(fecha=HOY, campo_id="C01", lineas=[CosechaLineaIn(calibre_id=granel.id, kg=1500)]),
                   db=db, current_user=user)
    d = crear_despacho(DespachoIn(cliente_id=f["cliente"].id, fecha=HOY, campo_id="C01",
                                  lineas=[DespachoLineaIn(calibre_id=granel.id, kg=1200)]),
                       db=db, current_user=user)
    assert d["costo_total"] == 1200 * 38 and _stock(db, "FG") == 300

    liq = _liquidar(db, user, f, d["id"], kg18=700, kg22=420, rechazo=60)
    assert liq["subtotal"] == 700 * 1.60 + 420 * 1.40
    assert liq["costo_total"] == 45_600
    assert saldo_cuenta(db, f["1.1.03.08"]) == 0

    r = rentabilidad(temporada=TEMP, db=db, _=user)
    assert {k["calibre"] for k in r["por_calibre"]} == {"Cal 18", "Cal 22"}, "el reporte va por calibre del cliente"
    assert r["por_campo"][0]["kg_cosechados"] == 1500


def test_el_granel_no_lleva_precio_ni_se_liquida(db, user, f, granel):
    from routers.cosecha import ListaPreciosIn, PrecioLineaIn, matriz_precios, registrar_lista_precios
    with pytest.raises(HTTPException) as e:
        registrar_lista_precios(ListaPreciosIn(cliente_id=f["cliente"].id, moneda="USD", fecha_desde=HOY,
                                               lineas=[PrecioLineaIn(calibre_id=granel.id, precio=1.5)]),
                                db=db, current_user=user)
    assert "sin clasificar" in e.value.detail
    db.rollback()
    assert granel.id not in [c["id"] for c in matriz_precios(fecha=HOY, moneda="USD", db=db, _=user)["calibres"]]

    _cosechar(db, user, f)
    d = _despachar(db, user, f)
    with pytest.raises(HTTPException) as e:
        liquidar_despacho(d["id"], LiquidacionIn(fecha=HOY, ncf="E310000000201", tasa_cambio=60,
                                                 lineas=[LiquidacionLineaIn(calibre_id=granel.id, kg=100, precio=1)]),
                          db=db, current_user=user)
    assert "clasificó el cliente" in e.value.detail


def test_un_calibre_a_granel_necesita_producto(db, user, f):
    from routers.cosecha import CalibreIn, create_calibre
    with pytest.raises(HTTPException) as e:
        create_calibre(CalibreIn(nombre="Sin clasificar", es_granel=True), db=db, _=user)
    assert "producto de inventario" in e.value.detail


# ── Auditoría de cosecha y venta (2026-10) ───────────────────────────────────

def test_anular_una_cosecha_revalua_lo_que_queda(db, user, f):
    from routers.cosecha import anular_cosecha
    _cosechar(db, user, f, kg18=1000, kg22=0)                         # 1.000 kg a 40
    p = db.query(models.Producto).filter_by(id_prod="F18").one()
    p.costo_unitario = 50
    db.commit()
    create_cosecha(CosechaIn(fecha=HOY, campo_id="C01", lineas=[CosechaLineaIn(calibre_id=f["cal18"].id, kg=500)]),
                   db=db, current_user=user)                          # 500 kg a 50
    segunda = db.query(models.Cosecha).order_by(models.Cosecha.id.desc()).first()

    anular_cosecha(segunda.id, motivo="Registro de prueba", db=db, current_user=user)
    db.refresh(p)
    assert float(p.stock_actual) == 1000 and float(p.costo_promedio) == pytest.approx(40, abs=0.01)
    assert saldo_cuenta(db, f["1.1.03.03"]) == pytest.approx(1000 * 40, abs=0.05), "mayor = valuación"


def test_anular_cosecha_ya_despachada_indica_que_hacer(db, user, f):
    from routers.cosecha import anular_cosecha
    _cosechar(db, user, f)
    _despachar(db, user, f)
    cos = db.query(models.Cosecha).one()
    with pytest.raises(HTTPException) as e:
        anular_cosecha(cos.id, motivo="Registro de prueba", db=db, current_user=user)
    assert "anule primero ese despacho" in e.value.detail


@pytest.mark.parametrize("cambio,msg", [("inactivo", "inactivo"), ("futura", "futura")])
def test_cosecha_rechaza_producto_inactivo_y_fecha_futura(db, user, f, cambio, msg):
    fecha = HOY
    if cambio == "inactivo":
        db.query(models.Producto).filter_by(id_prod="F18").one().activo = False
        db.commit()
    else:
        fecha = HOY + dt.timedelta(days=1)
    with pytest.raises(HTTPException) as e:
        create_cosecha(CosechaIn(fecha=fecha, campo_id="C01", lineas=[CosechaLineaIn(calibre_id=f["cal18"].id, kg=10)]),
                       db=db, current_user=user)
    assert msg in e.value.detail


def test_no_se_repite_conduce_ni_liquidacion_del_cliente(db, user, f):
    _cosechar(db, user, f)
    d = _despachar(db, user, f, kg18=100, kg22=50)                    # conduce CD-100
    with pytest.raises(HTTPException) as e:
        _despachar(db, user, f, kg18=100, kg22=50)
    assert "CD-100" in e.value.detail
    db.rollback()

    _liquidar(db, user, f, d["id"], kg18=90, kg22=50, rechazo=10)     # referencia LQ-77
    d2 = crear_despacho(DespachoIn(cliente_id=f["cliente"].id, fecha=HOY, campo_id="C01", conduce="CD-101",
                                   lineas=[DespachoLineaIn(calibre_id=f["cal18"].id, kg=100)]),
                        db=db, current_user=user)
    with pytest.raises(HTTPException) as e:
        _liquidar(db, user, f, d2["id"], kg18=100, kg22=0, rechazo=0, ncf="E310000000301")
    assert "LQ-77" in e.value.detail


def test_si_la_bascula_del_cliente_pesa_mas_el_rechazo_igual_lleva_su_costo(db, user, f):
    _cosechar(db, user, f, kg18=1500, kg22=0)
    d = _despachar(db, user, f, kg18=1200, kg22=0)                    # 1.200 kg, costo 48.000
    liq = _liquidar(db, user, f, d["id"], kg18=1150, kg22=0, rechazo=70)   # 1.220 kg, dentro del 2%
    assert liq["costo_rechazo"] == pytest.approx(48_000 * 70 / 1220, abs=0.02)
    assert liq["costo_total"] == 48_000


def test_rentabilidad_con_costo_real_de_produccion(db, user, f):
    _cosechar(db, user, f)                                            # 1.500 kg
    db.add(models.OrdenTrabajo(ot_id=501, campo_id="C01", actividad_id="A1", estado="Cerrada",
                               fecha_ejecucion=dt.datetime.combine(HOY, dt.time(8)), costo_total=30_000))
    db.commit()
    d = _despachar(db, user, f)
    _liquidar(db, user, f, d["id"])
    c01 = rentabilidad(temporada=TEMP, db=db, _=user)["por_campo"][0]
    assert c01["costo_produccion"] == 30_000
    assert c01["costo_kg"] == 20.0
    assert c01["resultado"] == pytest.approx(102_480 - 30_000)


def test_los_movimientos_de_cosechas_anuladas_se_pueden_ocultar(db, user, f):
    from routers.cosecha import anular_cosecha
    from routers.inventario import list_movimientos
    _cosechar(db, user, f, kg18=100, kg22=0)
    _cosechar(db, user, f, kg18=50, kg22=0)
    prueba = db.query(models.Cosecha).order_by(models.Cosecha.id.desc()).first()
    anular_cosecha(prueba.id, motivo="Registro de prueba", db=db, current_user=user)

    def contar(ocultar):
        return list_movimientos(producto_id=None, tipo_doc=None, fecha_desde=None, fecha_hasta=None,
                                ocultar_anulados=ocultar, limit=500, offset=0, db=db, _=user)["total"]
    assert contar(False) == 3 and contar(True) == 1


def test_un_calibre_con_precios_se_desactiva_en_vez_de_borrarse(db, user, f):
    from routers.cosecha import delete_calibre
    r = delete_calibre(f["cal22"].id, db=db, _=user)
    assert "Desactivado" in r["message"]
    assert db.query(models.Calibre).get(f["cal22"].id).activo is False


def test_el_costo_de_venta_exige_la_cuenta_de_costo_del_producto(db, user, f):
    _cosechar(db, user, f)
    d = _despachar(db, user, f)
    db.query(models.Producto).filter_by(id_prod="F18").one().cuenta_costo_id = None
    db.commit()
    with pytest.raises(HTTPException) as e:
        _liquidar(db, user, f, d["id"])
    assert "Cuenta Costo" in e.value.detail
    db.rollback()
    assert db.query(models.CuentaPorCobrar).count() == 0, "sin cuenta de costo no se factura a medias"


def test_packout_retorno_y_distribucion_por_calibre(db, user, f):
    _cosechar(db, user, f)
    d = _despachar(db, user, f)                                       # 1.200 kg
    _liquidar(db, user, f, d["id"], kg18=700, kg22=420, rechazo=60)   # 1.120 kg pagados
    _despachar_pendiente = crear_despacho(DespachoIn(cliente_id=f["cliente"].id, fecha=HOY, campo_id="C01",
                                                     conduce="CD-200",
                                                     lineas=[DespachoLineaIn(calibre_id=f["cal18"].id, kg=100)]),
                                          db=db, current_user=user)
    r = rentabilidad(temporada=TEMP, db=db, _=user)
    c01 = r["por_campo"][0]
    assert c01["packout_pct"] == round(1120 / 1200 * 100, 1), "lo aún no liquidado no cuenta en el packout"
    assert c01["pct_rechazo"] == 5.0
    assert c01["retorno_kg"] == round(102_480 / 1200, 2)
    assert c01["kg_por_liquidar"] == 100
    cal18 = next(k for k in r["por_calibre"] if k["calibre"] == "Cal 18")
    assert cal18["pct"] == 62.5
    assert r["totales"]["packout_pct"] == c01["packout_pct"]


def test_liquidar_sin_facturar_y_facturar_despues(db, user, f):
    from routers.ventas import FacturaVentaIn, facturar_liquidaciones
    _cosechar(db, user, f)
    d = _despachar(db, user, f)
    liq = _liquidar(db, user, f, d["id"], ncf=None, tasa=None, facturar=False)
    assert liq["por_facturar"] and liq["cxc_id"] is None
    assert db.query(models.CuentaPorCobrar).count() == 0
    assert saldo_cuenta(db, f["1.1.03.08"]) == 46_000, "el costo sigue por liquidar hasta facturar"
    p = despachos_pendientes(db=db, _=user)
    assert p["liquidaciones_por_facturar"] == 1 and p["diferencia"] == 0

    fac = facturar_liquidaciones(FacturaVentaIn(liquidacion_ids=[liq["id"]], ncf="E310000000101", tasa_cambio=60),
                                 db=db, current_user=user)
    assert fac["total"] == 1708 and fac["total_dop"] == 1708 * 60
    assert saldo_cuenta(db, f["1.1.03.08"]) == 0
    assert despachos_pendientes(db=db, _=user)["diferencia"] == 0


def test_el_mismo_conduce_puede_traer_fruta_de_otro_campo(db, user, f):
    db.add(models.Campo(id_campo="C02", nombre="Lote Sur", area_ha=3, variedad="Hass", activo=True))
    db.commit()
    _cosechar(db, user, f)
    _despachar(db, user, f, kg18=100, kg22=50)                        # CD-100, campo C01
    d2 = crear_despacho(DespachoIn(cliente_id=f["cliente"].id, fecha=HOY, campo_id="C02", conduce="CD-100",
                                   lineas=[DespachoLineaIn(calibre_id=f["cal18"].id, kg=100)]),
                        db=db, current_user=user)
    assert d2["conduce"] == "CD-100" and d2["campo_id"] == "C02"
