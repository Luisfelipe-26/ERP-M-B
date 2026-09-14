"""Configuración de productos: categorías y el régimen de ITBIS con el que nacen las OC."""
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import models
import schemas
from routers.categorias_producto import (CategoriaCreate, create_categoria,
                                         delete_categoria, list_categorias)
from routers.productos import create_producto, update_producto


def test_crear_producto_con_regimen_exento(db, user):
    """Antes impuesto_compra no era escribible por el API: solo por SQL directo."""
    create_producto(schemas.ProductoCreate(
        id_prod="SVC-1", producto="Fumigación", impuesto_compra="exento",
        es_inventariable=False), db=db, _=user)

    p = db.query(models.Producto).filter_by(id_prod="SVC-1").one()
    assert p.impuesto_compra == "exento"


def test_rechaza_un_regimen_de_itbis_inventado():
    with pytest.raises(ValidationError):
        schemas.ProductoCreate(id_prod="X", producto="X", impuesto_compra="itbis_99")


def test_rechaza_factor_de_conversion_no_positivo():
    with pytest.raises(ValidationError):
        schemas.ProductoCreate(id_prod="X", producto="X", factor_conversion=0)


def test_categoria_presta_sus_cuentas_al_producto(db, user, cuenta):
    cat = create_categoria(CategoriaCreate(nombre="Agroquímicos", cuenta_costo_id=cuenta.id),
                           db=db, _=user)
    create_producto(schemas.ProductoCreate(
        id_prod="P1", producto="Herbicida", categoria_id=cat.id), db=db, _=user)

    p = db.query(models.Producto).filter_by(id_prod="P1").one()
    assert p.categoria_id == cat.id
    assert p.categoria.cuenta_costo_id == cuenta.id

    listado = list_categorias(db=db, _=user)
    assert listado[0].num_productos == 1


def test_categoria_rechaza_una_cuenta_inexistente(db, user):
    with pytest.raises(HTTPException) as e:
        create_categoria(CategoriaCreate(nombre="X", cuenta_costo_id=9999), db=db, _=user)
    assert e.value.status_code == 400


def test_categoria_en_uso_se_desactiva_en_vez_de_borrarse(db, user):
    cat = create_categoria(CategoriaCreate(nombre="Fertilizantes"), db=db, _=user)
    create_producto(schemas.ProductoCreate(
        id_prod="P1", producto="Urea", categoria_id=cat.id), db=db, _=user)

    r = delete_categoria(cat.id, db=db, _=user)

    assert "Desactivada" in r["message"]
    assert db.query(models.CategoriaProducto).get(cat.id).activo is False


def test_categoria_sin_uso_se_borra(db, user):
    cat = create_categoria(CategoriaCreate(nombre="Temporal"), db=db, _=user)

    delete_categoria(cat.id, db=db, _=user)

    assert db.query(models.CategoriaProducto).get(cat.id) is None


def test_editar_producto_no_pisa_el_stock(db, user):
    create_producto(schemas.ProductoCreate(
        id_prod="P1", producto="Urea", stock_actual=50), db=db, _=user)

    update_producto("P1", schemas.ProductoCreate(
        id_prod="P1", producto="Urea 46%", stock_actual=0), db=db, _=user)

    p = db.query(models.Producto).filter_by(id_prod="P1").one()
    assert p.producto == "Urea 46%"
    assert float(p.stock_actual) == 50, "el stock solo lo mueven los movimientos"
