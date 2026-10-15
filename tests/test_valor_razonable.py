"""Valor razonable de la fruta cosechada (NIC 41 párr. 13; NIIF para PYMES 34.5 y 34.9).

La fruta a granel entra al inventario al valor de mercado por kg menos los costos de venta.
El valor del mes sale de las liquidaciones facturadas (venta en pesos por kg despachado); las
cosechas de un mes usan el del mes anterior, o uno fijado a mano.
"""
import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import auth
import main
import models
from conftest import periodo_abierto, saldo_cuenta
from database import get_db
from routers import auth as auth_router
from routers.cosecha import CosechaIn, CosechaLineaIn, _config_cosecha, create_cosecha, valor_calculado_mes, valor_para_fecha
from routers.ventas import ImportarIn, importar_liquidaciones

SEP, OCT = dt.date(2026, 9, 1), dt.date(2026, 10, 1)
TASA = 60
# Dos recepciones de septiembre: 1.000 kg a US$ 1.50 y 500 kg a US$ 1.20 → US$ 2.100 → RD$ 126.000
FILAS = [
    {"fecha": dt.date(2026, 9, 5), "factura": "100", "referencia": "R1", "calibre": "Aguacate Hass Calibre 14",
     "campo": "5", "precio": 1.50, "kg": 1000},
    {"fecha": dt.date(2026, 9, 12), "factura": "100", "referencia": "R2", "calibre": "Aguacate Hass Industria",
     "campo": "5", "precio": 1.20, "kg": 500},
]
VALOR_SEP = 126_000 / 1_500                     # RD$ 84 por kg despachado


@pytest.fixture
def f(db):
    c = {}
    for cod, nom, nat, tipo in [
        ("1.1.02.01", "CxC clientes", "deudora", "activo"),
        ("1.1.03.03", "Fruta cosechada", "deudora", "activo"),
        ("1.1.03.08", "Fruta despachada por liquidar", "deudora", "activo"),
        ("4.1.01", "Venta de aguacate", "acreedora", "ingreso"),
        ("4.3.01", "Ganancia por medición de producto agrícola", "acreedora", "ingreso"),
        ("5.1.11", "Costo de venta aguacate", "deudora", "costo"),
        ("5.2.03", "Merma", "deudora", "costo"),
    ]:
        c[cod] = models.CuentaContable(codigo=cod, nombre=nom, naturaleza=nat, tipo=tipo)
        db.add(c[cod])
    db.flush()
    for ev, con, debe, haber in [
        ("cosecha", "produccion", "1.1.03.03", "4.3.01"),
        ("venta", "factura_cliente", "1.1.02.01", "4.1.01"),
        ("venta", "despacho_por_liquidar", "1.1.03.08", "1.1.03.03"),
        ("inventario", "ajuste", "5.2.03", "1.1.03.03"),
    ]:
        db.add(models.ReglaContabilizacion(evento=ev, concepto=con, activo=True,
                                           cuenta_debe_id=c[debe].id, cuenta_haber_id=c[haber].id))
    db.add(models.Producto(id_prod="AGR", producto="Aguacate a granel", unidad="kg", costo_unitario=20,
                           costo_promedio=0, stock_actual=0, es_inventariable=True, activo=True,
                           cuenta_inventario_id=c["1.1.03.03"].id, cuenta_costo_id=c["5.1.11"].id))
    db.add(models.Producto(id_prod="C18", producto="Aguacate Cal 18", unidad="kg", costo_unitario=35,
                           costo_promedio=0, stock_actual=0, es_inventariable=True, activo=True,
                           cuenta_inventario_id=c["1.1.03.03"].id, cuenta_costo_id=c["5.1.11"].id))
    db.add(models.Campo(id_campo="C05", nombre="Campo 5", area_ha=5, variedad="Hass", activo=True))
    db.add(models.Cliente(id_cliente="PL1", nombre="Planta", activo=True, condicion_pago_dias=30))
    db.flush()
    c["granel"] = models.Calibre(nombre="Fruta de campo", orden=0, producto_id="AGR", es_granel=True)
    c["cal18"] = models.Calibre(nombre="Cal 18", orden=5, producto_id="C18")
    db.add_all([c["granel"], c["cal18"]])
    c["cliente"] = db.query(models.Cliente).one()
    periodo_abierto(db, SEP)
    periodo_abierto(db, OCT)
    db.commit()
    return c


