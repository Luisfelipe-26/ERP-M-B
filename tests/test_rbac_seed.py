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


def test_los_modulos_de_solo_lectura_no_dan_escritura(db, monkeypatch):
    monkeypatch.setattr(main, "SessionLocal", sessionmaker(bind=db.get_bind()))
    main.seed_rbac()
    op = _codigos(db, "operador")
    assert {"contabilidad.read", "trabajadores.read", "clientes.read"} <= op
    assert not {"contabilidad.create", "trabajadores.update", "clientes.delete", "efectivo_banco.create"} & op
    assert {"ordenes.create", "cosecha.update", "riego.create", "sanidad.update"} <= op, "lo de campo sí lo escribe"
    sup = _codigos(db, "supervisor")
    assert "nomina.read" in sup and "nomina.create" not in sup


def _como_lo_dejo_el_error(db):
    """Roles de una instalación anterior: los módulos de lectura con las cuatro acciones."""
    def todos(*mods):
        return db.query(models.Permiso).filter(models.Permiso.modulo.in_(mods)).all()
    op = db.query(models.Rol).filter_by(nombre="operador").one()
    op.permisos = todos("ordenes", "dashboard", "costos", "inventario", "actividades", "campos", "cosecha",
                        "analytics", "nomina", "clima", "sanidad", "riego", "trabajadores", "productos",
                        "contabilidad", "clientes", "proveedores", "presupuesto", "activos_fijos",
                        "efectivo_banco", "compras")
    sup = db.query(models.Rol).filter_by(nombre="supervisor").one()
    sup.permisos = todos("dashboard", "ordenes", "costos", "analytics", "clima", "sanidad", "riego",
                         "contabilidad", "campos", "trabajadores", "productos", "inventario", "actividades",
                         "compras", "clientes", "proveedores", "efectivo_banco", "activos_fijos",
                         "presupuesto", "cosecha", "ventas", "nomina")
    db.commit()


def test_corrige_los_roles_que_dejo_el_error(db, monkeypatch):
    monkeypatch.setattr(main, "SessionLocal", sessionmaker(bind=db.get_bind()))
    main.seed_rbac()
    _como_lo_dejo_el_error(db)
    assert "contabilidad.create" in _codigos(db, "operador")

    main.seed_rbac()
    op = _codigos(db, "operador")
    assert "contabilidad.read" in op and not {"contabilidad.create", "efectivo_banco.update", "trabajadores.delete"} & op
    assert {"riego.create", "sanidad.delete", "ordenes.update"} <= op, "lo de campo queda igual"
    sup = _codigos(db, "supervisor")
    assert "nomina.read" in sup and "nomina.update" not in sup and "contabilidad.create" in sup

    antes = _codigos(db, "operador")
    main.seed_rbac()
    assert _codigos(db, "operador") == antes, "corregido, no se repite"


def test_respeta_los_roles_que_cambio_un_administrador(db, monkeypatch):
    monkeypatch.setattr(main, "SessionLocal", sessionmaker(bind=db.get_bind()))
    main.seed_rbac()
    _como_lo_dejo_el_error(db)
    op = db.query(models.Rol).filter_by(nombre="operador").one()
    op.permisos = [p for p in op.permisos if p.codigo != "clientes.delete"]       # ajuste a mano
    db.commit()
    main.seed_rbac()
    op = _codigos(db, "operador")
    assert "contabilidad.create" in op and "clientes.delete" not in op, "no se toca un rol ya revisado"
