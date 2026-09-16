"""El pago se reparte en el presupuesto igual que la factura se devengó."""
import datetime as dt
from decimal import Decimal

import models
from conftest import ANIO
from routers.contabilidad import _registrar_pagado_cxp


def _cxp(db, proveedor, numero, oc_id=None):
    c = models.CuentaPorPagar(numero=numero, proveedor_id=proveedor.id, oc_id=oc_id,
                              fecha_factura=dt.date(ANIO, 1, 20), subtotal=80_000, itbis=14_400,
                              total=94_400, saldo_pendiente=94_400, estado="pendiente")
    db.add(c)
    db.commit()
    return c


def _devengado(db, cxp, cuenta_id, monto, departamento_id=None):
    db.add(models.MovimientoPresupuestario(
        fecha=dt.date(ANIO, 1, 20), tipo="DEVENGADO", anio=ANIO, mes=1, cuenta_id=cuenta_id,
        departamento_id=departamento_id, monto=monto, origen_tipo="CXP", origen_id=cxp.numero))
    db.commit()


def _pagados(db):
    return db.query(models.MovimientoPresupuestario).filter_by(tipo="PAGADO").all()


def test_reparte_el_pago_entre_las_cuentas_que_devengaron(db, user, proveedor, cuenta):
    """Factura de 80k devengada 50k en insumos y 30k en combustible; se pagan 40k."""
    otra = models.CuentaContable(codigo="6.1.02", nombre="Combustible", naturaleza="deudora", tipo="gasto")
    db.add(otra)
    db.commit()
    cxp = _cxp(db, proveedor, "CXP-1", oc_id="OC-1")
    _devengado(db, cxp, cuenta.id, 50_000, departamento_id=7)
    _devengado(db, cxp, otra.id, 30_000, departamento_id=9)

    registrado = _registrar_pagado_cxp(db, cxp, Decimal("40000"), dt.date(ANIO, 1, 25), "PAG-1", user)
    db.commit()

    assert registrado == Decimal("40000.00")
    por_cuenta = {m.cuenta_id: float(m.monto) for m in _pagados(db)}
    assert por_cuenta[cuenta.id] == 25_000, "5/8 del pago"
    assert por_cuenta[otra.id] == 15_000, "3/8 del pago"
    deptos = {m.cuenta_id: m.departamento_id for m in _pagados(db)}
    assert deptos[cuenta.id] == 7 and deptos[otra.id] == 9, "hereda las dimensiones del devengado"


def test_una_factura_sin_oc_tambien_registra_el_pago(db, user, proveedor, cuenta):
    """Antes una factura directa devengaba pero nunca registraba PAGADO."""
    cxp = _cxp(db, proveedor, "CXP-2", oc_id=None)
    _devengado(db, cxp, cuenta.id, 80_000)

    registrado = _registrar_pagado_cxp(db, cxp, Decimal("80000"), dt.date(ANIO, 1, 25), "PAG-2", user)
    db.commit()

    assert registrado == Decimal("80000.00")
    assert len(_pagados(db)) == 1 and _pagados(db)[0].cuenta_id == cuenta.id


def test_sin_devengar_cae_al_compromiso_de_la_oc(db, user, proveedor, cuenta):
    """Pagar antes de validar: no hay base de reparto, se usa el compromiso de la OC."""
    cxp = _cxp(db, proveedor, "CXP-3", oc_id="OC-3")
    db.add(models.CompromisoPresupuestario(anio=ANIO, mes=1, cuenta_id=cuenta.id, monto=80_000,
                                           departamento_id=7, origen_tipo="OC", origen_id="OC-3",
                                           estado="activo"))
    db.commit()

    registrado = _registrar_pagado_cxp(db, cxp, Decimal("10000"), dt.date(ANIO, 1, 25), "PAG-3", user)
    db.commit()

    assert registrado == Decimal("10000")
    m = _pagados(db)[0]
    assert m.cuenta_id == cuenta.id and m.departamento_id == 7


def test_sin_devengar_y_sin_oc_no_registra_nada(db, user, proveedor):
    cxp = _cxp(db, proveedor, "CXP-4", oc_id=None)

    registrado = _registrar_pagado_cxp(db, cxp, Decimal("10000"), dt.date(ANIO, 1, 25), "PAG-4", user)

    assert registrado == Decimal("0")
    assert _pagados(db) == []
