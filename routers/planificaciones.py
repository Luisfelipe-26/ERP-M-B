"""Router de Planificación de Labores y Reportes Analíticos."""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func, or_, desc
from typing import List, Optional
from datetime import datetime, date
import uuid

from database import get_db
from auth import get_current_user
import models
import schemas
from routers.sequences import get_next, peek_next

router = APIRouter(prefix="/api/planificaciones", tags=["planificaciones"])


def _calculate_plan_metrics(plan: models.PlanificacionLabor, db: Session) -> dict:
    campos = plan.campos_plan or []
    campos_count = len(campos)
    campos_completados_count = sum(1 for c in campos if c.completado)
    
    ots = plan.ordenes or []
    ots_count = len(ots)
    costo_real_total = sum(ot.costo_total or 0 for ot in ots)
    
    porcentaje_avance = 0.0
    if campos_count > 0:
        porcentaje_avance = round((campos_completados_count / campos_count) * 100, 1)
    elif ots_count > 0:
        cerradas = sum(1 for ot in ots if ot.estado == "Cerrada")
        porcentaje_avance = round((cerradas / ots_count) * 100, 1)
    
    dias_atraso = 0
    hoy = date.today()
    if plan.fecha_fin_estimada and plan.estado not in ("Completa", "Cancelada"):
        if hoy > plan.fecha_fin_estimada:
            dias_atraso = (hoy - plan.fecha_fin_estimada).days

    campo_nombres = []
    for cp in campos:
        if cp.campo:
            campo_nombres.append(f"{cp.campo.id_campo} ({cp.campo.nombre or ''})")
        else:
            campo_nombres.append(cp.campo_id)
    campos_resumen = ", ".join(campo_nombres) if campo_nombres else "Sin lote asignado"

    labor_previa_completada = True
    if plan.labor_previa:
        labor_previa_completada = (plan.labor_previa.estado == "Completa")

    return {
        "campos_count": campos_count,
        "campos_completados_count": campos_completados_count,
        "porcentaje_avance": porcentaje_avance,
        "ots_count": ots_count,
        "campos_resumen": campos_resumen,
        "costo_real_total": costo_real_total,
        "dias_atraso": dias_atraso,
        "labor_previa_completada": labor_previa_completada,
    }


def _enrich_plan_out(plan: models.PlanificacionLabor, db: Session) -> dict:
    metrics = _calculate_plan_metrics(plan, db)
    act_nom = (plan.actividad_rel.actividad if (plan.actividad_rel and hasattr(plan.actividad_rel, 'actividad')) else (plan.actividad_rel.nombre if hasattr(plan.actividad_rel, 'nombre') else plan.actividad_id)) if plan.actividad_rel else plan.actividad_id
    prev_act = (plan.labor_previa.actividad_rel.actividad if hasattr(plan.labor_previa.actividad_rel, 'actividad') else plan.labor_previa.actividad_rel.nombre) if (plan.labor_previa and plan.labor_previa.actividad_rel) else None

    return {
        "id": plan.id,
        "numero": plan.numero,
        "anio": plan.anio,
        "semana": plan.semana,
        "fecha_inicio_estimada": plan.fecha_inicio_estimada,
        "fecha_fin_estimada": plan.fecha_fin_estimada,
        "actividad_id": plan.actividad_id,
        "actividad_nombre": act_nom,
        "etapa_fenologica": plan.etapa_fenologica,
        "prioridad": plan.prioridad or "Normal",
        "responsable_id": plan.responsable_id,
        "responsable_nombre": (plan.responsable_rel.nombre if plan.responsable_rel else plan.responsable_nombre),
        "presupuesto_id": plan.presupuesto_id,
        "presupuesto_nombre": plan.presupuesto_rel.nombre if plan.presupuesto_rel else None,
        "cuenta_id": plan.cuenta_id,
        "cuenta_codigo": plan.cuenta_rel.codigo if plan.cuenta_rel else None,
        "cuenta_nombre": plan.cuenta_rel.nombre if plan.cuenta_rel else None,
        "labor_previa_id": plan.labor_previa_id,
        "labor_previa_numero": plan.labor_previa.numero if plan.labor_previa else None,
        "labor_previa_actividad": prev_act,
        "labor_previa_estado": plan.labor_previa.estado if plan.labor_previa else None,
        "labor_previa_completada": metrics["labor_previa_completada"],
        "estado": plan.estado,
        "es_recurrente": plan.es_recurrente or False,
        "frecuencia_semanas": plan.frecuencia_semanas or 0,
        "grupo_recurrencia_id": plan.grupo_recurrencia_id,
        "repeticion_num": plan.repeticion_num or 1,
        "total_repeticiones": plan.total_repeticiones or 1,
        "reprogramada_de_id": plan.reprogramada_de_id,
        "motivo_reprogramacion": plan.motivo_reprogramacion,
        "veces_reprogramada": plan.veces_reprogramada or 0,
        "horas_mo_estimadas": plan.horas_mo_estimadas or 0.0,
        "jornales_estimados": plan.jornales_estimados or 0.0,
        "costo_mo_estimado": plan.costo_mo_estimado or 0.0,
        "costo_insumos_estimado": plan.costo_insumos_estimado or 0.0,
        "costo_equipo_estimado": plan.costo_equipo_estimado or 0.0,
        "costo_total_estimado": plan.costo_total_estimado or 0.0,
        "observaciones": plan.observaciones,
        "created_at": plan.created_at,
        "campos_count": metrics["campos_count"],
        "campos_completados_count": metrics["campos_completados_count"],
        "porcentaje_avance": metrics["porcentaje_avance"],
        "ots_count": metrics["ots_count"],
        "campos_resumen": metrics["campos_resumen"],
        "costo_real_total": metrics["costo_real_total"],
        "dias_atraso": metrics["dias_atraso"],
    }



