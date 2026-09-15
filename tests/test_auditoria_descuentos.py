"""La auditoría encuentra las recepciones valuadas al precio bruto y no marca las correctas."""
import datetime as dt

import models
from conftest import ANIO
from routers.compras import (RecepcionLinea, RecepcionPayload, auditoria_descuentos_brutos,
                             recibir_oc)


def _linea_con_descuento(db, proveedor, oc_id, descuento_pct=10, precio=1_000.0, cant=10):
    subtotal = round(cant * precio * (1 - descuento_pct / 100), 2)
    db.add(models.OrdenCompra(oc_id=oc_id, fecha=dt.datetime(ANIO, 1, 10), proveedor=proveedor.nombre,
                              proveedor_id=proveedor.id, estado="Recibida", total_estimado=subtotal))
    db.flush()
    l = models.OrdenCompraLinea(oc_id=oc_id, producto_id="P1", cantidad=cant, cantidad_recibida=cant,
                                precio_unitario=precio, descuento_pct=descuento_pct,
                                subtotal=subtotal, impuesto="itbis_18")
    db.add(l)
    db.commit()
    return l


def _recepcion_vieja_bruta(db, proveedor, linea):
    """Lo que dejaba el código anterior: GR y CxP al precio sin descontar."""
    bruto = float(linea.precio_unitario)
    cant = float(linea.cantidad_recibida)
    db.add(models.MovimientoInventario(num_documento="GR-OLD", producto_id="P1", tipo_doc="GR",
                                       tipo="entrada", cantidad=cant, costo_unitario=bruto,
                                       oc_referencia=linea.oc_id, fecha=dt.datetime(ANIO, 1, 15)))
    cxp = models.CuentaPorPagar(numero="CXP-OLD", proveedor_id=proveedor.id, oc_id=linea.oc_id,
                                fecha_factura=dt.date(ANIO, 1, 15), subtotal=cant * bruto,
                                itbis=cant * bruto * 0.18, total=cant * bruto * 1.18,
                                saldo_pendiente=0, estado="pagada")
    db.add(cxp)
    db.flush()
    db.add(models.LineaCxP(cxp_id=cxp.id, producto_id="P1", oc_linea_id=linea.id, cantidad=cant,
                           precio_unitario=bruto, descuento_pct=float(linea.descuento_pct),
                           impuesto="itbis_18", subtotal=cant * bruto))
    db.commit()


def test_detecta_una_recepcion_valuada_al_bruto(db, proveedor, user):
    db.add(models.Producto(id_prod="P1", producto="Urea", unidad="kg", es_inventariable=True, activo=True))
    linea = _linea_con_descuento(db, proveedor, "OC-VIEJA")
    _recepcion_vieja_bruta(db, proveedor, linea)

    r = auditoria_descuentos_brutos(db=db, _=user)

    assert r["lineas_afectadas"] == 1
    item = r["items"][0]
    assert item["precio_bruto"] == 1_000 and item["precio_neto"] == 900
    assert item["sobrecosto_inventario"] == 1_000          # 10 uds x (1000 - 900)
    assert item["sobrepago_cxp"] == 1_000
    assert item["cxp"][0]["ya_pagada"] is True
    assert r["cxp_ya_pagadas"] == 1
    assert r["sobrepago_cxp_total"] == 1_000


def test_una_recepcion_correcta_no_aparece(db, proveedor, user):
    """Con el fix, recibir_oc ya valúa al neto: la auditoría debe quedar vacía."""
    db.add(models.Producto(id_prod="P1", producto="Urea", unidad="kg", costo_promedio=0,
                           stock_actual=0, es_inventariable=True, activo=True))
    linea = _linea_con_descuento(db, proveedor, "OC-NUEVA")
    linea.cantidad_recibida = 0
    oc = db.query(models.OrdenCompra).filter_by(oc_id="OC-NUEVA").one()
    oc.estado = "Aprobada"
    db.commit()

    recibir_oc("OC-NUEVA", RecepcionPayload(lineas=[RecepcionLinea(linea_id=linea.id, cantidad_recibida=10)]),
               db=db, current_user=user)

    r = auditoria_descuentos_brutos(db=db, _=user)
    assert r["lineas_afectadas"] == 0


def test_ignora_lineas_sin_descuento(db, proveedor, user):
    db.add(models.Producto(id_prod="P1", producto="Urea", unidad="kg", es_inventariable=True, activo=True))
    linea = _linea_con_descuento(db, proveedor, "OC-SIN", descuento_pct=0)
    _recepcion_vieja_bruta(db, proveedor, linea)

    r = auditoria_descuentos_brutos(db=db, _=user)
    assert r["lineas_afectadas"] == 0
