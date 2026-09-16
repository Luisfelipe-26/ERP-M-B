"""Orden de trabajo de punta a punta: crear con insumos y mano de obra, consumir, cerrar.

Es la operación central de la finca. Verifica que el consumo salga de inventario al
promedio ponderado, que la OT acumule sus costos, y que al cerrar el asiento refleje
exactamente esos costos y balancee.
"""
import calendar
import datetime as dt

import pytest

import models
import schemas
from conftest import ANIO
from routers.ordenes import create_orden, update_estado

HOY = dt.date.today()


@pytest.fixture
def finca(db, user):
    """Campo, actividad, trabajador, artículo con stock, reglas contables y período de hoy."""
    ctas = {}
    for cod, nom, nat in [("1.1.03", "Inventario", "deudora"), ("5.1.01", "Costo insumos", "deudora"),
                          ("5.1.02", "Costo mano de obra", "deudora"), ("2.1.05", "Nómina por pagar", "acreedora")]:
        c = models.CuentaContable(codigo=cod, nombre=nom, naturaleza=nat, tipo="costo")
        db.add(c)
        ctas[cod] = c
    db.flush()
    fin = dt.date(HOY.year, HOY.month, calendar.monthrange(HOY.year, HOY.month)[1])
    db.add_all([
        models.ReglaContabilizacion(evento="consumo_ot", concepto="salida_insumo", activo=True,
                                    cuenta_debe_id=ctas["5.1.01"].id, cuenta_haber_id=ctas["1.1.03"].id),
        models.ReglaContabilizacion(evento="nomina", concepto="salario_jornada", activo=True,
                                    cuenta_debe_id=ctas["5.1.02"].id, cuenta_haber_id=ctas["2.1.05"].id),
        models.PeriodoContable(anio=HOY.year, mes=HOY.month, nombre=f"{HOY:%b-%Y}".upper(), estado="abierto",
                               fecha_inicio=dt.date(HOY.year, HOY.month, 1), fecha_fin=fin),
        models.Campo(id_campo="C01", nombre="Lote Norte", area_ha=5),
        models.Actividad(id_act="A01", actividad="Fertilización"),
        models.Trabajador(id_trab="T01", nombre="Juan Pérez"),
        models.Producto(id_prod="P1", producto="Urea", unidad="kg", costo_promedio=150,
                        costo_unitario=150, stock_actual=100, es_inventariable=True, activo=True),
    ])
    db.commit()
    return ctas


def _ot(db, user, **extra):
    base = dict(
        campo_id="C01", actividad_id="A01", fecha_ejecucion=dt.datetime.combine(HOY, dt.time(8)),
        detalles=[schemas.OTDetalleCreate(producto_id="P1", cantidad_usada=20)],
        mano_obra=[schemas.OTManoObraCreate(trabajador_id="T01", horas_netas=8, costo_hora=100)],
    )
    base.update(extra)
    return create_orden(schemas.OrdenTrabajoCreate(**base), db=db, current_user=user)


def test_crear_ot_consume_inventario_al_promedio(db, user, finca):
    r = _ot(db, user)
    ot_id = r["ot_id"] if isinstance(r, dict) else r.ot_id

    prod = db.query(models.Producto).filter_by(id_prod="P1").one()
    assert float(prod.stock_actual) == 80, "100 - 20 consumidos"

    mov = db.query(models.MovimientoInventario).filter_by(producto_id="P1", tipo_doc="OT").one()
    assert float(mov.cantidad) == 20
    assert float(mov.costo_unitario) == 150, "sale al promedio ponderado del artículo"
    assert float(mov.stock_post) == 80
    assert mov.ot_referencia == ot_id

    orden = db.query(models.OrdenTrabajo).get(ot_id)
    assert float(orden.costo_insumos) == 3_000          # 20 x 150
    assert float(orden.costo_mano_obra) == 800          # 8 h x 100
    assert float(orden.costo_total) == 3_800


def test_no_permite_consumir_mas_del_stock(db, user, finca):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        _ot(db, user, detalles=[schemas.OTDetalleCreate(producto_id="P1", cantidad_usada=500)])
    assert e.value.status_code == 400
    assert "Stock insuficiente" in e.value.detail

    db.rollback()  # lo que hace get_db al cerrar la sesión de una petición fallida
    prod = db.query(models.Producto).filter_by(id_prod="P1").one()
    assert float(prod.stock_actual) == 100, "un rechazo no debe tocar el stock"
    assert db.query(models.OrdenTrabajo).count() == 0


def test_cerrar_ot_genera_asiento_que_cuadra_con_sus_costos(db, user, finca):
    r = _ot(db, user)
    ot_id = r["ot_id"] if isinstance(r, dict) else r.ot_id

    res = update_estado(ot_id, "Cerrada", hora_cierre=None, db=db, current_user=user)
    orden = db.query(models.OrdenTrabajo).get(ot_id)
    assert orden.estado == "Cerrada"
    assert res.get("asiento"), "cerrar la OT debe contabilizar sus costos"

    asiento = db.query(models.AsientoContable).filter_by(numero=res["asiento"]).one()
    assert float(asiento.total_debe) == pytest.approx(float(asiento.total_haber))
    assert float(asiento.total_debe) == pytest.approx(3_800), "insumos 3.000 + mano de obra 800"

    por_cuenta = {}
    for l in asiento.lineas:
        por_cuenta[l.cuenta_id] = por_cuenta.get(l.cuenta_id, 0) + float(l.debe or 0) - float(l.haber or 0)
    assert por_cuenta[finca["5.1.01"].id] == pytest.approx(3_000)
    assert por_cuenta[finca["1.1.03"].id] == pytest.approx(-3_000)
    assert por_cuenta[finca["5.1.02"].id] == pytest.approx(800)
    assert por_cuenta[finca["2.1.05"].id] == pytest.approx(-800)


def test_cerrar_dos_veces_no_duplica_el_asiento(db, user, finca):
    r = _ot(db, user)
    ot_id = r["ot_id"] if isinstance(r, dict) else r.ot_id

    update_estado(ot_id, "Cerrada", hora_cierre=None, db=db, current_user=user)
    update_estado(ot_id, "En Proceso", hora_cierre=None, db=db, current_user=user)
    update_estado(ot_id, "Cerrada", hora_cierre=None, db=db, current_user=user)

    asientos = db.query(models.AsientoContable).filter(
        models.AsientoContable.referencia_id == str(ot_id)).all()
    vigentes = [a for a in asientos if a.estado != "anulado"]
    assert len(vigentes) == 1, f"debe haber un solo asiento de cierre vigente, hay {len(vigentes)}"