@router.get("", response_model=List[schemas.PlanificacionLaborOut])
def listar_planificaciones(
    anio: Optional[int] = None,
    semana: Optional[int] = None,
    semana_desde: Optional[int] = None,
    semana_hasta: Optional[int] = None,
    campo_id: Optional[str] = None,
    actividad_id: Optional[str] = None,
    estado: Optional[str] = None,
    prioridad: Optional[str] = None,
    search: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user)
):
    query = db.query(models.PlanificacionLabor)

    if anio:
        query = query.filter(models.PlanificacionLabor.anio == anio)
    if semana:
        query = query.filter(models.PlanificacionLabor.semana == semana)
    if semana_desde:
        query = query.filter(models.PlanificacionLabor.semana >= semana_desde)
    if semana_hasta:
        query = query.filter(models.PlanificacionLabor.semana <= semana_hasta)
    if actividad_id:
        query = query.filter(models.PlanificacionLabor.actividad_id == actividad_id)
    if estado:
        query = query.filter(models.PlanificacionLabor.estado == estado)
    if prioridad:
        query = query.filter(models.PlanificacionLabor.prioridad == prioridad)
    if campo_id:
        query = query.join(models.PlanificacionCampo).filter(models.PlanificacionCampo.campo_id == campo_id)

    if search:
        s = f"%{search}%"
        query = query.filter(
            or_(
                models.PlanificacionLabor.numero.ilike(s),
                models.PlanificacionLabor.etapa_fenologica.ilike(s),
                models.PlanificacionLabor.responsable_nombre.ilike(s),
                models.PlanificacionLabor.observaciones.ilike(s),
            )
        )

    planes = query.order_by(models.PlanificacionLabor.anio.desc(), models.PlanificacionLabor.semana.desc(), models.PlanificacionLabor.id.desc()).all()
    
    hoy = date.today()
    current_year, current_week, _ = hoy.isocalendar()
    actualizado = False
    for p in planes:
        if p.estado == "Pendiente":
            if p.anio < current_year or (p.anio == current_year and p.semana < current_week):
                p.estado = "Vencida"
                actualizado = True
    if actualizado:
        db.commit()

    return [_enrich_plan_out(p, db) for p in planes]


