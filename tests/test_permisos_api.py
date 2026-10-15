"""El API aplica los permisos por módulo al escribir, igual que el frontend al mostrar pantallas.

Antes cualquier usuario logueado podía crear, modificar o borrar llamando a la ruta
directamente, aunque su rol solo tuviera lectura.
"""
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import auth
import main
import models
from database import get_db
from routers import auth as auth_router

ESCRITURA = {"POST", "PUT", "PATCH", "DELETE"}


@pytest.fixture
def entorno(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    fabrica = sessionmaker(bind=engine)
    monkeypatch.setattr(main, "SessionLocal", fabrica)
    main.seed_rbac()                                   # roles admin / supervisor / operador por defecto
    db = fabrica()
    main.app.dependency_overrides[get_db] = lambda: db
    for lim in (auth_router.limite_ip, auth_router.limite_cuenta):
        lim._fallos.clear()
    yield db, TestClient(main.app, raise_server_exceptions=False)
    main.app.dependency_overrides.clear()
    db.close()


def _usuario(db, email, rol_nombre=None, permisos=()):
    """Usuario con un rol por defecto, o con un rol propio que tiene solo `permisos`."""
    if rol_nombre:
        rol = db.query(models.Rol).filter_by(nombre=rol_nombre).one()
    else:
        rol = models.Rol(nombre=f"rol-{email}", activo=True)
        rol.permisos = db.query(models.Permiso).filter(models.Permiso.codigo.in_(permisos)).all()
        db.add(rol)
    # El texto `rol` de un usuario con rol propio no es "operador": hay rutas que aún bloquean ese texto.
    u = models.Usuario(nombre=email, email=email, hashed_password="x", rol=rol_nombre or "personalizado", activo=True)
    u.roles = [rol]
    db.add(u)
    db.commit()
    return {"Authorization": f"Bearer {auth.create_access_token({'sub': email})}"}


def _rutas_de_escritura():
    for modulo in main.PERMISOS_MODULO:
        for r in modulo.router.routes:
            if isinstance(r, APIRoute):
                for metodo in sorted(r.methods & ESCRITURA):
                    yield metodo, r.path


def _llamar(c, metodo, ruta, h):
    url = "/".join("1" if s.startswith("{") else s for s in ruta.split("/"))
    return c.request(metodo, url, headers=h, json={})


def test_cada_router_del_api_tiene_su_permiso():
    import pathlib
    routers = {p.stem for p in (pathlib.Path(main.__file__).parent / "routers").glob("*.py")} - {"__init__", "auth"}
    assert {m.__name__.split(".")[-1] for m in main.PERMISOS_MODULO} == routers


def test_un_rol_de_solo_lectura_no_escribe_en_ninguna_ruta(entorno):
    db, c = entorno
    lectura = [p.codigo for p in db.query(models.Permiso).filter_by(accion="read")]
    h = _usuario(db, "lector@test.local", permisos=lectura)
    rutas = list(_rutas_de_escritura())
    assert len(rutas) > 150
    abiertas = []
    for metodo, ruta in rutas:
        r = _llamar(c, metodo, ruta, h)
        if r.status_code != 403:
            abiertas.append(f"{metodo} {ruta} -> {r.status_code}")
    assert not abiertas, "escrituras sin control de permisos:\n" + "\n".join(abiertas)


def test_admin_pasa_el_control_en_todas(entorno):
    """Solo el control de permisos, sin ejecutar las rutas (algunas recalculan o sincronizan)."""
    from starlette.requests import Request
    db, _ = entorno
    h = _usuario(db, "jefe@test.local", "admin")
    for modulo, permiso in main.PERMISOS_MODULO.items():
        for r in modulo.router.routes:
            for metodo in sorted(getattr(r, "methods", set()) & ESCRITURA):
                req = Request({"type": "http", "method": metodo, "path": r.path, "route": r, "query_string": b"",
                               "headers": [(b"authorization", h["Authorization"].encode())]})
                assert permiso(req, db).email == "jefe@test.local", f"{metodo} {r.path}"


@pytest.mark.parametrize("rol,metodo,ruta,permitido", [
    # El operador registra lo de campo (órdenes, cosecha, riego, sanidad); trabajadores solo los ve
    ("operador", "POST", "/api/ordenes", True),
    ("operador", "POST", "/api/cosecha", True),
    ("operador", "POST", "/api/riego/lecturas", True),
    ("operador", "POST", "/api/sanidad/monitoreo", True),
    ("operador", "POST", "/api/trabajadores", False),
    ("operador", "PUT", "/api/trabajadores/T-001", False),
    ("operador", "POST", "/api/clientes", False),
    ("operador", "POST", "/api/contabilidad/pagos", False),
    ("operador", "POST", "/api/contabilidad/presupuestos-documento", False),
    ("supervisor", "POST", "/api/trabajadores", True),
    ("supervisor", "POST", "/api/contabilidad/pagos", True),
    ("supervisor", "POST", "/api/ventas/despachos", True),
    ("supervisor", "POST", "/api/admin/usuarios", False),
])
def test_roles_por_defecto(entorno, rol, metodo, ruta, permitido):
    db, c = entorno
    r = _llamar(c, metodo, ruta, _usuario(db, f"{rol}@test.local", rol))
    bloqueado = r.status_code == 403
    assert bloqueado != permitido, f"{metodo} {ruta} -> {r.status_code} {r.text[:120]}"


def test_pantalla_de_otro_modulo_puede_escribir_lo_suyo(entorno):
    """Efectivo y Banco registra cobros en contabilidad; con eso no puede hacer asientos."""
    db, c = entorno
    h = _usuario(db, "caja@test.local", permisos=["efectivo_banco.read", "efectivo_banco.create"])
    assert c.post("/api/contabilidad/cobros", json={}, headers=h).status_code == 422
    r = c.post("/api/contabilidad/asientos", json={}, headers=h)
    assert r.status_code == 403 and "Contabilidad" in r.json()["detail"]


def test_crear_no_permite_eliminar(entorno):
    db, c = entorno
    h = _usuario(db, "altas@test.local", permisos=["clientes.read", "clientes.create"])
    assert c.post("/api/clientes", json={}, headers=h).status_code == 422
    assert c.put("/api/clientes/1", json={}, headers=h).status_code == 403


def test_lo_publico_sigue_publico(entorno):
    _, c = entorno
    assert c.get("/api/ordenes/plantilla/descargar").status_code in (200, 404)   # sin sesión, como antes
    assert c.post("/api/ventas/despachos", json={}).status_code == 401
    assert c.get("/api/ventas/despachos").status_code == 401


# ─── Lecturas ────────────────────────────────────────────────────────────────

def _rutas_de_lectura():
    for modulo in main.PERMISOS_MODULO:
        for r in modulo.router.routes:
            if isinstance(r, APIRoute) and "GET" in r.methods:
                yield r.path


@pytest.mark.parametrize("permisos,ruta,puede", [
    # La pantalla de Ventas lee clientes, campos y temporadas de cosecha, no contabilidad
    (["ventas.read"], "/api/ventas/liquidaciones", True),
    (["ventas.read"], "/api/clientes", True),
    (["ventas.read"], "/api/cosecha/temporadas", True),
    (["ventas.read"], "/api/campos", True),
    (["ventas.read"], "/api/contabilidad/asientos", False),
    (["ventas.read"], "/api/trabajadores", False),
    # Riego lee los campos; no la nómina ni los proveedores
    (["riego.read"], "/api/riego/lecturas", True),
    (["riego.read"], "/api/campos", True),
    (["riego.read"], "/api/contabilidad/cxp", False),
    (["riego.read"], "/api/proveedores", False),
    # Con un permiso de escritura también se lee el módulo (como en el frontend)
    (["clientes.create"], "/api/clientes", True),
    # Efectivo y Banco ve las facturas de proveedor y las cuentas por cobrar
    (["efectivo_banco.read"], "/api/contabilidad/cxp", True),
    (["efectivo_banco.read"], "/api/contabilidad/cxc", True),
    (["efectivo_banco.read"], "/api/contabilidad/asientos", False),
])
def test_cada_pantalla_lee_lo_suyo(entorno, permisos, ruta, puede):
    db, c = entorno
    r = c.get(ruta, headers=_usuario(db, f"{permisos[0]}-{ruta}@test.local", permisos=permisos))
    assert (r.status_code != 403) == puede, f"{ruta} -> {r.status_code} {r.text[:150]}"


def test_sin_permisos_no_lee_nada_salvo_lo_comun(entorno):
    from permisos_lectura import LECTURAS
    db, c = entorno
    h = _usuario(db, "nadie@test.local", permisos=[])
    abiertas = []
    for ruta in _rutas_de_lectura():
        if ruta in auth.RUTAS_PUBLICAS or LECTURAS.get(ruta) == "*":
            continue
        url = "/".join("1" if s.startswith("{") else s for s in ruta.split("/"))
        r = c.get(url, headers=h)
        if r.status_code != 403:
            abiertas.append(f"{ruta} -> {r.status_code}")
    assert not abiertas, "lecturas sin control: " + ", ".join(abiertas)
    assert c.get("/api/contabilidad/notificaciones", headers=h).status_code == 200, "el menú la pide en toda página"


def test_el_mapa_de_lecturas_esta_al_dia():
    from permisos_lectura import LECTURAS
    rutas = set(_rutas_de_lectura())
    assert set(LECTURAS) <= rutas, f"rutas que ya no existen: {set(LECTURAS) - rutas}"
    validos = {"dashboard", "ordenes", "costos", "analytics", "nomina", "clima", "sanidad", "riego",
               "contabilidad", "activos_fijos", "presupuesto", "clientes", "proveedores", "efectivo_banco",
               "campos", "trabajadores", "productos", "inventario", "actividades", "planificacion",
               "compras", "cosecha", "ventas", "configuracion", "admin"}
    usados = {m for v in LECTURAS.values() if v != "*" for m in v}
    assert usados <= validos, usados - validos


def test_la_campana_muestra_a_cada_uno_lo_suyo(entorno):
    from datetime import date, timedelta
    db, c = entorno
    prov = models.Proveedor(nombre="Agro SRL", activo=True)
    db.add(prov)
    db.flush()
    db.add(models.CuentaPorPagar(numero="CXP-1", proveedor_id=prov.id, fecha_factura=date.today() - timedelta(days=40),
                                 fecha_vencimiento=date.today() - timedelta(days=10), total=1000,
                                 saldo_pendiente=1000, estado="pendiente"))
    db.add(models.Producto(id_prod="FER1", producto="Fertilizante", unidad="kg", stock_actual=2, stock_minimo=10,
                           es_inventariable=True, activo=True))
    db.commit()
    tipos = lambda h: {a["tipo"] for a in c.get("/api/contabilidad/notificaciones", headers=h).json()["alertas"]}
    assert tipos(_usuario(db, "conta@test.local", permisos=["contabilidad.read", "inventario.read"])) >= {"cxp_vencida", "inventario_bajo"}
    assert tipos(_usuario(db, "campo@test.local", permisos=["riego.read"])) == set(), "riego no ve deudas ni stock"
    assert tipos(_usuario(db, "bodega@test.local", permisos=["inventario.read"])) == {"inventario_bajo"}
