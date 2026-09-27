"""Revertir un asiento contabilizado: el original y su reverso cuentan juntos y netean a cero.

Antes el original quedaba "anulado" (fuera de los reportes por líneas) mientras su
reverso sí entraba: el libro mayor y los reportes por dimensión restaban la anulación
dos veces.
"""
import datetime as dt
from decimal import Decimal

import pytest
from fastapi import HTTPException

import models
from conftest import periodo_abierto
from routers.contabilidad import _crear_asiento_auto, anular_asiento, balance_comprobacion, libro_mayor


def test_revertir_deja_la_cuenta_en_cero_en_el_mayor_y_por_dimension(db, user):
    hoy = dt.date.today()
    periodo_abierto(db, hoy)
    gasto = models.CuentaContable(codigo="6.1.09", nombre="Gasto de prueba", naturaleza="deudora", tipo="gasto")
    banco = models.CuentaContable(codigo="1.1.01", nombre="Banco", naturaleza="deudora", tipo="activo")
    db.add_all([gasto, banco])
    db.flush()
    a = _crear_asiento_auto(db, hoy, "MAN", "X1", "Gasto a revertir", [
        {"cuenta_id": gasto.id, "debe": Decimal("100"), "haber": 0, "campo_id": "C01"},
        {"cuenta_id": banco.id, "debe": 0, "haber": Decimal("100"), "campo_id": "C01"}], "test", requerido=True)
    db.commit()

    anular_asiento(a.numero, motivo="Error de registro", db=db, user=user)
    db.refresh(a)
    assert a.estado == "revertido" and a.asiento_reverso_id

    mayor = libro_mayor(cuenta_id=gasto.id, codigo=None, desde=None, hasta=None, campo_id=None,
                        unidad_negocio_id=None, departamento_id=None, almacen_id=None,
                        skip=0, limit=100, db=db, user=user)
    assert len(mayor["items"]) == 2 and mayor["items"][-1]["saldo"] == 0

    bc = balance_comprobacion(periodo_id=None, anio=hoy.year, mes=hoy.month, campo_id="C01",
                              unidad_negocio_id=None, departamento_id=None, almacen_id=None,
                              db=db, user=user)
    fila = next(f for f in bc if f["codigo"] == "6.1.09")
    assert fila["sumas_debe"] == 100 and fila["sumas_haber"] == 100, "cuentan el original y su reverso"
    assert fila["saldo_deudor"] == 0 and fila["saldo_acreedor"] == 0

    with pytest.raises(HTTPException):
        anular_asiento(a.numero, motivo="Otra vez", db=db, user=user)
