"""Facturas y cobros en US$: el mayor se lleva en pesos.

Antes el asiento tomaba el monto en dólares como si fueran pesos: una venta de US$ 10.000
entraba como RD$ 10.000. Ahora la factura se registra a su tasa, el cobro a la del día y la
diferencia va a ganancia o pérdida cambiaria.
"""
import datetime as dt
from decimal import Decimal

import pytest
from fastapi import HTTPException

import models
import schemas
from conftest import periodo_abierto, saldo_cuenta
from routers.contabilidad import ajuste_moneda_cxc, crear_cxc, dgii_607, registrar_cobro

HOY = dt.date.today()


@pytest.fixture
def v(db):
    c = {}
    for cod, nom, nat, tipo in [
        ("1.1.01.03", "Banco", "deudora", "activo"),
        ("1.1.02.01", "CxC clientes", "deudora", "activo"),
        ("4.1.01", "Venta de aguacate", "acreedora", "ingreso"),
        ("4.2.01", "Ingreso por Diferencia Cambiaria", "acreedora", "ingreso"),
        ("6.2.03", "Pérdida por Diferencia Cambiaria", "deudora", "gasto"),
    ]:
        c[cod] = models.CuentaContable(codigo=cod, nombre=nom, naturaleza=nat, tipo=tipo)
        db.add(c[cod])
    db.flush()
    db.add_all([
        models.ReglaContabilizacion(evento="venta", concepto="factura_cliente", activo=True,
                                    cuenta_debe_id=c["1.1.02.01"].id, cuenta_haber_id=c["4.1.01"].id),
        models.ReglaContabilizacion(evento="cobro", concepto="cobro_cliente", activo=True,
                                    cuenta_debe_id=c["1.1.01.03"].id, cuenta_haber_id=c["1.1.02.01"].id),
        models.Cliente(id_cliente="CL1", nombre="Exportadora del Cibao", activo=True),
    ])
    periodo_abierto(db, HOY)
    db.commit()
    c["cliente"] = db.query(models.Cliente).one()
    return c


def _factura(db, user, v, usd=10_000, tasa=60, ncf="B0100000001"):
    return crear_cxc(schemas.CuentaPorCobrarCreate(cliente_id=v["cliente"].id, fecha=HOY, moneda="USD",
                                                   tasa_cambio=tasa, subtotal=usd, itbis=0, ncf=ncf),
                     db=db, user=user)


def _cobro(db, user, cxc_id, usd, tasa):
    return registrar_cobro(schemas.CobroCreate(cxc_id=cxc_id, fecha=HOY, monto=usd, tasa_cambio=tasa),
                           db=db, user=user)


def test_la_factura_en_dolares_entra_al_mayor_en_pesos(db, user, v):
    r = _factura(db, user, v)
    cxc = db.query(models.CuentaPorCobrar).get(r["id"])
    assert float(cxc.total) == 10_000 and float(cxc.total_dop) == 600_000
    assert saldo_cuenta(db, v["1.1.02.01"]) == 600_000
    assert saldo_cuenta(db, v["4.1.01"]) == -600_000
    assert cxc.ncf == "B0100000001" and cxc.tipo_ncf == "B01"


def test_sin_tasa_no_se_factura_en_dolares(db, user, v):
    with pytest.raises(HTTPException) as e:
        _factura(db, user, v, tasa=1)
    assert "tasa" in e.value.detail


def test_el_cobro_registra_la_diferencia_cambiaria(db, user, v):
    r = _factura(db, user, v)
    c1 = _cobro(db, user, r["id"], 4_000, 61)        # la tasa subió: ganancia
    assert c1["monto_dop"] == 244_000 and c1["diferencia_cambiaria"] == 4_000
    c2 = _cobro(db, user, r["id"], 6_000, 59.5)      # bajó: pérdida
    assert c2["diferencia_cambiaria"] == -3_000

    assert saldo_cuenta(db, v["1.1.02.01"]) == 0, "saldada en dólares, la CxC queda en cero en pesos"
    assert saldo_cuenta(db, v["1.1.01.03"]) == 244_000 + 357_000
    assert saldo_cuenta(db, v["4.2.01"]) == -4_000
    assert saldo_cuenta(db, v["6.2.03"]) == 3_000
    cxc = db.query(models.CuentaPorCobrar).get(r["id"])
    assert cxc.estado == "cobrada"