@router.get("/{plan_id}", response_model=schemas.PlanificacionLaborDetailOut)
def obtener_planificacion(
    plan_id: int,
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user)
):
    plan = db.query(models.PlanificacionLabor).filter(models.PlanificacionLabor.id == plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Planificación no encontrada")

    res = _enrich_plan_out(plan, db)

    insumos_out = []
    for ins in (plan.insumos_plan or []):
        p_nombre = ins.producto.producto if (ins.producto and hasattr(ins.producto, 'producto')) else (ins.producto.nombre if hasattr(ins.producto, 'nombre') else None)
        p_unidad = ins.unidad or (ins.producto.unidad if (ins.producto and hasattr(ins.producto, 'unidad')) else None)
        insumos_out.append({
            "id": ins.id,
            "planificacion_id": ins.planificacion_id,
            "producto_id": ins.producto_id,
            "producto_nombre": p_nombre,
            "dosis_por_ha": ins.dosis_por_ha or 0,
            "cantidad_total": ins.cantidad_total or 0,
            "unidad": p_unidad,
            "costo_unitario_estimado": ins.costo_unitario_estimado or 0,
            "costo_total_estimado": ins.costo_total_estimado or 0,
            "observacion": ins.observacion,
        })

    campos_out = []
    for cp in (plan.campos_plan or []):
        campos_out.append({
            "id": cp.id,
            "planificacion_id": cp.planificacion_id,
            "campo_id": cp.campo_id,
            "area_ha": cp.area_ha or (cp.campo.area_ha if cp.campo else 0),
            "completado": cp.completado,
            "ot_id": cp.ot_id,
            "campo_nombre": cp.campo.nombre if cp.campo else None,
            "bloque": cp.campo.bloque if cp.campo else None,
        })

    reprog_out = []
    for rp in (plan.reprogramaciones or []):
        reprog_out.append({
            "id": rp.id,
            "planificacion_id": rp.planificacion_id,
            "semana_anterior": rp.semana_anterior,
            "anio_anterior": rp.anio_anterior,
            "semana_nueva": rp.semana_nueva,
            "anio_nuevo": rp.anio_nuevo,
            "motivo": rp.motivo,
            "usuario_id": rp.usuario_id,
            "usuario_nombre": rp.usuario_nombre,
            "fecha_cambio": rp.fecha_cambio,
        })

    ots_out = []
    costo_real_mo = 0.0
    costo_real_ins = 0.0
    costo_real_eq = 0.0
    for ot in (plan.ordenes or []):
        costo_real_mo += (ot.costo_mano_obra or 0)
        costo_real_ins += (ot.costo_insumos or 0)
        costo_real_eq += (ot.costo_equipo or 0)
        ot_act_nom = ot.actividad_rel.actividad if (ot.actividad_rel and hasattr(ot.actividad_rel, 'actividad')) else (ot.actividad_rel.nombre if hasattr(ot.actividad_rel, 'nombre') else None)
        ots_out.append({
            "id": ot.id,
            "ot_id": ot.ot_id,
            "fecha_ejecucion": ot.fecha_ejecucion,
            "hora_inicio": ot.hora_inicio,
            "campo_id": ot.campo_id,
            "actividad_id": ot.actividad_id,
            "actividad_nombre": ot_act_nom,
            "supervisor": ot.supervisor,
            "estado": ot.estado,
            "equipo": ot.equipo,
            "costo_insumos": ot.costo_insumos or 0,
            "costo_mano_obra": ot.costo_mano_obra or 0,
            "costo_equipo": ot.costo_equipo or 0,
            "costo_total": ot.costo_total or 0,
            "costo_ha": ot.costo_ha or 0,
            "horas_mo": ot.horas_mo or 0,
            "observaciones": ot.observaciones,
            "hora_cierre": ot.hora_cierre,
            "creado_en": ot.creado_en,
            "planificacion_id": plan.id,
            "planificacion_numero": plan.numero,
        })

    res["campos"] = campos_out
    res["insumos"] = insumos_out
    res["reprogramaciones"] = reprog_out
    res["ordenes"] = ots_out
    res["comparativa_costos"] = {
        "presupuestado": {
            "mano_obra": plan.costo_mo_estimado or 0,
            "insumos": plan.costo_insumos_estimado or 0,
            "equipo": plan.costo_equipo_estimado or 0,
            "total": plan.costo_total_estimado or 0,
        },
        "real": {
            "mano_obra": round(costo_real_mo, 2),
            "insumos": round(costo_real_ins, 2),
            "equipo": round(costo_real_eq, 2),
            "total": round(costo_real_mo + costo_real_ins + costo_real_eq, 2),
        },
        "desviacion_total": round((costo_real_mo + costo_real_ins + costo_real_eq) - (plan.costo_total_estimado or 0), 2)
    }

    return res


@router.post("", response_model=schemas.PlanificacionLaborOut)
def crear_planificacion(
    data: schemas.PlanificacionLaborCreate,
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user)
):
    actividad = db.query(models.Actividad).filter(models.Actividad.id_act == data.actividad_id).first()
    if not actividad:
        raise HTTPException(status_code=400, detail=f"Actividad {data.actividad_id} no existe")

    repeticiones = data.total_repeticiones if (data.es_recurrente and data.total_repeticiones > 1) else 1
    frecuencia = data.frecuencia_semanas if (data.es_recurrente and data.frecuencia_semanas > 0) else 1
    grupo_id = str(uuid.uuid4())[:8] if data.es_recurrente else None

    primer_plan = None
    ultimo_plan_creado_id = None

    for rep in range(repeticiones):
        semana_target = data.semana + (rep * frecuencia)
        anio_target = data.anio
        while semana_target > 52:
            semana_target -= 52
            anio_target += 1

        num = get_next("PLAN", db)

        c_mo = data.costo_mo_estimado or 0
        c_ins = data.costo_insumos_estimado or 0
        c_eq = data.costo_equipo_estimado or 0
        
        if data.insumos and c_ins == 0:
            c_ins = sum(i.costo_total_estimado or ((i.cantidad_total or 0) * (i.costo_unitario_estimado or 0)) for i in data.insumos)

        c_tot = data.costo_total_estimado or (c_mo + c_ins + c_eq)
        previa_id = data.labor_previa_id if rep == 0 else ultimo_plan_creado_id

        plan = models.PlanificacionLabor(
            numero=num,
            anio=anio_target,
            semana=semana_target,
            fecha_inicio_estimada=data.fecha_inicio_estimada,
            fecha_fin_estimada=data.fecha_fin_estimada,
            actividad_id=data.actividad_id,
            etapa_fenologica=data.etapa_fenologica,
            prioridad=data.prioridad or "Normal",
            responsable_id=data.responsable_id,
            responsable_nombre=data.responsable_nombre,
            presupuesto_id=data.presupuesto_id,
            cuenta_id=data.cuenta_id,
            labor_previa_id=previa_id,
            estado="Pendiente",
            es_recurrente=data.es_recurrente,
            frecuencia_semanas=data.frecuencia_semanas,
            grupo_recurrencia_id=grupo_id,
            repeticion_num=rep + 1,
            total_repeticiones=repeticiones,
            horas_mo_estimadas=data.horas_mo_estimadas or 0,
            jornales_estimados=data.jornales_estimados or 0,
            costo_mo_estimado=c_mo,
            costo_insumos_estimado=c_ins,
            costo_equipo_estimado=c_eq,
            costo_total_estimado=c_tot,
            observaciones=data.observaciones,
            usuario_id=current_user.id if current_user else None,
        )
        db.add(plan)
        db.flush()

        for c in data.campos:
            cp = models.PlanificacionCampo(
                planificacion_id=plan.id,
                campo_id=c.campo_id,
                area_ha=c.area_ha or 0,
                completado=False
            )
            db.add(cp)

        for ins in (data.insumos or []):
            ins_obj = models.PlanificacionInsumo(
                planificacion_id=plan.id,
                producto_id=ins.producto_id,
                dosis_por_ha=ins.dosis_por_ha or 0,
                cantidad_total=ins.cantidad_total or 0,
                unidad=ins.unidad,
                costo_unitario_estimado=ins.costo_unitario_estimado or 0,
                costo_total_estimado=ins.costo_total_estimado or ((ins.cantidad_total or 0) * (ins.costo_unitario_estimado or 0)),
                observacion=ins.observacion,
            )
            db.add(ins_obj)

        if primer_plan is None:
            primer_plan = plan
        ultimo_plan_creado_id = plan.id

    db.commit()
    db.refresh(primer_plan)
    return _enrich_plan_out(primer_plan, db)

