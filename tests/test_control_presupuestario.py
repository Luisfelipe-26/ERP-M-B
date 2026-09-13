"""Bloqueo presupuestario: qué consume presupuesto, cuándo bloquea y contra qué ventana."""
import datetime as dt
import types
from decimal import Decimal

import models
from conftest import ANIO, mover, presupuestar
from routers.contabilidad import _monto_presupuestario, _verificar_presupuesto

ENERO = dt.date(ANIO, 1, 20)
FEBRERO = dt.date(ANIO, 2, 20)


def verificar(db, cuenta_id, monto, fecha=ENERO, **dims):
    return _verificar_presupuesto(
        db, [{"cuenta_id": cuenta_id, "debe": monto, "haber": 0, **dims}], fecha)


def test_alerta_al_superar_el_umbral(db, config, cuenta):
    presupuestar(db, cuenta.id, 100_000)
    mover(db, cuenta.id, "COMPROMISO", 30_000, origen_tipo="OC", origen_id="OC-1")

    assert verificar(db, cuenta.id, 50_000)["alertas"] == []          # 80%
    assert len(verificar(db, cuenta.id, 58_000)["alertas"]) == 1      # 88%


def test_bloquea_al_exceder_lo_apropiado(db, config, cuenta):
    presupuestar(db, cuenta.id, 100_000)
    mover(db, cuenta.id, "COMPROMISO", 30_000, origen_tipo="OC", origen_id="OC-1")

    r = verificar(db, cuenta.id, 80_000)
    assert r["bloqueado"] is True
    assert r["detalle"][0]["pct_proyectado"] == 110.0


def test_el_saldo_no_usado_se_arrastra_al_mes_siguiente(db, config, cuenta):
    """Control acumulado YTD: lo que sobra en enero sigue disponible en febrero."""
    presupuestar(db, cuenta.id, 100_000)
    mover(db, cuenta.id, "COMPROMISO", 30_000, origen_tipo="OC", origen_id="OC-1")

    assert verificar(db, cuenta.id, 80_000, ENERO)["bloqueado"] is True
    r = verificar(db, cuenta.id, 80_000, FEBRERO)
    assert r["detalle"][0]["apropiado_ytd"] == 200_000
    assert r["bloqueado"] is False


def test_un_asiento_contabilizado_no_consume_presupuesto(db, config, cuenta):
    """El consumo sale del ledger presupuestario, no del mayor.

    Medirlo contra LineaAsiento contaba la misma compra dos veces: al comprometerse
    y otra vez al contabilizarse el asiento de recepción.
    """
    presupuestar(db, cuenta.id, 100_000)
    mover(db, cuenta.id, "COMPROMISO", 30_000, origen_tipo="OC", origen_id="OC-1")

    periodo = models.PeriodoContable(anio=ANIO, mes=1, nombre="ENE", estado="abierto",
                                     fecha_inicio=dt.date(ANIO, 1, 1),
                                     fecha_fin=dt.date(ANIO, 1, 31))
    db.add(periodo)
    db.flush()
    asiento = models.AsientoContable(numero="AC-1", fecha=ENERO, periodo_id=periodo.id,
                                     estado="contabilizado", total_debe=90_000,
                                     total_haber=90_000)
    db.add(asiento)
    db.flush()
    db.add(models.LineaAsiento(asiento_id=asiento.id, cuenta_id=cuenta.id,
                               debe=90_000, haber=0))
    db.commit()

    r = verificar(db, cuenta.id, 50_000)
    assert r["detalle"][0]["consumido_ytd"] == 30_000
    assert r["bloqueado"] is False


def test_varias_lineas_a_la_misma_cuenta_pesan_juntas(db, config, cuenta):
    presupuestar(db, cuenta.id, 100_000)
    mover(db, cuenta.id, "COMPROMISO", 30_000, origen_tipo="OC", origen_id="OC-1")

    r = _verificar_presupuesto(db, [
        {"cuenta_id": cuenta.id, "debe": 40_000, "haber": 0},
        {"cuenta_id": cuenta.id, "debe": 40_000, "haber": 0},
    ], ENERO)

    assert len(r["detalle"]) == 1
    assert r["detalle"][0]["monto_solicitado"] == 80_000
    assert r["bloqueado"] is True


def test_roll_up_cuando_la_dimension_no_esta_presupuestada(db, config, cuenta):
    """Sin roll-up, un gasto con centro de costo no presupuestado pasaba sin control."""
    presupuestar(db, cuenta.id, 100_000)  # presupuesto a nivel de cuenta, sin depto

    r = verificar(db, cuenta.id, 120_000, departamento_id=7)
    assert r["detalle"][0]["nivel_control"] == "cuenta"
    assert r["bloqueado"] is True


def test_sin_presupuesto_no_bloquea(db, config, cuenta):
    """Una cuenta que nadie presupuestó no puede bloquear operaciones."""
    assert verificar(db, cuenta.id, 999_999)["bloqueado"] is False


def test_control_deshabilitado_no_bloquea(db, cuenta):
    db.add(models.ConfigPresupuesto(control_habilitado=False, umbral_bloqueo=100))
    presupuestar(db, cuenta.id, 1_000)
    db.commit()

    assert verificar(db, cuenta.id, 999_999)["bloqueado"] is False


def test_solo_el_itbis_no_recuperable_consume():
    formal = types.SimpleNamespace(tipo_contribuyente="formal")
    informal = types.SimpleNamespace(tipo_contribuyente="informal")

    assert _monto_presupuestario(100_000, 18_000, formal) == Decimal("100000")
    assert _monto_presupuestario(100_000, 18_000, informal) == Decimal("118000")


def test_escenario_alterno_no_infla_lo_disponible(db, config, cuenta):
    """Solo el escenario principal cuenta; antes se sumaban todos."""
    presupuestar(db, cuenta.id, 100_000)
    presupuestar(db, cuenta.id, 500_000, escenario="pesimista")

    assert verificar(db, cuenta.id, 150_000)["bloqueado"] is True
