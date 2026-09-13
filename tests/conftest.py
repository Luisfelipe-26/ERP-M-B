"""Fixtures comunes: BD en memoria construida desde los modelos, sin tocar producción."""
import datetime as dt
import sys
import types
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import models  # noqa: E402

ANIO = 2026
MESES_PRES = ["monto_ene", "monto_feb", "monto_mar", "monto_abr", "monto_may", "monto_jun",
              "monto_jul", "monto_ago", "monto_sep", "monto_oct", "monto_nov", "monto_dic"]


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def user():
    return types.SimpleNamespace(id=1, rol="admin", nombre="test")


@pytest.fixture
def config(db):
    """Control presupuestario activo con los umbrales por defecto."""
    cfg = models.ConfigPresupuesto(
        umbral_alerta=85, umbral_bloqueo=100, control_habilitado=True,
        dim_campo=True, dim_unidad_negocio=True, dim_departamento=True)
    db.add(cfg)
    db.commit()
    return cfg


@pytest.fixture
def cuenta(db):
    c = models.CuentaContable(codigo="6.1.01", nombre="Insumos",
                              naturaleza="deudora", tipo="gasto")
    db.add(c)
    db.commit()
    return c


@pytest.fixture
def proveedor(db):
    p = models.Proveedor(nombre="Agroquímica SA", tipo_contribuyente="formal")
    db.add(p)
    db.commit()
    return p


def presupuestar(db, cuenta_id, monto_mensual, *, meses=12, departamento_id=None,
                 campo_id=None, unidad_negocio_id=None, escenario="principal"):
    """Crea una línea de presupuesto aprobada con el mismo monto en los primeros N meses."""
    p = models.Presupuesto(anio=ANIO, cuenta_id=cuenta_id, estado="aprobado",
                           escenario=escenario, departamento_id=departamento_id,
                           campo_id=campo_id, unidad_negocio_id=unidad_negocio_id)
    for mk in MESES_PRES[:meses]:
        setattr(p, mk, monto_mensual)
    db.add(p)
    db.commit()
    return p


def mover(db, cuenta_id, tipo, monto, *, mes=1, departamento_id=None, campo_id=None,
          unidad_negocio_id=None, origen_tipo=None, origen_id=None):
    """Registra un movimiento presupuestario."""
    m = models.MovimientoPresupuestario(
        fecha=dt.date(ANIO, mes, 15), tipo=tipo, anio=ANIO, mes=mes,
        cuenta_id=cuenta_id, monto=Decimal(str(monto)),
        departamento_id=departamento_id, campo_id=campo_id,
        unidad_negocio_id=unidad_negocio_id,
        origen_tipo=origen_tipo, origen_id=origen_id)
    db.add(m)
    db.commit()
    return m
