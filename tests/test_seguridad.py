"""Seguridad del API: CORS con lista cerrada y límite de intentos en el login."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import auth
import models
from database import get_db
from main import FRONTENDS, app, origenes_cors
from routers import auth as auth_router

CORVUS = "https://erp-m-f-1.onrender.com"


@pytest.fixture
def cliente():
    """Cliente HTTP contra la app real (sin arranque) con una base en memoria. FastAPI corre
    los endpoints en otros hilos: la conexión de SQLite tiene que poder compartirse."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(models.Usuario(nombre="Ana", email="ana@test.local", rol="admin", activo=True,
                          hashed_password=auth.get_password_hash("clave-correcta")))
    db.commit()
    app.dependency_overrides[get_db] = lambda: db
    for lim in (auth_router.limite_ip, auth_router.limite_cuenta):
        lim._fallos.clear()
    yield TestClient(app)
    app.dependency_overrides.clear()
    db.close()


def _preflight(c, origen):
    return c.options("/api/auth/login", headers={
        "Origin": origen, "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type"})


def test_cors_no_refleja_origenes_ajenos(cliente):
    r = _preflight(cliente, "https://evil.com")
    assert r.headers.get("access-control-allow-origin") is None
    r = cliente.get("/", headers={"Origin": "https://evil.com"})
    assert r.headers.get("access-control-allow-origin") is None
    assert r.headers.get("access-control-allow-credentials") is None


def test_cors_admite_el_frontend(cliente):
    r = _preflight(cliente, CORVUS)
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == CORVUS
    assert "authorization" in r.headers["access-control-allow-headers"].lower()
    assert r.headers.get("access-control-allow-credentials") is None


@pytest.mark.parametrize("env,esperado", [
    ("*", FRONTENDS),
    ("https://erp-m-f-1.onrender.com/,*", FRONTENDS),
    ("https://otro.ejemplo.do", FRONTENDS + ["https://otro.ejemplo.do"]),
    ("", FRONTENDS),
])
def test_cors_origins_nunca_admite_comodin(env, esperado):
    assert origenes_cors(env) == esperado


def test_login_sigue_funcionando(cliente):
    r = cliente.post("/api/auth/login", json={"email": "ana@test.local", "password": "clave-correcta"})
    assert r.status_code == 200 and r.json()["access_token"]
    me = cliente.get("/api/auth/me", headers={"Authorization": f"Bearer {r.json()['access_token']}"})
    assert me.status_code == 200 and me.json()["email"] == "ana@test.local"


def test_fuerza_bruta_recibe_429(cliente):
    for _ in range(5):
        r = cliente.post("/api/auth/login", json={"email": "ana@test.local", "password": "mala"})
        assert r.status_code == 401
    r = cliente.post("/api/auth/login", json={"email": "ana@test.local", "password": "clave-correcta"})
    assert r.status_code == 429, "ni la clave correcta pasa mientras dura el bloqueo"
    assert int(r.headers["retry-after"]) > 0 and "Espere" in r.json()["detail"]
    # /token comparte el límite
    r = cliente.post("/api/auth/token", data={"username": "ana@test.local", "password": "mala"})
    assert r.status_code == 429


def test_el_limite_es_por_conexion(cliente):
    for _ in range(5):
        cliente.post("/api/auth/login", json={"email": "x@test.local", "password": "mala"},
                     headers={"CF-Connecting-IP": "200.1.1.1"})
    otra = cliente.post("/api/auth/login", json={"email": "ana@test.local", "password": "clave-correcta"},
                        headers={"CF-Connecting-IP": "200.1.1.2"})
    assert otra.status_code == 200, "otra oficina no queda bloqueada"


def test_el_limite_por_cuenta_frena_ips_rotativas(cliente):
    for i in range(10):
        cliente.post("/api/auth/login", json={"email": "ana@test.local", "password": "mala"},
                     headers={"CF-Connecting-IP": f"10.0.0.{i}"})
    r = cliente.post("/api/auth/login", json={"email": "ANA@test.local", "password": "mala"},
                     headers={"CF-Connecting-IP": "10.0.1.1"})
    assert r.status_code == 429 and "cuenta" in r.json()["detail"]


def test_un_acierto_borra_los_fallos(cliente):
    for _ in range(4):
        cliente.post("/api/auth/login", json={"email": "ana@test.local", "password": "mala"})
    assert cliente.post("/api/auth/login", json={"email": "ana@test.local", "password": "clave-correcta"}).status_code == 200
    for _ in range(4):
        assert cliente.post("/api/auth/login", json={"email": "ana@test.local", "password": "mala"}).status_code == 401


def test_recover_tambien_tiene_limite(cliente):
    for _ in range(5):
        assert cliente.post("/api/auth/recover", json={"email": "z@test.local", "password": "x"}).status_code == 403
    assert cliente.post("/api/auth/recover", json={"email": "z@test.local", "password": "x"}).status_code == 429


def test_documentacion_apagada_en_produccion():
    """En Render (RENDER=true) /docs, /redoc y /openapi.json no existen; en local sí."""
    import os
    import subprocess
    import sys
    from pathlib import Path
    codigo = "import main; print(main.app.docs_url, main.app.redoc_url, main.app.openapi_url)"
    env = {k: v for k, v in os.environ.items() if k != "ENV"}
    prod = subprocess.run([sys.executable, "-c", codigo], env={**env, "RENDER": "true"},
                          cwd=Path(__file__).parent.parent, capture_output=True, text=True, timeout=120)
    assert prod.stdout.strip().splitlines()[-1] == "None None None", prod.stderr[-500:]
    assert app.docs_url == "/docs" and app.openapi_url == "/openapi.json"
