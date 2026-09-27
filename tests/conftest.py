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
def user(db):
    """Usuario real: el sistema de permisos recorre `roles`, que un stub no tiene."""
    return usuario(db, "admin")


def usuario(db, rol: str):
    u = models.Usuario(nombre=f"test-{rol}", email=f"{rol}@test.local",
                       hashed_password="x", rol=rol, activo=True)
    db.add(u)
    db.commit()
    return u


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


def periodo_abierto(db, d: dt.date):
    """Período contable abierto para el mes de `d` (si no existe ya)."""
    import calendar
    if not db.query(models.PeriodoContable).filter_by(anio=d.year, mes=d.month).first():
        fin = dt.date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])
        db.add(models.PeriodoContable(anio=d.year, mes=d.month, nombre=f"{d.month:02d}-{d.year}",
                                      estado="abierto", fecha_inicio=dt.date(d.year, d.month, 1),
                                      fecha_fin=fin))
        db.flush()


def cuentas_compra(db, cuenta_compra_id=None):
    """Cuentas y reglas del ciclo de compra con los códigos del catálogo de referencia.

    `cuenta_compra_id` sustituye la cuenta débito de la regla de compra (por defecto
    Inventario insumos). Devuelve las cuentas por código.
    """
    c = {}
    for cod, nom, nat, tipo in [
        ("1.1.03.01", "Inventario insumos", "deudora", "activo"),
        ("1.1.02.03", "ITBIS crédito fiscal", "deudora", "activo"),
        ("2.1.01.01", "CxP proveedores", "acreedora", "pasivo"),
        ("2.1.01.04", "Compras recibidas por facturar", "acreedora", "pasivo"),
        ("2.1.02.03", "Retenciones ISR por pagar", "acreedora", "pasivo"),
        ("2.1.02.04", "ITBIS retenido por pagar", "acreedora", "pasivo"),
    ]:
        c[cod] = db.query(models.CuentaContable).filter_by(codigo=cod).first() or \
            models.CuentaContable(codigo=cod, nombre=nom, naturaleza=nat, tipo=tipo)
        db.add(c[cod])
    db.flush()
    compra = cuenta_compra_id or c["1.1.03.01"].id
    for concepto, debe, haber in [("factura_proveedor", compra, c["2.1.01.01"].id),
                                  ("itbis_compra", c["1.1.02.03"].id, c["2.1.01.01"].id),
                                  ("recepcion_por_facturar", compra, c["2.1.01.04"].id)]:
        db.add(models.ReglaContabilizacion(evento="compra", concepto=concepto, activo=True,
                                           cuenta_debe_id=debe, cuenta_haber_id=haber))
    db.flush()
    return c


@pytest.fixture
def reglas_compra(db):
    """Lo mínimo para que una recepción contabilice: reglas de compra y período de hoy."""
    c = cuentas_compra(db)
    periodo_abierto(db, dt.date.today())
    db.commit()
    return {"inventario": c["1.1.03.01"], "cxp": c["2.1.01.01"], "puente": c["2.1.01.04"], **c}


def saldo_cuenta(db, cuenta) -> float:
    """Saldo deudor (debe - haber) de una cuenta en asientos no anulados."""
    total = 0.0
    for l in db.query(models.LineaAsiento).filter_by(cuenta_id=cuenta.id).all():
        if l.asiento.estado != "anulado":
            total += float(l.debe or 0) - float(l.haber or 0)
    return round(total, 2)


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
