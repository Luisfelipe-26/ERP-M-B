"""Las reglas de jerarquía (supervisor, contador, admin) miran todos los roles asignados en
Administración, no el texto `usuario.rol`, que solo guarda el primero por orden alfabético.

Las que solo repetían lo que deciden los permisos del módulo ("el operador no hace
contabilidad", "solo admin, supervisor u operador registran") se quitaron.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import auth
import main
import models
from database import get_db
from routers import auth as auth_router


@pytest.fixture
def entorno(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    fabrica = sessionmaker(bind=engine)
    monkeypatch.setattr(main, "SessionLocal", fabrica)
    main.seed_rbac()
    db = fabrica()
    main.app.dependency_overrides[get_db] = lambda: db
    for lim in (auth_router.limite_ip, auth_router.limite_cuenta):
        lim._fallos.clear()
    yield db, TestClient(main.app, raise_server_exceptions=False)
    main.app.dependency_overrides.clear()
    db.close()


def _rol(db, nombre, permisos=None):
    rol = db.query(models.Rol).filter_by(nombre=nombre).first()
    if not rol:
        rol = models.Rol(nombre=nombre, activo=True)
        rol.permisos = db.query(models.Permiso).filter(models.Permiso.codigo.in_(permisos or [])).all()
        db.add(rol)
        db.flush()
    return rol


def _usuario(db, email, texto, roles):
    u = models.Usuario(nombre=email, email=email, hashed_password="x", rol=texto, activo=True)
    u.roles = roles
    db.add(u)
    db.commit()
    return u, {"Authorization": f"Bearer {auth.create_access_token({'sub': email})}"}


def _contador(db):
    return _rol(db, "contador", [f"{m}.{a}" for m in ("contabilidad", "nomina", "efectivo_banco")
                                 for a in ("read", "create", "update", "delete")])


def test_varios_roles_cuentan_todos(entorno):
    """Con "contador" y "supervisor" el texto queda "contador"; antes no podía despachar."""
    db, c = entorno
    _, h = _usuario(db, "dos@test.local", "contador", [_contador(db), _rol(db, "supervisor")])
    r = c.post("/api/ventas/despachos", json={}, headers=h)
    assert r.status_code == 422, r.text[:200]                     # pasa los permisos; faltan datos
    r = c.post("/api/contabilidad/nomina/procesar", json={}, headers=h)
    assert r.status_code != 403, r.text[:200]                     # lo de contador también


def test_manda_el_rol_asignado_no_el_texto(entorno):
    """Texto "supervisor" pero rol asignado operador: no anula cosechas (el menú tampoco se lo muestra)."""
    db, c = entorno
    _, h = _usuario(db, "texto@test.local", "supervisor", [_rol(db, "operador")])
    r = c.post("/api/cosecha/1/anular", params={"motivo": "Prueba de rol"}, headers=h)
    assert r.status_code == 403 and "Supervisor" in r.json()["detail"]


def test_un_rol_propio_no_choca_con_el_texto_operador(entorno):
    """Alta en Administración con rol "operador" y luego el rol "Caja": antes "Acceso denegado"."""
    db, c = entorno
    caja = _rol(db, "Caja", ["efectivo_banco.read", "efectivo_banco.create"])
    _, h = _usuario(db, "caja@test.local", "operador", [caja])
    assert c.get("/api/contabilidad/cxc", headers=h).status_code == 200
    assert c.post("/api/contabilidad/cobros", json={}, headers=h).status_code == 422
    assert c.post("/api/contabilidad/asientos", json={}, headers=h).status_code == 403, "sin permiso de contabilidad"


def test_un_rol_propio_registra_cosecha(entorno):
    """Antes registrar cosecha exigía el texto admin, supervisor u operador."""
    db, c = entorno
    _, h = _usuario(db, "campo@test.local", "Jefe de campo", [_rol(db, "Jefe de campo", ["cosecha.read", "cosecha.create"])])
    assert c.post("/api/cosecha", json={}, headers=h).status_code == 422


def test_el_contador_crea_asientos_manuales(entorno):
    db, c = entorno
    _, h = _usuario(db, "conta@test.local", "contador", [_contador(db)])
    assert c.post("/api/contabilidad/asientos", json={}, headers=h).status_code == 422
    _, h2 = _usuario(db, "op@test.local", "operador", [_rol(db, "operador")])
    assert c.post("/api/contabilidad/asientos", json={}, headers=h2).status_code == 403


def test_usuarios_sin_roles_asignados_siguen_con_su_texto(entorno):
    db, _ = entorno
    viejo_admin, _ = _usuario(db, "viejo@test.local", "admin", [])
    viejo_op, _ = _usuario(db, "viejo-op@test.local", "operador", [])
    assert auth.tiene_rol(viejo_admin, "admin") and auth.roles_de(viejo_op) == {"operador"}
    assert not auth.tiene_rol(viejo_op, "supervisor")


def test_roles_inactivos_no_cuentan(entorno):
    db, _ = entorno
    sup = _rol(db, "supervisor")
    sup.activo = False
    u, _ = _usuario(db, "inactivo@test.local", "supervisor", [sup, _rol(db, "operador")])
    assert auth.roles_de(u) == {"operador"} and not auth.tiene_rol(u, "supervisor")
