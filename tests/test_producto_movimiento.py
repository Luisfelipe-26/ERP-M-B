"""El atajo de movimiento desde la ficha del producto no puede descuadrar el inventario."""
import pytest
from fastapi import HTTPException

import models
import schemas
from routers.productos import registrar_movimiento


@pytest.fixture
def articulo(db):
    p = models.Producto(id_prod="P1", producto="Fertilizante", unidad="kg",
                        costo_unitario=100, costo_promedio=100, stock_actual=50,
                        es_inventariable=True, activo=True)
    db.add(p)
    db.commit()
    return p


def _mov(db, id_prod):
    return (db.query(models.MovimientoInventario)
            .filter_by(producto_id=id_prod)
            .order_by(models.MovimientoInventario.id.desc()).first())


def test_una_entrada_queda_trazable_y_recalcula_el_costo(db, articulo, user):
    """Sin delegar, el movimiento salía sin num_documento ni costo promedio."""
    registrar_movimiento("P1", schemas.MovimientoCreate(
        producto_id="P1", tipo="entrada", cantidad=50, costo_unitario=200,
        referencia="FAC-001"), db=db, current_user=user)

    m = _mov(db, "P1")
    assert m.num_documento and m.num_documento.startswith("GR")
    assert m.tipo_doc == "GR"
    assert m.stock_post == 100
    # 50 uds a 100 + 50 uds a 200 -> promedio ponderado 150
    assert float(m.costo_promedio_post) == 150
    assert float(articulo.stock_actual) == 100
    assert float(articulo.costo_promedio) == 150


def test_una_salida_queda_trazable(db, articulo, user):
    registrar_movimiento("P1", schemas.MovimientoCreate(
        producto_id="P1", tipo="salida", cantidad=20, motivo="Merma"),
        db=db, current_user=user)

    m = _mov(db, "P1")
    assert m.tipo_doc == "GI"
    assert m.motivo == "Merma"
    assert m.stock_post == 30
    assert float(articulo.stock_actual) == 30


def test_una_salida_exige_rol_supervisor(db, articulo):
    from conftest import usuario
    operador = usuario(db, "operador")

    with pytest.raises(HTTPException) as e:
        registrar_movimiento("P1", schemas.MovimientoCreate(
            producto_id="P1", tipo="salida", cantidad=1, motivo="Merma"),
            db=db, current_user=operador)
    assert e.value.status_code == 403


def test_no_permite_salida_sin_stock(db, articulo, user):
    with pytest.raises(HTTPException) as e:
        registrar_movimiento("P1", schemas.MovimientoCreate(
            producto_id="P1", tipo="salida", cantidad=999, motivo="Merma"),
            db=db, current_user=user)
    assert e.value.status_code == 400


def test_rechaza_un_servicio_no_inventariable(db, user):
    db.add(models.Producto(id_prod="S1", producto="Fumigación", unidad="svc",
                           es_inventariable=False, activo=True, stock_actual=0))
    db.commit()

    with pytest.raises(HTTPException) as e:
        registrar_movimiento("S1", schemas.MovimientoCreate(
            producto_id="S1", tipo="entrada", cantidad=1, costo_unitario=10),
            db=db, current_user=user)
    assert e.value.status_code == 400


def test_rechaza_un_tipo_invalido(db, articulo, user):
    with pytest.raises(HTTPException) as e:
        registrar_movimiento("P1", schemas.MovimientoCreate(
            producto_id="P1", tipo="transferencia", cantidad=1),
            db=db, current_user=user)
    assert e.value.status_code == 400