@router.put("/{plan_id}", response_model=schemas.PlanificacionLaborOut)
def actualizar_planificacion(
    plan_id: int,
    data: schemas.PlanificacionLaborUpdate,
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user)
):
    plan = db.query(models.PlanificacionLabor).filter(models.PlanificacionLabor.id == plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Planificación no encontrada")

    update_dict = data.dict(exclude_unset=True, exclude={"campos", "insumos"})
    for k, v in update_dict.items():
        setattr(plan, k, v)

    if data.campos is not None:
        db.query(models.PlanificacionCampo).filter(models.PlanificacionCampo.planificacion_id == plan.id).delete()
        for c in data.campos:
            db.add(models.PlanificacionCampo(
                planificacion_id=plan.id,
                campo_id=c.campo_id,
                area_ha=c.area_ha or 0,
                completado=False
            ))

    if data.insumos is not None:
        db.query(models.PlanificacionInsumo).filter(models.PlanificacionInsumo.planificacion_id == plan.id).delete()
        for ins in data.insumos:
            db.add(models.PlanificacionInsumo(
                planificacion_id=plan.id,
                producto_id=ins.producto_id,
                dosis_por_ha=ins.dosis_por_ha or 0,
                cantidad_total=ins.cantidad_total or 0,
                unidad=ins.unidad,
                costo_unitario_estimado=ins.costo_unitario_estimado or 0,
                costo_total_estimado=ins.costo_total_estimado or ((ins.cantidad_total or 0) * (ins.costo_unitario_estimado or 0)),
                observacion=ins.observacion,
            ))

    db.commit()
    db.refresh(plan)
    return _enrich_plan_out(plan, db)


@router.post("/{plan_id}/reprogramar", response_model=schemas.PlanificacionLaborOut)
def reprogramar_planificacion(
    plan_id: int,
    data: schemas.PlanificacionReprogramacionCreate,
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user)
):
    plan = db.query(models.PlanificacionLabor).filter(models.PlanificacionLabor.id == plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Planificación no encontrada")

    reprog = models.PlanificacionReprogramacion(
        planificacion_id=plan.id,
        semana_anterior=plan.semana,
        anio_anterior=plan.anio,
        semana_nueva=data.semana_nueva,
        anio_nuevo=data.anio_nuevo,
        motivo=data.motivo,
        usuario_id=current_user.id if current_user else None,
        usuario_nombre=current_user.nombre if current_user else "Usuario",
    )
    db.add(reprog)

    plan.semana = data.semana_nueva
    plan.anio = data.anio_nuevo
    plan.motivo_reprogramacion = data.motivo
    plan.veces_reprogramada = (plan.veces_reprogramada or 0) + 1
    plan.estado = "Reprogramada"

    db.commit()
    db.refresh(plan)
    return _enrich_plan_out(plan, db)


@router.delete("/{plan_id}")
def eliminar_planificacion(
    plan_id: int,
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user)
):
    plan = db.query(models.PlanificacionLabor).filter(models.PlanificacionLabor.id == plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Planificación no encontrada")

    if plan.ordenes and len(plan.ordenes) > 0:
        raise HTTPException(status_code=400, detail="No se puede eliminar una planificación con Órdenes de Trabajo vinculadas. Cancele la planificación en su lugar.")

    db.delete(plan)
    db.commit()
    return {"ok": True, "message": "Planificación eliminada exitosamente"}
@router.get("/reportes/indicadores-cumplimiento")
def reporte_indicadores_cumplimiento(
    anio: Optional[int] = None,
    semana_desde: Optional[int] = None,
    semana_hasta: Optional[int] = None,
    campo_id: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user)
):
    if not anio:
        anio = date.today().year

    query = db.query(models.PlanificacionLabor).filter(models.PlanificacionLabor.anio == anio)
    if semana_desde:
        query = query.filter(models.PlanificacionLabor.semana >= semana_desde)
    if semana_hasta:
        query = query.filter(models.PlanificacionLabor.semana <= semana_hasta)
    if campo_id:
        query = query.join(models.PlanificacionCampo).filter(models.PlanificacionCampo.campo_id == campo_id)

    planes = query.all()
    total_planes = len(planes)
    
    pendientes = sum(1 for p in planes if p.estado == "Pendiente")
    parciales = sum(1 for p in planes if p.estado == "Parcial")
    completas = sum(1 for p in planes if p.estado == "Completa")
    vencidas = sum(1 for p in planes if p.estado == "Vencida")
    reprogramadas = sum(1 for p in planes if p.estado == "Reprogramada")
    canceladas = sum(1 for p in planes if p.estado == "Cancelada")

    costo_estimado_total = sum(p.costo_total_estimado or 0 for p in planes)
    costo_real_total = 0.0
    jornales_estimados_total = sum(p.jornales_estimados or 0 for p in planes)

    total_lotes_planificados = sum(len(p.campos_plan or []) for p in planes)
    lotes_completados = sum(sum(1 for c in (p.campos_plan or []) if c.completado) for p in planes)

    for p in planes:
        for ot in (p.ordenes or []):
            costo_real_total += (ot.costo_total or 0)

    divisor = total_planes - canceladas
    porcentaje_cumplimiento = 0.0
    if divisor > 0:
        porcentaje_cumplimiento = round(((completas + (parciales * 0.5)) / divisor) * 100, 1)

    porcentaje_cobertura_lotes = 0.0
    if total_lotes_planificados > 0:
        porcentaje_cobertura_lotes = round((lotes_completados / total_lotes_planificados) * 100, 1)

    semanas_dict = {}
    for p in planes:
        sem = p.semana
        if sem not in semanas_dict:
            semanas_dict[sem] = {
                "semana": sem,
                "total": 0,
                "completadas": 0,
                "reprogramadas": 0,
                "vencidas": 0,
                "costo_plan": 0.0,
                "costo_real": 0.0
            }
        semanas_dict[sem]["total"] += 1
        if p.estado == "Completa":
            semanas_dict[sem]["completadas"] += 1
        elif p.estado == "Reprogramada":
            semanas_dict[sem]["reprogramadas"] += 1
        elif p.estado == "Vencida":
            semanas_dict[sem]["vencidas"] += 1
        
        semanas_dict[sem]["costo_plan"] += (p.costo_total_estimado or 0)
        for ot in (p.ordenes or []):
            semanas_dict[sem]["costo_real"] += (ot.costo_total or 0)

    tendencia_semanal = sorted(list(semanas_dict.values()), key=lambda x: x["semana"])

    return {
        "anio": anio,
        "total_planes": total_planes,
        "pendientes": pendientes,
        "parciales": parciales,
        "completas": completas,
        "vencidas": vencidas,
        "reprogramadas": reprogramadas,
        "canceladas": canceladas,
        "porcentaje_cumplimiento": porcentaje_cumplimiento,
        "total_lotes_planificados": total_lotes_planificados,
        "lotes_completados": lotes_completados,
        "porcentaje_cobertura_lotes": porcentaje_cobertura_lotes,
        "costo_estimado_total": round(costo_estimado_total, 2),
        "costo_real_total": round(costo_real_total, 2),
        "variacion_costo": round(costo_real_total - costo_estimado_total, 2),
        "jornales_estimados_total": jornales_estimados_total,
        "tendencia_semanal": tendencia_semanal,
    }