def test_el_ultimo_cobro_no_deja_centavos_sueltos(db, user, v):
    r = _factura(db, user, v, usd=100, tasa=Decimal("58.3333"))
    for _ in range(3):
        saldo = float(db.query(models.CuentaPorCobrar).get(r["id"]).saldo_pendiente)
        _cobro(db, user, r["id"], min(33.33, saldo) if _ < 2 else saldo, 58.3333)
    assert saldo_cuenta(db, v["1.1.02.01"]) == 0


def test_cobrar_en_dolares_exige_la_tasa_del_dia(db, user, v):
    r = _factura(db, user, v)
    with pytest.raises(HTTPException) as e:
        registrar_cobro(schemas.CobroCreate(cxc_id=r["id"], fecha=HOY, monto=100), db=db, user=user)
    assert "tasa" in e.value.detail


def test_el_607_va_en_pesos(db, user, v):
    _factura(db, user, v)
    r = dgii_607(anio=HOY.year, mes=HOY.month, db=db, user=user)
    fila = r["registros"][0]
    assert fila["monto_facturado"] == 600_000 and fila["moneda"] == "USD" and fila["total_moneda"] == 10_000


def test_ajuste_de_facturas_y_cobros_viejos_en_dolares(db, user, v):
    """Documentos registrados con el código viejo: asientos por el monto en dólares."""
    from routers.contabilidad import _crear_asiento_auto
    a = _crear_asiento_auto(db, HOY, "VTA", "CXC-V1", "Venta vieja", [
        {"cuenta_id": v["1.1.02.01"].id, "debe": Decimal("1000"), "haber": 0},
        {"cuenta_id": v["4.1.01"].id, "debe": 0, "haber": Decimal("1000")}], "test", requerido=True)
    cxc = models.CuentaPorCobrar(numero="CXC-V1", cliente_id=v["cliente"].id, fecha=HOY, moneda="USD",
                                 tasa_cambio=60, subtotal=1_000, itbis=0, total=1_000, total_dop=60_000,
                                 saldo_pendiente=600, estado="parcial", asiento_id=a.id)
    db.add(cxc)
    db.flush()
    b = _crear_asiento_auto(db, HOY, "COB", "COB-V1", "Cobro viejo", [
        {"cuenta_id": v["1.1.01.03"].id, "debe": Decimal("400"), "haber": 0},
        {"cuenta_id": v["1.1.02.01"].id, "debe": 0, "haber": Decimal("400")}], "test", requerido=True)
    db.add(models.Cobro(numero="COB-V1", cxc_id=cxc.id, fecha=HOY, monto=400, asiento_id=b.id))
    db.commit()

    prueba = ajuste_moneda_cxc(dry_run=True, fecha=None, db=db, user=user)
    assert prueba["facturas"] == 1 and prueba["cobros"] == 1
    assert prueba["ajuste_dop"] == 59_000 + 23_600
    assert saldo_cuenta(db, v["1.1.02.01"]) == 600, "el modo prueba no toca nada"

    ajuste_moneda_cxc(dry_run=False, fecha=None, db=db, user=user)
    assert saldo_cuenta(db, v["1.1.02.01"]) == 36_000, "US$ 600 pendientes a 60"
    assert saldo_cuenta(db, v["4.1.01"]) == -60_000
    assert saldo_cuenta(db, v["1.1.01.03"]) == 24_000
    assert ajuste_moneda_cxc(dry_run=True, fecha=None, db=db, user=user)["documentos"] == 0, "no se ajusta dos veces"