def _importar_septiembre(db, user, f):
    return importar_liquidaciones(ImportarIn(
        cliente_id=f["cliente"].id, moneda="USD", temporada="2026", campos={"5": "C05"}, calibres={},
        facturas={"100": {"ncf": "E310000000100", "tasa_cambio": TASA}}, filas=FILAS),
        dry_run=False, db=db, current_user=user)


def _activar(db, costo_venta=0.0):
    cfg = _config_cosecha(db)
    cfg.valor_razonable, cfg.costo_venta_kg = True, costo_venta
    db.commit()


def _cosechar(db, user, f, fecha, kg=100, calibre="granel"):
    return create_cosecha(CosechaIn(fecha=fecha, campo_id="C05",
                                    lineas=[CosechaLineaIn(calibre_id=f[calibre].id, kg=kg)]),
                          db=db, current_user=user)


def test_el_valor_del_mes_sale_de_las_liquidaciones(db, user, f):
    _importar_septiembre(db, user, f)
    v = valor_calculado_mes(db, 2026, 9)
    assert v["liquidaciones"] == 2 and v["kg"] == 1500
    assert v["venta_dop"] == pytest.approx(126_000) and v["valor_kg"] == pytest.approx(VALOR_SEP)
    assert valor_calculado_mes(db, 2026, 8)["valor_kg"] is None


def test_las_cosechas_usan_el_mes_anterior_o_el_ultimo_con_datos(db, user, f):
    _importar_septiembre(db, user, f)
    _activar(db, costo_venta=4)
    octubre = valor_para_fecha(db, dt.date(2026, 10, 20))
    assert octubre["valor_mercado_kg"] == pytest.approx(VALOR_SEP) and octubre["valor_kg"] == pytest.approx(VALOR_SEP - 4)
    assert octubre["origen"] == "liquidaciones de 09/2026"
    assert valor_para_fecha(db, dt.date(2026, 12, 3))["origen"] == "liquidaciones de 09/2026", "octubre y noviembre sin datos"
    assert valor_para_fecha(db, dt.date(2026, 9, 20)) is None, "antes de septiembre no hay liquidaciones"


def test_un_valor_fijado_a_mano_manda(db, user, f):
    _importar_septiembre(db, user, f)
    _activar(db)
    db.add(models.ValorFrutaMes(anio=2026, mes=10, valor_kg=90, observacion="Según el contador"))
    db.commit()
    v = valor_para_fecha(db, dt.date(2026, 10, 2))
    assert v["valor_kg"] == 90 and v["origen"] == "fijado para 10/2026"


def test_la_cosecha_a_granel_entra_al_valor_razonable(db, user, f):
    _importar_septiembre(db, user, f)
    _activar(db, costo_venta=4)
    ganancia_antes = -saldo_cuenta(db, f["4.3.01"])
    cos = _cosechar(db, user, f, dt.date(2026, 10, 3), kg=100)
    linea = cos["lineas"][0]
    assert linea["costo_unitario"] == pytest.approx(VALOR_SEP - 4, abs=0.0001)
    assert "Valor razonable (liquidaciones de 09/2026)" in linea["base_valor"]
    assert -saldo_cuenta(db, f["4.3.01"]) - ganancia_antes == pytest.approx(100 * (VALOR_SEP - 4), abs=0.01)


def test_la_fruta_clasificada_sigue_al_costo_de_su_producto(db, user, f):
    _importar_septiembre(db, user, f)
    _activar(db)
    cos = _cosechar(db, user, f, dt.date(2026, 10, 3), kg=50, calibre="cal18")
    assert cos["lineas"][0]["costo_unitario"] == 35 and cos["lineas"][0]["base_valor"] == "Costo estándar"


def test_sin_activar_todo_sigue_al_costo_estandar(db, user, f):
    _importar_septiembre(db, user, f)
    cos = _cosechar(db, user, f, dt.date(2026, 10, 3))
    assert cos["lineas"][0]["costo_unitario"] == 20 and cos["lineas"][0]["base_valor"] == "Costo estándar"