@router.get("/reportes/gantt-actividades")
def reporte_gantt(
    anio: Optional[int] = None,
    campo_id: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user)
):
    if not anio:
        anio = date.today().year

    query = db.query(models.PlanificacionLabor).filter(models.PlanificacionLabor.anio == anio)
    if campo_id:
        query = query.join(models.PlanificacionCampo).filter(models.PlanificacionCampo.campo_id == campo_id)

    planes = query.order_by(models.PlanificacionLabor.semana.asc()).all()

    items = []
    for p in planes:
        metrics = _calculate_plan_metrics(p, db)
        act_nom = (p.actividad_rel.actividad if hasattr(p.actividad_rel, 'actividad') else p.actividad_rel.nombre) if p.actividad_rel else p.actividad_id
        items.append({
            "id": p.id,
            "numero": p.numero,
            "semana": p.semana,
            "anio": p.anio,
            "actividad_id": p.actividad_id,
            "actividad_nombre": act_nom,
            "prioridad": p.prioridad,
            "estado": p.estado,
            "campos": [c.campo_id for c in (p.campos_plan or [])],
            "campos_resumen": metrics["campos_resumen"],
            "avance": metrics["porcentaje_avance"],
            "labor_previa_id": p.labor_previa_id,
            "labor_previa_numero": p.labor_previa.numero if p.labor_previa else None,
            "responsable": p.responsable_rel.nombre if p.responsable_rel else p.responsable_nombre,
            "costo_estimado": p.costo_total_estimado or 0,
            "costo_real": metrics["costo_real_total"],
        })

    return {"anio": anio, "planes": items}


