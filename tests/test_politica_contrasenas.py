"""Política de contraseñas: se exige al crear un usuario, al restablecer su clave y en la
recuperación de emergencia."""
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


@pytest.mark.parametrize("clave,falta", [
    ("Corto1", "al menos 10"),
    ("solamenteletras", "letras y números"),
    ("1234567890123", "letras y números"),
    ("aaaa1111aaaa", "repetir"),
    ("Corvus2024!x", "palabras obvias"),
    ("MiAguacate2026", "palabras obvias"),
    ("maria.perez2026", "nombre ni el correo"),
    ("Perez-Lote-2026", "nombre ni el correo"),
    ("x7" * 40, "como máximo 72"),
])
def test_rechaza_contrasenas_debiles(clave, falta):
    problemas = auth.problemas_contrasena(clave, "maria.perez@finca.do", "María Perez")
    assert any(falta in p for p in problemas), problemas


@pytest.mark.parametrize("clave", ["Lote7-riego-norte", "tres palabras 2026 juntas", "Kq8#vT2m!pL4"])
def test_acepta_contrasenas_razonables(clave):
    assert auth.problemas_contrasena(clave, "maria.perez@finca.do", "María Perez") == []


def test_el_mensaje_dice_todo_lo_que_falta():
    with pytest.raises(Exception) as e:
        auth.exigir_contrasena_segura("abc")
    assert e.value.status_code == 400
    assert "al menos 10" in e.value.detail and "letras y números" in e.value.detail


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
    admin = models.Usuario(nombre="Jefa", email="jefa@test.local", hashed_password="x", rol="admin", activo=True)
    admin.roles = [db.query(models.Rol).filter_by(nombre="admin").one()]
    db.add(admin)
    db.commit()
    h = {"Authorization": f"Bearer {auth.create_access_token({'sub': 'jefa@test.local'})}"}
    yield db, TestClient(main.app), h
    main.app.dependency_overrides.clear()
    db.close()


def test_crear_usuario_exige_la_politica(entorno):
    db, c, h = entorno
    nuevo = {"nombre": "Pedro Gil", "email": "pedro@test.local", "rol": "operador"}
    r = c.post("/api/admin/usuarios", json={**nuevo, "password": "123456"}, headers=h)
    assert r.status_code == 400 and "al menos 10" in r.json()["detail"]
    assert db.query(models.Usuario).filter_by(email="pedro@test.local").count() == 0
    assert c.post("/api/admin/usuarios", json={**nuevo, "password": "Lote7-riego-norte"}, headers=h).status_code == 200


def test_restablecer_exige_la_politica(entorno):
    db, c, h = entorno
    u = models.Usuario(nombre="Ana Ruiz", email="ana@test.local", hashed_password=auth.get_password_hash("Vieja-clave-1"),
                       rol="operador", activo=True)
    db.add(u)
    db.commit()
    r = c.post(f"/api/admin/usuarios/{u.id}/reset-password", json={"password": "ana2026ruiz"}, headers=h)
    assert r.status_code == 400 and "nombre ni el correo" in r.json()["detail"]
    db.refresh(u)
    assert auth.verify_password("Vieja-clave-1", u.hashed_password), "la clave no cambió"
    assert c.post(f"/api/admin/usuarios/{u.id}/reset-password", json={"password": "Lote7-riego-norte"},
                  headers=h).status_code == 200


def test_ruta_vieja_de_alta_tambien(entorno):
    _, c, h = entorno
    r = c.post("/api/auth/usuarios", json={"nombre": "Luis", "email": "luis@test.local", "password": "admin12345"},
               headers=h)
    assert r.status_code == 400 and "palabras obvias" in r.json()["detail"]


def test_recuperacion_de_emergencia_tambien(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    main.app.dependency_overrides[get_db] = lambda: db
    for lim in (auth_router.limite_ip, auth_router.limite_cuenta):
        lim._fallos.clear()
    try:
        c = TestClient(main.app)
        r = c.post("/api/auth/recover", json={"email": "nuevo@test.local", "password": "Corvus2024!"})
        assert r.status_code == 400 and "palabras obvias" in r.json()["detail"]
        assert db.query(models.Usuario).count() == 0
    finally:
        main.app.dependency_overrides.clear()
        db.close()