def test_activo_pero_sin_ningun_valor_entra_al_estandar_y_lo_dice(db, user, f):
    _activar(db)
    cos = _cosechar(db, user, f, dt.date(2026, 9, 3))
    assert cos["lineas"][0]["costo_unitario"] == 20
    assert cos["lineas"][0]["base_valor"] == "Costo estándar (sin valor razonable aún)"


def test_la_importacion_valora_con_sus_propias_liquidaciones(db, user, f):
    _activar(db, costo_venta=4)
    r = _importar_septiembre(db, user, f)
    assert r["valor_razonable_kg"] == {"09/2026": pytest.approx(VALOR_SEP - 4)}
    lineas = db.query(models.CosechaLinea).all()
    assert len(lineas) == 2 and all(float(l.costo_unitario) == pytest.approx(VALOR_SEP - 4, abs=0.0001) for l in lineas)
    assert all(l.base_valor == "Valor razonable (liquidaciones importadas)" for l in lineas)
    # Despachado y liquidado todo: el inventario queda en cero y el costo de venta es ese valor
    assert saldo_cuenta(db, f["1.1.03.03"]) == pytest.approx(0, abs=0.05)
    assert saldo_cuenta(db, f["5.1.11"]) == pytest.approx(1500 * (VALOR_SEP - 4), abs=0.05)


# ─── API ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def api(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    fabrica = sessionmaker(bind=engine)
    monkeypatch.setattr(main, "SessionLocal", fabrica)
    main.seed_rbac()
    db = fabrica()
    main.app.dependency_overrides[get_db] = lambda: db
    for lim in (auth_router.limite_ip, auth_router.limite_cuenta):
        lim._fallos.clear()
    tokens = {}
    for nombre in ("admin", "supervisor"):
        u = models.Usuario(nombre=nombre, email=f"{nombre}@test.local", hashed_password="x", rol=nombre, activo=True)
        u.roles = [db.query(models.Rol).filter_by(nombre=nombre).one()]
        db.add(u)
        tokens[nombre] = {"Authorization": f"Bearer {auth.create_access_token({'sub': u.email})}"}
    db.commit()
    yield db, TestClient(main.app), tokens
    main.app.dependency_overrides.clear()
    db.close()


def test_api_configura_y_fija_valores(api):
    db, c, h = api
    r = c.get("/api/cosecha/valor-razonable", params={"meses": 3}, headers=h["supervisor"])
    assert r.status_code == 200 and r.json()["valor_razonable"] is False and len(r.json()["meses"]) == 3

    assert c.put("/api/cosecha/valor-razonable/config", json={"valor_razonable": True, "costo_venta_kg": 2},
                 headers=h["supervisor"]).status_code == 403, "es política contable: solo admin"
    r = c.put("/api/cosecha/valor-razonable/config", json={"valor_razonable": True, "costo_venta_kg": 2},
              headers=h["admin"])
    assert r.status_code == 200 and r.json() == {"valor_razonable": True, "costo_venta_kg": 2}

    hoy = dt.date.today()
    r = c.put(f"/api/cosecha/valor-razonable/{hoy.year}/{hoy.month}", json={"valor_kg": 88, "observacion": "Contador"},
              headers=h["admin"])
    assert r.status_code == 200 and r.json()["cosechas_del_mes"]["valor_kg"] == 86
    v = c.get("/api/cosecha/valor-razonable/para-fecha", params={"fecha": str(hoy)}, headers=h["supervisor"]).json()
    assert v["valor_razonable"] is True and v["valor"]["valor_kg"] == 86

    r = c.put(f"/api/cosecha/valor-razonable/{hoy.year}/{hoy.month}", json={"valor_kg": None}, headers=h["admin"])
    assert r.json()["cosechas_del_mes"] is None, "borrado el fijado y sin liquidaciones, no hay valor"
    assert c.put(f"/api/cosecha/valor-razonable/{hoy.year}/13", json={"valor_kg": 5}, headers=h["admin"]).status_code == 400
