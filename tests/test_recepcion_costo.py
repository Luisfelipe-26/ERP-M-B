"""El costo con el que la mercancía entra a inventario debe ser el que realmente se paga."""
import datetime as dt

import models
from conftest import ANIO
from routers.compras import RecepcionLinea, RecepcionPayload, recibir_oc


def _oc_con_descuento(db, proveedor, descuento_pct):
    """Línea de 10 kg a RD$1.000 con descuento: subtotal de OC ya neto."""
    precio, cant = 1_000.0, 10
    subtotal = round(cant * precio * (1 - descuento_pct / 100), 2)
    oc = models.OrdenCompra(oc_id="OC-001", fecha=dt.datetime(ANIO, 1, 10),
                            proveedor=proveedor.nombre, proveedor_id=proveedor.id,
                            estado="Aprobada", total_estimado=subtotal)
    db.add(oc)
    db.flush()
    linea = models.OrdenCompraLinea(oc_id="OC-001", producto_id="P1", cantidad=cant,
                                    precio_unitario=precio, descuento_pct=descuento_pct,
                                    subtotal=subtotal, impuesto="itbis_18")
    db.add(linea)
    db.add(models.Producto(id_prod="P1", producto="Urea", unidad="kg", costo_unitario=0,
                           costo_promedio=0, stock_actual=0, es_inventariable=True, activo=True))
    db.commit()
    return oc, linea


def test_la_entrada_a_inventario_respeta_el_descuento_de_la_linea(db, proveedor, user):
    """Con 10% de descuento, cada kg cuesta 900, no 1.000."""
    oc, linea = _oc_con_descuento(db, proveedor, descuento_pct=10)

    recibir_oc("OC-001", RecepcionPayload(lineas=[RecepcionLinea(linea_id=linea.id, cantidad_recibida=10)]),
               db=db, current_user=user)

    mov = db.query(models.MovimientoInventario).filter_by(producto_id="P1", tipo_doc="GR").one()
    prod = db.query(models.Producto).filter_by(id_prod="P1").one()
    assert float(mov.costo_unitario) == 900, f"entró a {mov.costo_unitario}, debía ser el precio neto"
    assert float(prod.costo_promedio) == 900
    assert float(mov.costo_promedio_post) == 900


def test_el_total_recibido_no_supera_al_estimado_con_descuento(db, proveedor, user):
    """Recibir toda la OC no puede dar un total_recibido mayor al total_estimado."""
    oc, linea = _oc_con_descuento(db, proveedor, descuento_pct=10)

    recibir_oc("OC-001", RecepcionPayload(lineas=[RecepcionLinea(linea_id=linea.id, cantidad_recibida=10)]),
               db=db, current_user=user)
    db.refresh(oc)

    assert float(oc.total_recibido) == 9_000
    assert float(oc.total_recibido) <= float(oc.total_estimado)


def test_recibir_sobre_stock_existente_promedia_el_costo(db, proveedor, user):
    """stock_actual es Decimal y la cantidad llega como float: la suma reventaba con stock > 0.

    Con 10 kg a 500 en inventario y 10 kg más a 900 netos, el promedio es 700.
    """
    oc, linea = _oc_con_descuento(db, proveedor, descuento_pct=10)
    prod = db.query(models.Producto).filter_by(id_prod="P1").one()
    prod.stock_actual = 10
    prod.costo_promedio = 500
    db.commit()

    recibir_oc("OC-001", RecepcionPayload(lineas=[RecepcionLinea(linea_id=linea.id, cantidad_recibida=10)]),
               db=db, current_user=user)
    db.refresh(prod)

    assert float(prod.stock_actual) == 20
    assert float(prod.costo_promedio) == 700
    mov = db.query(models.MovimientoInventario).filter_by(producto_id="P1", tipo_doc="GR").one()
    assert float(mov.stock_post) == 20
    assert float(mov.costo_promedio_post) == 700


def test_la_cxp_cobra_el_precio_neto(db, proveedor, user):
    oc, linea = _oc_con_descuento(db, proveedor, descuento_pct=10)

    recibir_oc("OC-001", RecepcionPayload(lineas=[RecepcionLinea(linea_id=linea.id, cantidad_recibida=10)]),
               db=db, current_user=user)

    cxp = db.query(models.CuentaPorPagar).filter_by(oc_id="OC-001").one()
    lcxp = db.query(models.LineaCxP).filter_by(cxp_id=cxp.id).one()
    assert float(cxp.subtotal) == 9_000, "el proveedor factura lo neto"
    assert float(lcxp.subtotal) == 9_000
    assert float(lcxp.descuento_pct) == 10
