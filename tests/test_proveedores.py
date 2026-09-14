"""Proveedores: el vínculo con OCs y productos es por FK, no por nombre."""
import datetime as dt

import pytest
from fastapi import HTTPException

import models
from conftest import ANIO
from routers.proveedores import (ProveedorCreate, delete_proveedor, resumen_proveedor,
                                 update_proveedor)


def _oc(db, prov, oc_id, estado="Cerrada", total=1_000, **extra):
    o = models.OrdenCompra(oc_id=oc_id, fecha=dt.datetime(ANIO, 1, 10), estado=estado,
                           total_estimado=total, proveedor=prov.nombre, proveedor_id=prov.id,
                           **extra)
    db.add(o)
    db.commit()
    return o


def test_renombrar_no_pierde_el_historial(db, proveedor):
    """Antes el resumen buscaba por nombre: tras renombrar, las OCs desaparecían."""
    _oc(db, proveedor, "OC-001", total=5_000)
    _oc(db, proveedor, "OC-002", total=3_000)

    update_proveedor(proveedor.id, ProveedorCreate(nombre="Agroquímica Dominicana SRL"),
                     db=db, _=None)

    r = resumen_proveedor(proveedor.id, db=db, _=None)
    assert r["num_ocs"] == 2
    assert r["total_compras"] == 8_000
    assert {o["oc_id"] for o in r["ultimas_ocs"]} == {"OC-001", "OC-002"}


def test_renombrar_propaga_el_nombre_a_las_ocs(db, proveedor):
    oc = _oc(db, proveedor, "OC-001")

    update_proveedor(proveedor.id, ProveedorCreate(nombre="Nuevo Nombre"), db=db, _=None)
    db.refresh(oc)

    assert oc.proveedor == "Nuevo Nombre"
    assert oc.proveedor_id == proveedor.id


def test_renombrar_con_ocs_abiertas_ya_no_bloquea(db, proveedor):
    """Con el vínculo por FK, renombrar es seguro aunque haya OCs en curso."""
    _oc(db, proveedor, "OC-001", estado="Aprobada")

    update_proveedor(proveedor.id, ProveedorCreate(nombre="Renombrado"), db=db, _=None)

    assert proveedor.nombre == "Renombrado"


def test_no_permite_renombrar_a_un_nombre_ya_usado(db, proveedor):
    db.add(models.Proveedor(nombre="Otro Proveedor", tipo_contribuyente="formal"))
    db.commit()

    with pytest.raises(HTTPException) as e:
        update_proveedor(proveedor.id, ProveedorCreate(nombre="Otro Proveedor"), db=db, _=None)
    assert e.value.status_code == 400


def test_una_oc_vieja_sin_fk_sigue_contando_por_nombre(db, proveedor):
    """Respaldo para filas anteriores al FK que la migración no alcanzó a vincular."""
    _oc(db, proveedor, "OC-NUEVA")
    vieja = models.OrdenCompra(oc_id="OC-VIEJA", fecha=dt.datetime(ANIO, 1, 1),
                               estado="Cerrada", total_estimado=500,
                               proveedor=proveedor.nombre, proveedor_id=None)
    db.add(vieja)
    db.commit()

    r = resumen_proveedor(proveedor.id, db=db, _=None)
    assert r["num_ocs"] == 2


def test_no_desactiva_con_ocs_abiertas_aunque_el_nombre_difiera(db, proveedor):
    """El check de OCs abiertas usa el FK: un nombre desincronizado ya no lo esquiva."""
    _oc(db, proveedor, "OC-001", estado="Aprobada")
    db.query(models.OrdenCompra).update({"proveedor": "nombre viejo"})
    db.commit()

    with pytest.raises(HTTPException) as e:
        delete_proveedor(proveedor.id, db=db, _=None)
    assert "OC(s) pendientes" in e.value.detail
