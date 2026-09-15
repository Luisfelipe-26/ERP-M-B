"""Consumo y reversión de inventario desde órdenes de trabajo."""
import datetime as dt

import models
from routers.ordenes import _crear_movimiento_consumo_ot, _crear_movimiento_reversion_ot


def _articulo(db, stock, costo):
    p = models.Producto(id_prod="P1", producto="Urea", unidad="kg", costo_promedio=costo,
                        costo_unitario=costo, stock_actual=stock, es_inventariable=True, activo=True)
    db.add(p)
    db.commit()
    return db.query(models.Producto).filter_by(id_prod="P1").one()


def test_consumir_con_stock_existente(db, user):
    """stock_actual vuelve de la BD como Decimal; restarle un float reventaba con stock > 0.

    La operación central de la finca —una OT que consume insumos— fallaba en cuanto el
    artículo tenía existencias. Solo funcionaba sobre stock cero.
    """
    p = _articulo(db, stock=50, costo=500)

    _crear_movimiento_consumo_ot(db, ot_id=1, producto=p, cantidad=10.0, costo_unitario=500.0,
                                 fecha=dt.datetime.now(), usuario_id=user.id)
    db.commit()

    assert float(p.stock_actual) == 40
    mov = db.query(models.MovimientoInventario).filter_by(producto_id="P1", tipo_doc="OT").one()
    assert float(mov.cantidad) == 10
    assert float(mov.costo_unitario) == 500
    assert float(mov.stock_post) == 40


def test_el_consumo_sale_al_costo_promedio_del_producto(db, user):
    """Inventario manda: la OT se valúa al promedio ponderado, no al costo que traiga."""
    p = _articulo(db, stock=50, costo=500)

    _crear_movimiento_consumo_ot(db, ot_id=1, producto=p, cantidad=10.0, costo_unitario=999.0,
                                 fecha=dt.datetime.now(), usuario_id=user.id)
    db.commit()

    mov = db.query(models.MovimientoInventario).filter_by(producto_id="P1", tipo_doc="OT").one()
    assert float(mov.costo_unitario) == 500


def test_no_deja_el_stock_en_negativo(db, user):
    p = _articulo(db, stock=5, costo=500)

    _crear_movimiento_consumo_ot(db, ot_id=1, producto=p, cantidad=10.0, costo_unitario=500.0,
                                 fecha=dt.datetime.now(), usuario_id=user.id)
    db.commit()

    assert float(p.stock_actual) == 0


def test_revertir_devuelve_el_stock(db, user):
    p = _articulo(db, stock=50, costo=500)
    _crear_movimiento_consumo_ot(db, ot_id=1, producto=p, cantidad=10.0, costo_unitario=500.0,
                                 fecha=dt.datetime.now(), usuario_id=user.id)
    db.commit()

    _crear_movimiento_reversion_ot(db, ot_id=1, producto=p, cantidad=10.0, usuario_id=user.id,
                                   costo_unitario=500.0)
    db.commit()

    assert float(p.stock_actual) == 50
