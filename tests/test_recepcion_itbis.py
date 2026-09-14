"""La CxP que genera la recepción debe cuadrar con sus propias líneas."""
import datetime as dt
from decimal import Decimal

import models
from conftest import ANIO
from routers.compras import RecepcionLinea, RecepcionPayload, recibir_oc


def _oc_mixta(db, proveedor):
    """OC con una línea gravada al 18% y otra exenta."""
    oc = models.OrdenCompra(oc_id="OC-001", fecha=dt.datetime(ANIO, 1, 10),
                            proveedor=proveedor.nombre, proveedor_id=proveedor.id,
                            estado="Aprobada", total_estimado=80_000)
    db.add(oc)
    db.flush()
    gravada = models.OrdenCompraLinea(oc_id="OC-001", producto_id="P1", cantidad=10,
                                      precio_unitario=5_000, subtotal=50_000,
                                      impuesto="itbis_18")
    exenta = models.OrdenCompraLinea(oc_id="OC-001", producto_id="P2", cantidad=10,
                                     precio_unitario=3_000, subtotal=30_000,
                                     impuesto="exento")
    db.add_all([gravada, exenta])
    db.add_all([
        models.Producto(id_prod="P1", producto="Fertilizante", unidad="kg",
                        es_inventariable=False, activo=True),
        models.Producto(id_prod="P2", producto="Servicio fumigación", unidad="svc",
                        es_inventariable=False, activo=True),
    ])
    db.commit()
    return oc, gravada, exenta


def test_el_itbis_de_la_cxp_cuadra_con_el_de_sus_lineas(db, proveedor, user):
    """El encabezado aplicaba 18% plano sobre todo lo recibido, ignorando las líneas exentas."""
    oc, gravada, exenta = _oc_mixta(db, proveedor)

    recibir_oc("OC-001",
               RecepcionPayload(lineas=[
                   RecepcionLinea(linea_id=gravada.id, cantidad_recibida=10),
                   RecepcionLinea(linea_id=exenta.id, cantidad_recibida=10),
               ]),
               db=db, current_user=user)

    cxp = db.query(models.CuentaPorPagar).filter_by(oc_id="OC-001").one()
    itbis_lineas = sum((Decimal(str(l.monto_itbis or 0))
                        for l in db.query(models.LineaCxP).filter_by(cxp_id=cxp.id)),
                       Decimal("0"))

    assert itbis_lineas == Decimal("9000.00"), "solo la línea gravada paga ITBIS"
    assert Decimal(str(cxp.itbis)) == itbis_lineas, (
        f"el ITBIS del encabezado ({cxp.itbis}) no cuadra con el de sus líneas ({itbis_lineas})")


def test_a_un_informal_no_se_le_carga_itbis(db, user):
    """No es contribuyente inscrito: se le paga el subtotal, sin el 18% que antes se sumaba."""
    informal = models.Proveedor(nombre="Vivero Don José", tipo_contribuyente="informal",
                                retencion_isr_pct=2, retencion_itbis_pct=0)
    db.add(informal)
    db.commit()
    oc, gravada, exenta = _oc_mixta(db, informal)

    recibir_oc("OC-001",
               RecepcionPayload(lineas=[RecepcionLinea(linea_id=gravada.id, cantidad_recibida=10)]),
               db=db, current_user=user)

    cxp = db.query(models.CuentaPorPagar).filter_by(oc_id="OC-001").one()
    assert Decimal(str(cxp.subtotal)) == Decimal("50000")
    assert Decimal(str(cxp.itbis)) == Decimal("0")
    assert Decimal(str(cxp.retencion_isr)) == Decimal("1000")           # 2% ISR sí aplica
    assert Decimal(str(cxp.total)) == Decimal("49000")
    linea = db.query(models.LineaCxP).filter_by(cxp_id=cxp.id).one()
    assert Decimal(str(linea.monto_itbis)) == Decimal("0")


def test_el_total_de_la_cxp_es_coherente(db, proveedor, user):
    oc, gravada, exenta = _oc_mixta(db, proveedor)

    recibir_oc("OC-001",
               RecepcionPayload(lineas=[
                   RecepcionLinea(linea_id=gravada.id, cantidad_recibida=10),
                   RecepcionLinea(linea_id=exenta.id, cantidad_recibida=10),
               ]),
               db=db, current_user=user)

    cxp = db.query(models.CuentaPorPagar).filter_by(oc_id="OC-001").one()
    esperado = (Decimal(str(cxp.subtotal)) + Decimal(str(cxp.itbis))
                - Decimal(str(cxp.retencion_isr or 0))
                - Decimal(str(cxp.retencion_itbis or 0)))

    assert Decimal(str(cxp.total)) == esperado
    assert Decimal(str(cxp.saldo_pendiente)) == Decimal(str(cxp.total))
