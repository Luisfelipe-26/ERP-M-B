"""La cuenta puente de compras se crea sola al arrancar, sin pisar un catálogo que divergió."""
import models
from reglas_contables import asegurar_cuenta_puente


def _cuenta(db, codigo, nombre, **kw):
    c = models.CuentaContable(codigo=codigo, nombre=nombre, naturaleza="acreedora", tipo="pasivo", **kw)
    db.add(c)
    db.flush()
    return c


def test_crea_la_cuenta_y_la_regla_y_no_repite(db):
    padre = _cuenta(db, "2.1.01", "Cuentas por Pagar", acepta_movimientos=False)
    cxp = _cuenta(db, "2.1.01.01", "CxP Proveedores", partida_id=None)

    assert asegurar_cuenta_puente(db, models) == "2.1.01.04"
    puente = db.query(models.CuentaContable).filter_by(codigo="2.1.01.04").one()
    assert puente.cuenta_padre_id == padre.id and puente.acepta_movimientos
    regla = db.query(models.ReglaContabilizacion).filter_by(evento="compra", concepto="recepcion_por_facturar").one()
    assert regla.cuenta_haber_id == puente.id

    assert asegurar_cuenta_puente(db, models) is None, "idempotente"
    assert db.query(models.ReglaContabilizacion).filter_by(concepto="recepcion_por_facturar").count() == 1


def test_si_el_codigo_esta_ocupado_usa_el_siguiente_libre(db):
    _cuenta(db, "2.1.01.01", "CxP Proveedores")
    _cuenta(db, "2.1.01.04", "Préstamos de socios")          # creada a mano en producción
    assert asegurar_cuenta_puente(db, models) == "2.1.01.05"
    assert db.query(models.CuentaContable).filter_by(codigo="2.1.01.04").one().nombre == "Préstamos de socios"


def test_reutiliza_una_cuenta_existente_con_ese_sentido(db):
    existente = _cuenta(db, "2.1.01.07", "Mercancía recibida pendiente de factura")
    assert asegurar_cuenta_puente(db, models) == "2.1.01.07"
    assert db.query(models.CuentaContable).count() == 1
    regla = db.query(models.ReglaContabilizacion).filter_by(concepto="recepcion_por_facturar").one()
    assert regla.cuenta_haber_id == existente.id
