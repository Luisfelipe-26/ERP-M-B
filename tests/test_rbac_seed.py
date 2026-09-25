"""Un módulo nuevo debe llegar a los roles que ya existen, sin reponer permisos quitados a mano."""
from sqlalchemy.orm import sessionmaker

import main
import models


def _codigos(db, rol_nombre):
    db.expire_all()
    rol = db.query(models.Rol).filter_by(nombre=rol_nombre).one()
    return {p.codigo for p in rol.permisos}


def test_un_modulo_nuevo_llega_a_los_roles_existentes(db, monkeypatch):
    monkeypatch.setattr(main, "SessionLocal", sessionmaker(bind=db.get_bind()))
    main.seed_rbac()

    # Simula una instalación anterior al módulo: sus permisos no existen todavía.
    for rol in db.query(models.Rol).all():
        rol.permisos = [p for p in rol.permisos if p.modulo != "cosecha"]
    db.query(models.Permiso).filter_by(modulo="cosecha").delete()
    # Y un admin le quitó a mano un permiso al supervisor.
    sup = db.query(models.Rol).filter_by(nombre="supervisor").one()
    sup.permisos = [p for p in sup.permisos if p.codigo != "compras.delete"]
    db.commit()
    assert "cosecha.read" not in _codigos(db, "admin")

    main.seed_rbac()

    assert {"cosecha.read", "cosecha.create", "cosecha.update", "cosecha.delete"} <= _codigos(db, "admin")
    assert "cosecha.create" in _codigos(db, "supervisor")
    assert "cosecha.create" in _codigos(db, "operador"), "la cosecha se registra en campo"
    assert "compras.delete" not in _codigos(db, "supervisor"), "no repone lo que un admin quitó"


def test_correr_el_seed_dos_veces_no_duplica(db, monkeypatch):
    monkeypatch.setattr(main, "SessionLocal", sessionmaker(bind=db.get_bind()))
    main.seed_rbac()
    antes = {r.nombre: len(r.permisos) for r in db.query(models.Rol).all()}
    main.seed_rbac()
    db.expire_all()
    assert {r.nombre: len(r.permisos) for r in db.query(models.Rol).all()} == antes
