"""Creación de órdenes de compra por los caminos que usa el frontend."""
import pytest
from fastapi import HTTPException

import models
import schemas
from routers.compras import create_oc


@pytest.fixture
def producto(db):
    p = models.Producto(id_prod="P1", producto="Urea", unidad="kg", costo_unitario=100,
                        es_inventariable=True, activo=True, impuesto_compra="itbis_18")
    db.add(p)
    db.commit()
    return p


def _oc(**kw):
    base = dict(lineas=[schemas.OCLineaCreate(producto_id="P1", cantidad=10, precio_unitario=100)])
    base.update(kw)
    return schemas.OrdenCompraCreate(**base)


def test_crea_con_proveedor_por_id(db, user, proveedor, producto):
    r = create_oc(_oc(proveedor_id=proveedor.id), db=db, current_user=user)

    oc = db.query(models.OrdenCompra).filter_by(oc_id=r.oc_id).one()
    assert oc.proveedor_id == proveedor.id
    assert oc.proveedor == proveedor.nombre
    assert float(oc.total_estimado) == 1_000
    assert oc.estado == "Borrador"


def test_crea_con_proveedor_solo_por_nombre_y_lo_vincula(db, user, proveedor, producto):
    r = create_oc(_oc(proveedor=proveedor.nombre), db=db, current_user=user)

    oc = db.query(models.OrdenCompra).filter_by(oc_id=r.oc_id).one()
    assert oc.proveedor_id == proveedor.id, "debe resolver el id por el nombre"


def test_crea_sin_proveedor(db, user, producto):
    """Una OC puede nacer sin proveedor y asignarlo después."""
    r = create_oc(_oc(), db=db, current_user=user)
    assert r.oc_id


def test_crea_con_nombre_de_proveedor_desconocido(db, user, producto):
    """Nombre sin match en catálogo: se guarda el texto, sin FK, sin rechazar."""
    r = create_oc(_oc(proveedor="Ferretería de la esquina"), db=db, current_user=user)

    oc = db.query(models.OrdenCompra).filter_by(oc_id=r.oc_id).one()
    assert oc.proveedor == "Ferretería de la esquina"
    assert oc.proveedor_id is None


def test_rechaza_proveedor_id_inexistente(db, user, producto):
    with pytest.raises(HTTPException) as e:
        create_oc(_oc(proveedor_id=9999), db=db, current_user=user)
    assert e.value.status_code == 400
    assert "no existe" in e.value.detail


def test_rechaza_proveedor_inactivo(db, user, producto):
    p = models.Proveedor(nombre="Cerrado SRL", tipo_contribuyente="formal", activo=False)
    db.add(p)
    db.commit()

    with pytest.raises(HTTPException) as e:
        create_oc(_oc(proveedor_id=p.id), db=db, current_user=user)
    assert e.value.status_code == 400
    assert "inactivo" in e.value.detail


def test_rechaza_sin_lineas(db, user, proveedor):
    with pytest.raises(HTTPException) as e:
        create_oc(schemas.OrdenCompraCreate(proveedor_id=proveedor.id, lineas=[]),
                  db=db, current_user=user)
    assert e.value.status_code == 400


def test_rechaza_producto_inexistente(db, user, proveedor):
    with pytest.raises(HTTPException) as e:
        create_oc(_oc(proveedor_id=proveedor.id,
                      lineas=[schemas.OCLineaCreate(producto_id="NOEXISTE", cantidad=1, precio_unitario=1)]),
                  db=db, current_user=user)
    assert e.value.status_code == 400


def test_la_linea_hereda_el_regimen_del_producto(db, user, proveedor):
    db.add(models.Producto(id_prod="SVC", producto="Servicio", unidad="svc",
                           es_inventariable=False, activo=True, impuesto_compra="exento"))
    db.commit()

    r = create_oc(_oc(proveedor_id=proveedor.id,
                      lineas=[schemas.OCLineaCreate(producto_id="SVC", cantidad=1, precio_unitario=500,
                                                    impuesto=None)]),
                  db=db, current_user=user)

    linea = db.query(models.OrdenCompraLinea).filter_by(oc_id=r.oc_id).one()
    assert linea.impuesto == "exento"
