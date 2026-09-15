"""Inventario a una fecha de corte: stock y valor según el último movimiento hasta ese día."""
import datetime as dt

import models
from conftest import ANIO
from routers.inventario import list_articulos


def _mov(db, fecha, tipo, cant, stock_post, cp_post):
    db.add(models.MovimientoInventario(
        num_documento=f"M-{fecha.day}", producto_id="P1", tipo_doc="GR" if tipo == "entrada" else "GI",
        tipo=tipo, cantidad=cant, costo_unitario=cp_post, stock_post=stock_post,
        costo_promedio_post=cp_post, fecha=dt.datetime.combine(fecha, dt.time(10, 0))))


def _articulo_con_historial(db):
    """10 kg a 100 el 5/ene; +10 kg a 200 el 15/ene (promedio 150); -5 kg el 10/feb."""
    db.add(models.Producto(id_prod="P1", producto="Urea", unidad="kg", stock_actual=15,
                           costo_promedio=150, es_inventariable=True, activo=True))
    db.commit()
    _mov(db, dt.date(ANIO, 1, 5), "entrada", 10, stock_post=10, cp_post=100)
    _mov(db, dt.date(ANIO, 1, 15), "entrada", 10, stock_post=20, cp_post=150)
    _mov(db, dt.date(ANIO, 2, 10), "salida", 5, stock_post=15, cp_post=150)
    db.commit()


def _fila(db, user, hasta=None):
    return next(a for a in list_articulos(hasta=hasta, db=db, _=user) if a["id_prod"] == "P1")


def test_sin_corte_devuelve_el_stock_actual(db, user):
    _articulo_con_historial(db)
    a = _fila(db, user)
    assert a["stock_actual"] == 15
    assert a["valor_inventario"] == 2_250


def test_corte_entre_movimientos(db, user):
    """Al 10 de enero solo había entrado la primera compra."""
    _articulo_con_historial(db)
    a = _fila(db, user, hasta=dt.date(ANIO, 1, 10))
    assert a["stock_actual"] == 10
    assert a["costo_promedio"] == 100
    assert a["valor_inventario"] == 1_000


def test_corte_el_mismo_dia_del_movimiento_lo_incluye(db, user):
    """El corte es inclusivo: el 15 de enero ya cuenta la segunda entrada."""
    _articulo_con_historial(db)
    a = _fila(db, user, hasta=dt.date(ANIO, 1, 15))
    assert a["stock_actual"] == 20
    assert a["costo_promedio"] == 150
    assert a["valor_inventario"] == 3_000


def test_corte_de_fin_de_mes(db, user):
    """Cierre de enero: 20 kg a 150."""
    _articulo_con_historial(db)
    a = _fila(db, user, hasta=dt.date(ANIO, 1, 31))
    assert a["stock_actual"] == 20
    assert a["valor_inventario"] == 3_000


def test_corte_anterior_a_todo_movimiento_da_cero(db, user):
    _articulo_con_historial(db)
    a = _fila(db, user, hasta=dt.date(ANIO - 1, 12, 31))
    assert a["stock_actual"] == 0
    assert a["valor_inventario"] == 0


def test_corte_posterior_coincide_con_el_actual(db, user):
    _articulo_con_historial(db)
    a = _fila(db, user, hasta=dt.date(ANIO, 12, 31))
    assert a["stock_actual"] == 15
    assert a["valor_inventario"] == 2_250
