import datetime as dt
import pytest
from fastapi import HTTPException
import models
import schemas
from routers.planificaciones import (
    crear_planificacion,
    listar_planificaciones,
    obtener_planificacion,
    actualizar_planificacion,
    reprogramar_planificacion,
    eliminar_planificacion,
    reporte_indicadores_cumplimiento,
    reporte_gantt,
)


def _setup_base(db):
    # Crear actividad
    act = models.Actividad(
        id_act="A-001",
        actividad="Poda de Formación",
        tarifa_jornada=150.0,
        unidad_rendimiento="ha"
    )
    db.add(act)

    # Crear campo
    c1 = models.Campo(
        id_campo="C-01",
        nombre="Lote Norte 1",
        area_ha=10.5,
        bloque="Norte"
    )
    db.add(c1)

    # Crear producto
    p1 = models.Producto(
        id_prod="P-001",
        producto="Fertilizante Foliar",
        unidad="Litros",
        costo_promedio=25.0
    )
    db.add(p1)
    db.commit()
    return act, c1, p1


def test_crear_planificacion_simple(db, user):
    act, c1, p1 = _setup_base(db)

    payload = schemas.PlanificacionLaborCreate(
        anio=2026,
        semana=15,
        fecha_inicio_estimada=dt.date(2026, 4, 10),
        fecha_fin_estimada=dt.date(2026, 4, 15),
        actividad_id="A-001",
        etapa_fenologica="Floración",
        prioridad="Alta",
        responsable_nombre="Carlos Ingeniero",
        horas_mo_estimadas=40.0,
        jornales_estimados=5.0,
        costo_mo_estimado=1500.0,
        costo_equipo_estimado=200.0,
        campos=[schemas.PlanificacionCampoCreate(campo_id="C-01", area_ha=10.5)],
        insumos=[
            schemas.PlanificacionInsumoCreate(
                producto_id="P-001",
                dosis_por_ha=2.0,
                cantidad_total=21.0,
                unidad="Litros",
                costo_unitario_estimado=25.0,
                costo_total_estimado=525.0
            )
        ]
    )

    res = crear_planificacion(data=payload, db=db, current_user=user)
    assert res["numero"].startswith("PLAN-")
    assert res["anio"] == 2026
    assert res["semana"] == 15
    assert res["actividad_id"] == "A-001"
    assert res["prioridad"] == "Alta"
    assert res["costo_mo_estimado"] == 1500.0
    assert res["costo_insumos_estimado"] == 525.0
    assert res["costo_total_estimado"] == 2225.0


def test_crear_planificacion_recurrente(db, user):
    act, c1, p1 = _setup_base(db)

    payload = schemas.PlanificacionLaborCreate(
        anio=2026,
        semana=10,
        actividad_id="A-001",
        es_recurrente=True,
        frecuencia_semanas=2,
        total_repeticiones=3,
        campos=[schemas.PlanificacionCampoCreate(campo_id="C-01", area_ha=10.5)],
    )

    res = crear_planificacion(data=payload, db=db, current_user=user)
    planes = db.query(models.PlanificacionLabor).order_by(models.PlanificacionLabor.semana.asc()).all()
    assert len(planes) == 3
    assert planes[0].semana == 10
    assert planes[1].semana == 12
    assert planes[2].semana == 14
    assert planes[0].grupo_recurrencia_id == planes[1].grupo_recurrencia_id
    assert planes[1].labor_previa_id == planes[0].id
    assert planes[2].labor_previa_id == planes[1].id


def test_reprogramar_planificacion(db, user):
    act, c1, p1 = _setup_base(db)

    payload = schemas.PlanificacionLaborCreate(
        anio=2026,
        semana=12,
        actividad_id="A-001",
        campos=[schemas.PlanificacionCampoCreate(campo_id="C-01", area_ha=10.5)],
    )
    plan_out = crear_planificacion(data=payload, db=db, current_user=user)

    reprog_payload = schemas.PlanificacionReprogramacionCreate(
        semana_nueva=14,
        anio_nuevo=2026,
        motivo="Lluvias intensas impidieron la entrada de maquinaria"
    )
    reprog_out = reprogramar_planificacion(plan_id=plan_out["id"], data=reprog_payload, db=db, current_user=user)

    assert reprog_out["semana"] == 14
    assert reprog_out["estado"] == "Reprogramada"
    assert reprog_out["veces_reprogramada"] == 1

    detalle = obtener_planificacion(plan_id=plan_out["id"], db=db, current_user=user)
    assert len(detalle["reprogramaciones"]) == 1
    assert detalle["reprogramaciones"][0]["semana_anterior"] == 12
    assert detalle["reprogramaciones"][0]["semana_nueva"] == 14


def test_indicadores_cumplimiento_y_gantt(db, user):
    act, c1, p1 = _setup_base(db)

    p1_data = schemas.PlanificacionLaborCreate(
        anio=2026,
        semana=20,
        actividad_id="A-001",
        costo_mo_estimado=1000.0,
        campos=[schemas.PlanificacionCampoCreate(campo_id="C-01", area_ha=10.5)],
    )
    crear_planificacion(data=p1_data, db=db, current_user=user)

    ind = reporte_indicadores_cumplimiento(anio=2026, db=db, current_user=user)
    assert ind["anio"] == 2026
    assert ind["total_planes"] == 1
    assert ind["pendientes"] == 1

    gantt = reporte_gantt(anio=2026, db=db, current_user=user)
    assert len(gantt["planes"]) == 1
    assert gantt["planes"][0]["semana"] == 20
