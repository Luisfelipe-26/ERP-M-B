"""
Advanced Analytics — KPIs agrícolas para finca de aguacate Hass.
Endpoints de solo lectura que cruzan OTs, insumos, clima, riego y sanidad.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import func, extract, distinct, case, desc, asc, literal
from database import get_db
import models
import auth
from datetime import datetime, date, timedelta
from typing import Optional

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


# ── helpers ──────────────────────────────────────────────────────────────────

def _mes_ano_defaults():
    now = datetime.now()
    return now.month, now.year


def _parse_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%d").date()


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Productividad de trabajadores
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/productividad-trabajadores")
def productividad_trabajadores(
    mes: Optional[int] = None,
    ano: Optional[int] = None,
    db: Session = Depends(get_db),
    _=Depends(auth.get_current_user),
):
    if mes is None or ano is None:
        m, a = _mes_ano_defaults()
        mes = mes or m
        ano = ano or a

    # Base query: OTManoObra joined to OrdenTrabajo joined to Campo
    q = (
        db.query(
            models.OTManoObra.trabajador_id,
            models.Trabajador.nombre.label("nombre"),
            func.count(distinct(models.OTManoObra.ot_id)).label("total_jornadas"),
            func.coalesce(func.sum(models.OTManoObra.horas_netas), 0).label("total_horas"),
            func.coalesce(func.sum(models.OTManoObra.costo_mo), 0).label("total_ganado"),
            func.count(distinct(models.OrdenTrabajo.campo_id)).label("campos_trabajados"),
        )
        .join(models.Trabajador, models.OTManoObra.trabajador_id == models.Trabajador.id_trab)
        .join(models.OrdenTrabajo, models.OTManoObra.ot_id == models.OrdenTrabajo.ot_id)
        .filter(
            models.OrdenTrabajo.fecha_ejecucion.isnot(None),
            extract("month", models.OrdenTrabajo.fecha_ejecucion) == mes,
            extract("year", models.OrdenTrabajo.fecha_ejecucion) == ano,
        )
        .group_by(models.OTManoObra.trabajador_id, models.Trabajador.nombre)
    )

    rows = q.all()

    # Get total area by campo for cost_per_ha
    campos = {c.id_campo: c.area_ha or 1 for c in db.query(models.Campo).all()}

    results = []
    for r in rows:
        total_horas = float(r.total_horas or 0)
        total_ganado = float(r.total_ganado or 0)
        jornadas = int(r.total_jornadas or 0)

        # 8h jornada standard
        score = round(total_horas / (jornadas * 8), 2) if jornadas > 0 else 0
        if score >= 1.1:
            cat = "EXCELENTE"
        elif score >= 0.9:
            cat = "EN_META"
        elif score >= 0.7:
            cat = "BAJO_META"
        else:
            cat = "REVISAR"

        ha_per_day = round(total_horas / jornadas, 2) if jornadas > 0 else 0

        results.append({
            "trabajador_id": r.trabajador_id,
            "nombre": r.nombre,
            "total_jornadas": jornadas,
            "total_horas": round(total_horas, 2),
            "total_ganado": round(total_ganado, 2),
            "campos_trabajados": int(r.campos_trabajados or 0),
            "ha_per_worker_day": ha_per_day,
            "score": score,
            "performance_category": cat,
        })

    results.sort(key=lambda x: x["total_horas"], reverse=True)
    return {"mes": mes, "ano": ano, "trabajadores": results}


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Eficiencia de insumos
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/eficiencia-insumos")
def eficiencia_insumos(
    fecha_desde: Optional[str] = None,
    fecha_hasta: Optional[str] = None,
    db: Session = Depends(get_db),
    _=Depends(auth.get_current_user),
):
    now = datetime.now()
    if not fecha_desde:
        fecha_desde = (now - timedelta(days=90)).strftime("%Y-%m-%d")
    if not fecha_hasta:
        fecha_hasta = now.strftime("%Y-%m-%d")

    fd = _parse_date(fecha_desde)
    fh = _parse_date(fecha_hasta)

    # campos for ha lookup
    campos = {c.id_campo: c.area_ha or 1 for c in db.query(models.Campo).all()}

    q = (
        db.query(
            models.OTDetalle.producto_id,
            models.Producto.producto.label("nombre"),
            models.Producto.unidad,
            func.coalesce(func.sum(models.OTDetalle.cantidad_usada), 0).label("total_qty"),
            func.coalesce(func.sum(models.OTDetalle.costo_real), 0).label("total_cost"),
            func.count(distinct(models.OrdenTrabajo.campo_id)).label("campos_applied"),
        )
        .join(models.Producto, models.OTDetalle.producto_id == models.Producto.id_prod)
        .join(models.OrdenTrabajo, models.OTDetalle.ot_id == models.OrdenTrabajo.ot_id)
        .filter(
            models.OrdenTrabajo.fecha_ejecucion.isnot(None),
            func.cast(models.OrdenTrabajo.fecha_ejecucion, models.Column.__class__) >= fd if False else models.OrdenTrabajo.fecha_ejecucion >= datetime.combine(fd, datetime.min.time()),
            models.OrdenTrabajo.fecha_ejecucion <= datetime.combine(fh, datetime.max.time()),
        )
        .group_by(models.OTDetalle.producto_id, models.Producto.producto, models.Producto.unidad)
        .order_by(desc("total_cost"))
        .limit(30)
    )

    rows = q.all()
    # total ha across all active campos for avg
    total_ha = sum(campos.values())

    results = []
    for r in rows:
        tc = float(r.total_cost or 0)
        tq = float(r.total_qty or 0)
        cost_per_ha = round(tc / total_ha, 2) if total_ha > 0 else 0
        avg_dosis = round(tq / total_ha, 4) if total_ha > 0 else 0
        results.append({
            "producto_id": r.producto_id,
            "nombre": r.nombre,
            "unidad": r.unidad,
            "total_consumed_qty": round(tq, 2),
            "total_cost": round(tc, 2),
            "campos_applied": int(r.campos_applied or 0),
            "cost_per_ha": cost_per_ha,
            "avg_dosis_per_ha": avg_dosis,
        })

    return {
        "fecha_desde": fecha_desde,
        "fecha_hasta": fecha_hasta,
        "productos": results,
        "top10_by_cost": results[:10],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Costo por kg
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/costo-por-kg")
def costo_por_kg(
    temporada: Optional[int] = None,
    db: Session = Depends(get_db),
    _=Depends(auth.get_current_user),
):
    if temporada is None:
        temporada = datetime.now().year
    campos_list = db.query(models.Campo).filter(models.Campo.activo == True).all()
    industry_avg = 0.55  # USD/kg benchmark

    results = []
    for campo in campos_list:
        cid = campo.id_campo
        area = campo.area_ha or 1

        # Costs from OTs in temporada
        ot_costs = (
            db.query(
                func.coalesce(func.sum(models.OrdenTrabajo.costo_mano_obra), 0).label("labor"),
                func.coalesce(func.sum(models.OrdenTrabajo.costo_insumos), 0).label("inputs"),
                func.coalesce(func.sum(models.OrdenTrabajo.costo_equipo), 0).label("equipment"),
                func.count(models.OrdenTrabajo.id).label("num_ots"),
            )
            .filter(
                models.OrdenTrabajo.campo_id == cid,
                extract("year", models.OrdenTrabajo.fecha_ejecucion) == temporada,
            )
            .first()
        )

        labor = float(ot_costs.labor or 0)
        inputs = float(ot_costs.inputs or 0)
        equipment = float(ot_costs.equipment or 0)

        # Service costs from OCs
        oc_cost = (
            db.query(func.coalesce(func.sum(models.OrdenCompra.total_recibido), 0))
            .filter(
                models.OrdenCompra.campo_id == cid,
                extract("year", models.OrdenCompra.fecha) == temporada,
            )
            .scalar()
        )
        services = float(oc_cost or 0)

        total = labor + inputs + equipment + services

        # Estimated yield: n_plantas * 60kg avg per tree (Hass benchmark)
        n_plantas = campo.n_plantas or 0
        estimated_yield_kg = n_plantas * 60  # conservative Hass yield
        cost_per_kg = round(total / estimated_yield_kg, 4) if estimated_yield_kg > 0 else 0

        # Breakdown percentages
        if total > 0:
            breakdown = {
                "labor_pct": round(labor / total * 100, 1),
                "inputs_pct": round(inputs / total * 100, 1),
                "equipment_pct": round(equipment / total * 100, 1),
                "services_pct": round(services / total * 100, 1),
            }
        else:
            breakdown = {"labor_pct": 0, "inputs_pct": 0, "equipment_pct": 0, "services_pct": 0}

        results.append({
            "campo_id": cid,
            "nombre": campo.nombre,
            "area_ha": area,
            "total_cost": round(total, 2),
            "cost_per_ha": round(total / area, 2) if area > 0 else 0,
            "estimated_yield_kg": estimated_yield_kg,
            "cost_per_kg_estimate": cost_per_kg,
            "benchmark_usd_per_kg": industry_avg,
            "vs_benchmark": round(cost_per_kg - industry_avg, 4),
            "breakdown": breakdown,
            "num_ots": int(ot_costs.num_ots or 0),
        })

    results.sort(key=lambda x: x["cost_per_kg_estimate"])
    return {"temporada": temporada, "campos": results}


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Balance hídrico resumen
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/balance-hidrico-resumen")
def balance_hidrico_resumen(
    ano: Optional[int] = None,
    db: Session = Depends(get_db),
    _=Depends(auth.get_current_user),
):
    if ano is None:
        ano = datetime.now().year

    q = (
        db.query(
            models.DailyWaterBalance.campo_id,
            func.coalesce(func.sum(models.DailyWaterBalance.et0_mm), 0).label("total_et0"),
            func.coalesce(func.sum(models.DailyWaterBalance.etc_mm), 0).label("total_etc"),
            func.coalesce(func.sum(models.DailyWaterBalance.rainfall_mm), 0).label("total_rain"),
            func.coalesce(func.sum(models.DailyWaterBalance.irrigation_mm), 0).label("total_irrigation"),
            func.coalesce(func.sum(models.DailyWaterBalance.balance_mm), 0).label("net_balance"),
        )
        .filter(extract("year", models.DailyWaterBalance.date) == ano)
        .group_by(models.DailyWaterBalance.campo_id)
    )

    rows = q.all()
    campos = {c.id_campo: c.nombre for c in db.query(models.Campo).all()}

    results = []
    for r in rows:
        total_etc = float(r.total_etc or 0)
        total_irrigation = float(r.total_irrigation or 0)
        total_rain = float(r.total_rain or 0)
        net_balance = float(r.net_balance or 0)

        irrigation_ratio = round(total_irrigation / total_etc, 2) if total_etc > 0 else 0
        wue = round((total_rain + total_irrigation) / total_etc, 2) if total_etc > 0 else 0

        if net_balance >= 0:
            status = "adecuado"
        elif net_balance >= -20:
            status = "deficit_leve"
        else:
            status = "deficit_severo"

        results.append({
            "campo_id": r.campo_id,
            "nombre": campos.get(r.campo_id, r.campo_id),
            "total_et0": round(float(r.total_et0), 2),
            "total_etc": round(total_etc, 2),
            "total_rain": round(total_rain, 2),
            "total_irrigation": round(total_irrigation, 2),
            "net_balance": round(net_balance, 2),
            "irrigation_ratio": irrigation_ratio,
            "wue_estimate": wue,
            "status": status,
        })

    return {"campos": results}


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Presión de plagas
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/presion-plagas")
def presion_plagas(
    fecha_desde: Optional[str] = None,
    fecha_hasta: Optional[str] = None,
    db: Session = Depends(get_db),
    _=Depends(auth.get_current_user),
):
    now = datetime.now()
    if not fecha_desde:
        fecha_desde = (now - timedelta(days=90)).strftime("%Y-%m-%d")
    if not fecha_hasta:
        fecha_hasta = now.strftime("%Y-%m-%d")

    fd = datetime.combine(_parse_date(fecha_desde), datetime.min.time())
    fh = datetime.combine(_parse_date(fecha_hasta), datetime.max.time())

    campos_list = db.query(models.Campo).filter(models.Campo.activo == True).all()
    campos_names = {c.id_campo: c.nombre for c in campos_list}

    results = []
    for campo in campos_list:
        cid = campo.id_campo

        # Visits count
        visits = (
            db.query(func.count(models.ScoutingVisit.id))
            .filter(
                models.ScoutingVisit.campo_id == cid,
                models.ScoutingVisit.visit_date >= fd,
                models.ScoutingVisit.visit_date <= fh,
            )
            .scalar() or 0
        )

        # Pest observations
        visit_ids = [
            v.id for v in
            db.query(models.ScoutingVisit.id)
            .filter(
                models.ScoutingVisit.campo_id == cid,
                models.ScoutingVisit.visit_date >= fd,
                models.ScoutingVisit.visit_date <= fh,
            )
            .all()
        ]

        pests_detected = 0
        threshold_exceeded = 0
        avg_severity = 0

        if visit_ids:
            obs = db.query(models.PestObservation).filter(
                models.PestObservation.visit_id.in_(visit_ids)
            ).all()
            pests_detected = len(set(o.pest_id for o in obs))
            threshold_exceeded = sum(1 for o in obs if o.threshold_exceeded)
            severities = [o.severity_scale for o in obs if o.severity_scale is not None]
            avg_severity = round(sum(severities) / len(severities), 1) if severities else 0

        # Sprays
        spray_count = (
            db.query(func.count(models.SprayLog.id))
            .filter(
                models.SprayLog.campo_id == cid,
                models.SprayLog.application_date >= fd,
                models.SprayLog.application_date <= fh,
            )
            .scalar() or 0
        )

        last_spray = (
            db.query(models.SprayLog.application_date)
            .filter(models.SprayLog.campo_id == cid)
            .order_by(desc(models.SprayLog.application_date))
            .first()
        )

        # Risk level
        ratio = threshold_exceeded / visits if visits > 0 else 0
        if ratio >= 0.5:
            risk = "CRITICO"
        elif ratio >= 0.3:
            risk = "ALTO"
        elif ratio >= 0.1:
            risk = "MEDIO"
        else:
            risk = "BAJO"

        results.append({
            "campo_id": cid,
            "nombre": campos_names.get(cid, cid),
            "num_visits": visits,
            "pests_detected": pests_detected,
            "threshold_exceeded_count": threshold_exceeded,
            "avg_severity": avg_severity,
            "spray_count": spray_count,
            "last_spray_date": last_spray[0].isoformat() if last_spray and last_spray[0] else None,
            "risk_level": risk,
        })

    return {"fecha_desde": fecha_desde, "fecha_hasta": fecha_hasta, "campos": results}


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Comparativo de campos
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/comparativo-campos")
def comparativo_campos(
    ano: Optional[int] = None,
    db: Session = Depends(get_db),
    _=Depends(auth.get_current_user),
):
    campos_list = db.query(models.Campo).filter(models.Campo.activo == True).all()
    if ano is None:
        ano = datetime.now().year

    results = []
    for campo in campos_list:
        cid = campo.id_campo
        area = campo.area_ha or 1

        # OT costs
        ot = (
            db.query(
                func.coalesce(func.sum(models.OrdenTrabajo.costo_mano_obra), 0).label("labor"),
                func.coalesce(func.sum(models.OrdenTrabajo.costo_insumos), 0).label("inputs"),
                func.coalesce(func.sum(models.OrdenTrabajo.costo_equipo), 0).label("equip"),
                func.count(models.OrdenTrabajo.id).label("num_ots"),
            )
            .filter(
                models.OrdenTrabajo.campo_id == cid,
                extract("year", models.OrdenTrabajo.fecha_ejecucion) == ano,
            )
            .first()
        )

        labor = float(ot.labor or 0)
        inputs = float(ot.inputs or 0)
        equip = float(ot.equip or 0)
        num_ots = int(ot.num_ots or 0)

        # OC services
        oc_cost = (
            db.query(func.coalesce(func.sum(models.OrdenCompra.total_recibido), 0))
            .filter(
                models.OrdenCompra.campo_id == cid,
                extract("year", models.OrdenCompra.fecha) == ano,
            )
            .scalar()
        )
        services = float(oc_cost or 0)
        total = labor + inputs + equip + services
        cost_per_ha = round(total / area, 2) if area > 0 else 0

        # Soil moisture status — latest reading
        soil = (
            db.query(models.SoilReading)
            .filter(models.SoilReading.campo_id == cid)
            .order_by(desc(models.SoilReading.reading_datetime))
            .first()
        )
        soil_status = "sin_datos"
        if soil:
            v = soil.value_cbar
            if v is not None:
                if v < 10:
                    soil_status = "saturado"
                elif v < 25:
                    soil_status = "optimo"
                elif v < 40:
                    soil_status = "seco"
                else:
                    soil_status = "critico"

        # Pest pressure — last 90 days
        fd90 = datetime.now() - timedelta(days=90)
        visit_cnt = (
            db.query(func.count(models.ScoutingVisit.id))
            .filter(models.ScoutingVisit.campo_id == cid, models.ScoutingVisit.visit_date >= fd90)
            .scalar() or 0
        )

        threshold_cnt = 0
        if visit_cnt > 0:
            vids = [v.id for v in db.query(models.ScoutingVisit.id).filter(
                models.ScoutingVisit.campo_id == cid, models.ScoutingVisit.visit_date >= fd90
            ).all()]
            if vids:
                threshold_cnt = db.query(func.count(models.PestObservation.id)).filter(
                    models.PestObservation.visit_id.in_(vids),
                    models.PestObservation.threshold_exceeded == True,
                ).scalar() or 0

        pest_ratio = threshold_cnt / visit_cnt if visit_cnt > 0 else 0
        if pest_ratio >= 0.5:
            pest_pressure = "CRITICO"
        elif pest_ratio >= 0.3:
            pest_pressure = "ALTO"
        elif pest_ratio >= 0.1:
            pest_pressure = "MEDIO"
        else:
            pest_pressure = "BAJO"

        # Irrigation deficit
        wb = (
            db.query(func.coalesce(func.sum(models.DailyWaterBalance.balance_mm), 0))
            .filter(
                models.DailyWaterBalance.campo_id == cid,
                extract("year", models.DailyWaterBalance.date) == ano,
            )
            .scalar()
        )
        irrigation_deficit = round(float(wb or 0), 2)

        results.append({
            "campo_id": cid,
            "nombre": campo.nombre,
            "area_ha": area,
            "cost_per_ha": cost_per_ha,
            "num_ots": num_ots,
            "labor_cost": round(labor, 2),
            "input_cost": round(inputs, 2),
            "equipment_cost": round(equip, 2),
            "service_cost": round(services, 2),
            "total_cost": round(total, 2),
            "soil_moisture_status": soil_status,
            "pest_pressure": pest_pressure,
            "irrigation_deficit": irrigation_deficit,
        })

    results.sort(key=lambda x: x["cost_per_ha"])
    return {"ano": ano, "campos": results}


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Tendencia de costos
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/tendencia-costos")
def tendencia_costos(
    meses: int = 12,
    db: Session = Depends(get_db),
    _=Depends(auth.get_current_user),
):
    now = datetime.now()
    start = now - timedelta(days=meses * 30)

    q = (
        db.query(
            extract("month", models.OrdenTrabajo.fecha_ejecucion).label("mes"),
            extract("year", models.OrdenTrabajo.fecha_ejecucion).label("ano"),
            func.coalesce(func.sum(models.OrdenTrabajo.costo_mano_obra), 0).label("costo_mo"),
            func.coalesce(func.sum(models.OrdenTrabajo.costo_insumos), 0).label("costo_insumos"),
            func.coalesce(func.sum(models.OrdenTrabajo.costo_equipo), 0).label("costo_equipo"),
            func.coalesce(func.sum(models.OrdenTrabajo.costo_total), 0).label("costo_total"),
            func.count(models.OrdenTrabajo.id).label("num_ots"),
        )
        .filter(
            models.OrdenTrabajo.fecha_ejecucion.isnot(None),
            models.OrdenTrabajo.fecha_ejecucion >= start,
        )
        .group_by("ano", "mes")
        .order_by("ano", "mes")
    )

    rows = q.all()
    results = []
    for r in rows:
        results.append({
            "mes": int(r.mes),
            "ano": int(r.ano),
            "costo_mo": round(float(r.costo_mo), 2),
            "costo_insumos": round(float(r.costo_insumos), 2),
            "costo_equipo": round(float(r.costo_equipo), 2),
            "costo_total": round(float(r.costo_total), 2),
            "num_ots": int(r.num_ots),
        })

    return {"meses": meses, "tendencia": results}


# ═══════════════════════════════════════════════════════════════════════════════
# 8. Rendimiento por hectárea — horas/hombre y costo por actividad por campo
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/rendimiento-por-ha")
def rendimiento_por_ha(
    ano: Optional[int] = None,
    mes: Optional[int] = None,
    db: Session = Depends(get_db),
    _=Depends(auth.get_current_user),
):
    if ano is None:
        ano = datetime.now().year

    campos_map = {c.id_campo: {"nombre": c.nombre, "area_ha": c.area_ha or 1}
                  for c in db.query(models.Campo).filter(models.Campo.activo == True).all()}
    actividades_map = {a.id_act: a.actividad for a in db.query(models.Actividad).all()}

    ot_filters = [
        models.OrdenTrabajo.fecha_ejecucion.isnot(None),
        extract("year", models.OrdenTrabajo.fecha_ejecucion) == ano,
    ]
    if mes:
        ot_filters.append(extract("month", models.OrdenTrabajo.fecha_ejecucion) == mes)

    # Horas y costo de mano de obra agrupado por campo + actividad
    q = (
        db.query(
            models.OrdenTrabajo.campo_id,
            models.OrdenTrabajo.actividad_id,
            func.coalesce(func.sum(models.OTManoObra.horas_netas), 0).label("total_horas"),
            func.coalesce(func.sum(models.OTManoObra.costo_mo), 0).label("total_costo"),
            func.count(distinct(models.OTManoObra.trabajador_id)).label("num_trabajadores"),
            func.count(distinct(models.OrdenTrabajo.ot_id)).label("num_jornadas"),
        )
        .join(models.OTManoObra, models.OTManoObra.ot_id == models.OrdenTrabajo.ot_id)
        .filter(*ot_filters)
        .group_by(models.OrdenTrabajo.campo_id, models.OrdenTrabajo.actividad_id)
    )

    rows = []
    for r in q.all():
        area = campos_map.get(r.campo_id, {}).get("area_ha", 1)
        horas = float(r.total_horas or 0)
        costo = float(r.total_costo or 0)
        trabajadores = int(r.num_trabajadores or 0)
        jornadas = int(r.num_jornadas or 0)

        horas_ha = round(horas / area, 2) if area > 0 else 0
        costo_ha = round(costo / area, 2) if area > 0 else 0
        prom_horas_hombre = round(horas / trabajadores, 2) if trabajadores > 0 else 0

        rows.append({
            "campo_id": r.campo_id,
            "campo": campos_map.get(r.campo_id, {}).get("nombre", r.campo_id),
            "area_ha": area,
            "actividad_id": r.actividad_id,
            "actividad": actividades_map.get(r.actividad_id, r.actividad_id),
            "total_horas": round(horas, 2),
            "total_costo": round(costo, 2),
            "num_trabajadores": trabajadores,
            "num_jornadas": jornadas,
            "horas_ha": horas_ha,
            "costo_ha": costo_ha,
            "prom_horas_hombre": prom_horas_hombre,
        })

    rows.sort(key=lambda x: x["horas_ha"], reverse=True)

    # Insumos por campo + producto
    ins_filters = [
        models.OrdenTrabajo.fecha_ejecucion.isnot(None),
        extract("year", models.OrdenTrabajo.fecha_ejecucion) == ano,
    ]
    if mes:
        ins_filters.append(extract("month", models.OrdenTrabajo.fecha_ejecucion) == mes)

    q_ins = (
        db.query(
            models.OTDetalle.producto_id,
            models.Producto.producto.label("nombre_producto"),
            models.Producto.unidad,
            models.OrdenTrabajo.campo_id,
            func.coalesce(func.sum(models.OTDetalle.cantidad_usada), 0).label("qty"),
            func.coalesce(func.sum(models.OTDetalle.costo_real), 0).label("costo"),
            func.count(distinct(models.OTDetalle.ot_id)).label("num_apps"),
        )
        .join(models.Producto, models.OTDetalle.producto_id == models.Producto.id_prod)
        .join(models.OrdenTrabajo, models.OTDetalle.ot_id == models.OrdenTrabajo.ot_id)
        .filter(*ins_filters)
        .group_by(
            models.OrdenTrabajo.campo_id,
            models.OTDetalle.producto_id,
            models.Producto.producto,
            models.Producto.unidad,
        )
    )

    insumos_rows = []
    for r in q_ins.all():
        area = campos_map.get(r.campo_id, {}).get("area_ha", 1)
        costo = float(r.costo or 0)
        qty = float(r.qty or 0)
        insumos_rows.append({
            "campo_id": r.campo_id,
            "campo": campos_map.get(r.campo_id, {}).get("nombre", r.campo_id),
            "area_ha": area,
            "producto_id": r.producto_id,
            "producto": r.nombre_producto,
            "unidad": r.unidad,
            "cantidad_total": round(qty, 2),
            "costo_total": round(costo, 2),
            "cantidad_ha": round(qty / area, 4) if area > 0 else 0,
            "costo_ha": round(costo / area, 2) if area > 0 else 0,
            "num_aplicaciones": int(r.num_apps),
        })

    insumos_rows.sort(key=lambda x: x["costo_ha"], reverse=True)

    return {
        "ano": ano,
        "mes": mes,
        "rendimiento": rows,
        "insumos": insumos_rows,
    }
