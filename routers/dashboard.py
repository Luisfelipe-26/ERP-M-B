from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import func, extract, desc, case, and_
from database import get_db
import models, schemas, auth
from datetime import datetime, timedelta
from typing import Optional
import csv, io
from fastapi.responses import StreamingResponse

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


def _filter_mes_ano(q, col, mes, ano):
    """PostgreSQL-compatible month/year filter."""
    return q.filter(
        col.isnot(None),
        extract('month', col) == mes,
        extract('year', col) == ano
    )


@router.get("/stats", response_model=schemas.DashboardStats)
def get_stats(db: Session = Depends(get_db), _=Depends(auth.get_current_user)):
    now = datetime.now()
    mes, ano = now.month, now.year

    total_campos = db.query(models.Campo).filter(models.Campo.activo == True).count()
    total_trabajadores = db.query(models.Trabajador).filter(models.Trabajador.activo == True).count()
    total_productos = db.query(models.Producto).filter(models.Producto.activo == True).count()
    ordenes_abiertas = db.query(models.OrdenTrabajo).filter(
        models.OrdenTrabajo.estado.in_(["Abierta", "En Proceso"])
    ).count()

    ordenes_mes = _filter_mes_ano(
        db.query(models.OrdenTrabajo),
        models.OrdenTrabajo.fecha_ejecucion, mes, ano
    ).count()

    costos_mes = _filter_mes_ano(
        db.query(
            func.coalesce(func.sum(models.OrdenTrabajo.costo_mano_obra), 0),
            func.coalesce(func.sum(models.OrdenTrabajo.costo_insumos), 0),
            func.coalesce(func.sum(models.OrdenTrabajo.costo_total), 0),
        ),
        models.OrdenTrabajo.fecha_ejecucion, mes, ano
    ).first()

    productos_bajo_stock = db.query(models.Producto).filter(
        models.Producto.activo == True,
        models.Producto.stock_actual <= models.Producto.stock_minimo,
        models.Producto.stock_minimo > 0
    ).count()

    return schemas.DashboardStats(
        total_campos=total_campos,
        total_trabajadores=total_trabajadores,
        total_productos=total_productos,
        ordenes_abiertas=ordenes_abiertas,
        ordenes_mes=ordenes_mes,
        costo_mo_mes=float(costos_mes[0] or 0),
        costo_insumos_mes=float(costos_mes[1] or 0),
        costo_total_mes=float(costos_mes[2] or 0),
        productos_bajo_stock=productos_bajo_stock,
    )


@router.get("/costos-por-campo")
def costos_por_campo(
    mes: int = Query(None), ano: int = Query(None),
    db: Session = Depends(get_db), _=Depends(auth.get_current_user)
):
    # Costos de OTs por campo
    q_ot = db.query(
        models.OrdenTrabajo.campo_id,
        func.coalesce(func.sum(models.OrdenTrabajo.costo_total), 0).label("costo_total"),
        func.count(models.OrdenTrabajo.id).label("num_ordenes")
    )
    if mes and ano:
        q_ot = _filter_mes_ano(q_ot, models.OrdenTrabajo.fecha_ejecucion, mes, ano)
    rows_ot = q_ot.group_by(models.OrdenTrabajo.campo_id).all()

    # Costos de OCs asignadas a campo (servicios no inventariables)
    q_oc = db.query(
        models.OrdenCompra.campo_id,
        func.coalesce(func.sum(models.OrdenCompra.total_estimado), 0).label("costo_total"),
        func.count(models.OrdenCompra.id).label("num_ocs")
    ).filter(
        models.OrdenCompra.campo_id.isnot(None),
        models.OrdenCompra.estado != "Cancelada"
    )
    if mes and ano:
        q_oc = _filter_mes_ano(q_oc, models.OrdenCompra.fecha, mes, ano)
    rows_oc = q_oc.group_by(models.OrdenCompra.campo_id).all()

    # Merge
    costos = {}
    for r in rows_ot:
        costos[r[0]] = {"campo_id": r[0], "costo_ot": float(r[1]), "num_ordenes": r[2], "costo_oc": 0, "num_ocs": 0}
    for r in rows_oc:
        if r[0] in costos:
            costos[r[0]]["costo_oc"] = float(r[1])
            costos[r[0]]["num_ocs"] = r[2]
        else:
            costos[r[0]] = {"campo_id": r[0], "costo_ot": 0, "num_ordenes": 0, "costo_oc": float(r[1]), "num_ocs": r[2]}
    for c in costos.values():
        c["costo_total"] = c["costo_ot"] + c["costo_oc"]
    return list(costos.values())


@router.get("/costos-por-actividad")
def costos_por_actividad(
    mes: int = Query(None), ano: int = Query(None),
    db: Session = Depends(get_db), _=Depends(auth.get_current_user)
):
    q = db.query(
        models.OrdenTrabajo.actividad_id,
        models.Actividad.actividad,
        func.coalesce(func.sum(models.OrdenTrabajo.costo_total), 0).label("costo_total"),
        func.count(models.OrdenTrabajo.id).label("num_ordenes")
    ).join(models.Actividad, models.OrdenTrabajo.actividad_id == models.Actividad.id_act, isouter=True)
    if mes and ano:
        q = _filter_mes_ano(q, models.OrdenTrabajo.fecha_ejecucion, mes, ano)
    rows = q.group_by(models.OrdenTrabajo.actividad_id, models.Actividad.actividad).all()
    return [{"actividad_id": r[0], "actividad": r[1], "costo_total": float(r[2]), "num_ordenes": r[3]} for r in rows]


@router.get("/nomina-mensual")
def nomina_mensual(
    mes: int = Query(None), ano: int = Query(None),
    db: Session = Depends(get_db), _=Depends(auth.get_current_user)
):
    now = datetime.now()
    filtro_mes = mes or now.month
    filtro_ano = ano or now.year

    rows = db.query(
        models.Trabajador.id_trab,
        models.Trabajador.nombre,
        models.Trabajador.cargo,
        func.coalesce(func.sum(models.OTManoObra.costo_mo), 0).label("total_ganado"),
        func.count(models.OTManoObra.id).label("num_jornadas")
    ).outerjoin(
        models.OTManoObra,
        models.Trabajador.id_trab == models.OTManoObra.trabajador_id
    ).filter(
        models.Trabajador.activo == True
    ).filter(
        (models.OTManoObra.id.is_(None)) |
        (
            models.OTManoObra.fecha.isnot(None) &
            (extract('month', models.OTManoObra.fecha) == filtro_mes) &
            (extract('year', models.OTManoObra.fecha) == filtro_ano)
        )
    ).group_by(
        models.Trabajador.id_trab, models.Trabajador.nombre, models.Trabajador.cargo
    ).all()

    return [
        {"id_trab": r[0], "nombre": r[1], "cargo": r[2],
         "total_ganado": float(r[3]), "num_jornadas": int(r[4])}
        for r in rows
    ]


@router.get("/export/nomina-csv")
def export_nomina_csv(
    mes: int = Query(...), ano: int = Query(...),
    db: Session = Depends(get_db), _=Depends(auth.get_current_user)
):
    rows = db.query(
        models.Trabajador.id_trab,
        models.Trabajador.nombre,
        models.Trabajador.cargo,
        models.Trabajador.costo_hora,
        func.coalesce(func.sum(models.OTManoObra.costo_mo), 0).label("total_ganado"),
        func.count(models.OTManoObra.id).label("num_jornadas"),
        func.coalesce(func.sum(models.OTManoObra.horas_netas), 0).label("total_horas"),
    ).outerjoin(
        models.OTManoObra,
        models.Trabajador.id_trab == models.OTManoObra.trabajador_id
    ).filter(
        models.Trabajador.activo == True
    ).filter(
        (models.OTManoObra.id.is_(None)) |
        (
            models.OTManoObra.fecha.isnot(None) &
            (extract('month', models.OTManoObra.fecha) == mes) &
            (extract('year', models.OTManoObra.fecha) == ano)
        )
    ).group_by(
        models.Trabajador.id_trab, models.Trabajador.nombre,
        models.Trabajador.cargo, models.Trabajador.costo_hora
    ).all()

    output = io.StringIO()
    output.write('\ufeff')  # BOM UTF-8 for Excel español
    writer = csv.writer(output)
    writer.writerow(["ID", "Nombre", "Cargo", "Costo/Hora", "Total Horas", "Jornadas", "Total Ganado (RD$)"])
    total = 0
    for r in rows:
        writer.writerow([r[0], r[1], r[2], r[3], round(r[6], 1), r[5], round(r[4], 2)])
        total += r[4]
    writer.writerow([])
    writer.writerow(["", "", "", "", "", "TOTAL", round(total, 2)])

    output.seek(0)
    filename = f"nomina_{mes:02d}_{ano}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


@router.get("/export/ordenes-csv")
def export_ordenes_csv(
    mes: int = Query(None), ano: int = Query(None),
    campo_id: str = Query(None),
    db: Session = Depends(get_db), _=Depends(auth.get_current_user)
):
    q = db.query(models.OrdenTrabajo)
    if mes and ano:
        q = _filter_mes_ano(q, models.OrdenTrabajo.fecha_ejecucion, mes, ano)
    if campo_id:
        q = q.filter(models.OrdenTrabajo.campo_id == campo_id)
    ordenes = q.order_by(models.OrdenTrabajo.fecha_ejecucion).all()

    output = io.StringIO()
    output.write('\ufeff')  # BOM UTF-8 for Excel español
    writer = csv.writer(output)
    # C-6 FIX: Incluir costo_equipo en CSV
    writer.writerow(["OT #", "Fecha", "Campo", "Actividad", "Supervisor", "Estado",
                     "Costo MO", "Costo Insumos", "Costo Equipo", "Total", "Costo/ha"])
    for o in ordenes:
        writer.writerow([
            o.ot_id,
            o.fecha_ejecucion.strftime('%Y-%m-%d') if o.fecha_ejecucion else '',
            o.campo_id, o.actividad_id, o.supervisor or '', o.estado,
            round(o.costo_mano_obra or 0, 2), round(o.costo_insumos or 0, 2),
            round(o.costo_equipo or 0, 2),
            round(o.costo_total or 0, 2), round(o.costo_ha or 0, 2)
        ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="ordenes.csv"'}
    )


@router.get("/briefing")
def morning_briefing(db: Session = Depends(get_db), _=Depends(auth.get_current_user)):
    """Morning briefing: weather, pest alerts, irrigation, OTs, OCs, financial summary."""
    now = datetime.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    mes, ano = now.month, now.year
    seven_days_ago = now - timedelta(days=7)

    # --- Weather panel ---
    latest_weather = db.query(models.WeatherReading).order_by(
        desc(models.WeatherReading.recorded_at)
    ).first()

    weather_summary = db.query(models.WeatherDailySummary).order_by(
        desc(models.WeatherDailySummary.date)
    ).first()

    weather = None
    if latest_weather:
        weather = {
            "temperature_c": latest_weather.temperature_c,
            "humidity_pct": latest_weather.humidity_pct,
            "rainfall_mm": latest_weather.rainfall_mm or 0,
            "wind_speed_kmh": latest_weather.wind_speed_kmh,
            "recorded_at": latest_weather.recorded_at.isoformat() if latest_weather.recorded_at else None,
        }
    if weather_summary:
        weather = weather or {}
        weather["daily_rain_mm"] = weather_summary.rainfall_total_mm or 0
        weather["temp_min"] = weather_summary.temp_min
        weather["temp_max"] = weather_summary.temp_max
        weather["et0_mm"] = weather_summary.et0_mm
        weather["anthracnose_risk"] = weather_summary.anthracnose_risk
        weather["phytophthora_risk"] = weather_summary.phytophthora_risk

    # --- Active weather alerts ---
    active_weather_alerts = db.query(models.WeatherAlert).filter(
        models.WeatherAlert.resolved_at.is_(None)
    ).count()

    # --- Pest panel ---
    recent_visits = db.query(models.ScoutingVisit).filter(
        models.ScoutingVisit.visit_date >= seven_days_ago
    ).count()

    threshold_exceeded_count = db.query(models.PestObservation).filter(
        models.PestObservation.threshold_exceeded == True
    ).join(models.ScoutingVisit).filter(
        models.ScoutingVisit.visit_date >= seven_days_ago
    ).count()

    active_traps = db.query(models.Trap).filter(
        models.Trap.status == "activa"
    ).count()

    pest = {
        "recent_visits_7d": recent_visits,
        "alerts_threshold": threshold_exceeded_count,
        "active_traps": active_traps,
    }

    # --- Irrigation panel ---
    today_irrigation = db.query(
        func.count(models.IrrigationEvent.id),
        func.coalesce(func.sum(models.IrrigationEvent.volume_mm), 0),
    ).filter(
        models.IrrigationEvent.start_time >= today_start
    ).first()

    deficit_count = db.query(models.DailyWaterBalance).filter(
        models.DailyWaterBalance.status == "deficit"
    ).filter(
        models.DailyWaterBalance.date >= today_start - timedelta(days=1)
    ).count()

    irrigation = {
        "events_today": today_irrigation[0] if today_irrigation else 0,
        "volume_mm_today": float(today_irrigation[1]) if today_irrigation else 0,
        "campos_deficit": deficit_count,
    }

    # --- OTs today ---
    ots_today = db.query(models.OrdenTrabajo).filter(
        models.OrdenTrabajo.fecha_ejecucion >= today_start,
        models.OrdenTrabajo.fecha_ejecucion < today_start + timedelta(days=1),
    ).count()

    ots_open = db.query(models.OrdenTrabajo).filter(
        models.OrdenTrabajo.estado.in_(["Abierta", "En Proceso"])
    ).count()

    # --- OCs pending ---
    ocs_pending = db.query(models.OrdenCompra).filter(
        models.OrdenCompra.estado.in_(["Pendiente", "Parcial"])
    ).count()

    ocs_pending_total = db.query(
        func.coalesce(func.sum(models.OrdenCompra.total_estimado), 0)
    ).filter(
        models.OrdenCompra.estado.in_(["Pendiente", "Parcial"])
    ).scalar()

    # --- Financial summary (current month) ---
    costos_mes = db.query(
        func.coalesce(func.sum(models.OrdenTrabajo.costo_total), 0),
        func.coalesce(func.sum(models.OrdenTrabajo.costo_mano_obra), 0),
        func.coalesce(func.sum(models.OrdenTrabajo.costo_insumos), 0),
    ).filter(
        models.OrdenTrabajo.fecha_ejecucion.isnot(None),
        extract('month', models.OrdenTrabajo.fecha_ejecucion) == mes,
        extract('year', models.OrdenTrabajo.fecha_ejecucion) == ano,
    ).first()

    total_ha = db.query(func.coalesce(func.sum(models.Campo.area_ha), 0)).filter(
        models.Campo.activo == True
    ).scalar()

    financial = {
        "costo_total_mes": float(costos_mes[0] or 0),
        "costo_mo_mes": float(costos_mes[1] or 0),
        "costo_insumos_mes": float(costos_mes[2] or 0),
        "total_ha": float(total_ha or 0),
        "costo_por_ha": round(float(costos_mes[0] or 0) / float(total_ha), 2) if total_ha else 0,
    }

    # --- Low stock alert ---
    productos_bajo_stock = db.query(models.Producto).filter(
        models.Producto.activo == True,
        models.Producto.stock_actual <= models.Producto.stock_minimo,
        models.Producto.stock_minimo > 0
    ).count()

    return {
        "weather": weather,
        "weather_alerts": active_weather_alerts,
        "pest": pest,
        "irrigation": irrigation,
        "ots_today": ots_today,
        "ots_open": ots_open,
        "ocs_pending": ocs_pending,
        "ocs_pending_total": float(ocs_pending_total or 0),
        "financial": financial,
        "productos_bajo_stock": productos_bajo_stock,
    }


@router.get("/campo-status")
def campo_status(db: Session = Depends(get_db), _=Depends(auth.get_current_user)):
    """Per-campo status grid: soil, scouting, pest pressure, irrigation, OTs, costs."""
    now = datetime.now()
    mes, ano = now.month, now.year
    seven_days_ago = now - timedelta(days=7)

    campos = db.query(models.Campo).filter(models.Campo.activo == True).all()
    result = []

    for campo in campos:
        cid = campo.id_campo

        # Last soil reading
        last_soil = db.query(models.SoilReading).filter(
            models.SoilReading.campo_id == cid
        ).order_by(desc(models.SoilReading.reading_datetime)).first()

        soil_info = None
        if last_soil:
            soil_info = {
                "value_cbar": last_soil.value_cbar,
                "visual_scale": last_soil.visual_scale,
                "depth_cm": last_soil.depth_cm,
                "reading_datetime": last_soil.reading_datetime.isoformat() if last_soil.reading_datetime else None,
            }

        # Last scouting visit
        last_visit = db.query(models.ScoutingVisit).filter(
            models.ScoutingVisit.campo_id == cid
        ).order_by(desc(models.ScoutingVisit.visit_date)).first()

        scouting_info = None
        if last_visit:
            # Count threshold exceeded in this visit
            pest_alerts = db.query(models.PestObservation).filter(
                models.PestObservation.visit_id == last_visit.id,
                models.PestObservation.threshold_exceeded == True
            ).count()
            scouting_info = {
                "visit_date": last_visit.visit_date.isoformat() if last_visit.visit_date else None,
                "general_vigor": last_visit.general_vigor,
                "pest_alerts": pest_alerts,
            }

        # Pest pressure (last 7 days)
        pest_pressure = db.query(func.count(models.PestObservation.id)).join(
            models.ScoutingVisit
        ).filter(
            models.ScoutingVisit.campo_id == cid,
            models.ScoutingVisit.visit_date >= seven_days_ago,
            models.PestObservation.threshold_exceeded == True,
        ).scalar() or 0

        # Water balance (latest)
        last_balance = db.query(models.DailyWaterBalance).filter(
            models.DailyWaterBalance.campo_id == cid
        ).order_by(desc(models.DailyWaterBalance.date)).first()

        water_info = None
        if last_balance:
            water_info = {
                "date": last_balance.date.isoformat() if last_balance.date else None,
                "balance_mm": last_balance.balance_mm,
                "cumulative_deficit_mm": last_balance.cumulative_deficit_mm,
                "status": last_balance.status,
            }

        # OT count (open)
        ot_count = db.query(models.OrdenTrabajo).filter(
            models.OrdenTrabajo.campo_id == cid,
            models.OrdenTrabajo.estado.in_(["Abierta", "En Proceso"])
        ).count()

        # Cost accumulated this month
        cost_month = db.query(
            func.coalesce(func.sum(models.OrdenTrabajo.costo_total), 0)
        ).filter(
            models.OrdenTrabajo.campo_id == cid,
            models.OrdenTrabajo.fecha_ejecucion.isnot(None),
            extract('month', models.OrdenTrabajo.fecha_ejecucion) == mes,
            extract('year', models.OrdenTrabajo.fecha_ejecucion) == ano,
        ).scalar()

        # Determine status color: green/yellow/red
        status = "green"
        issues = []
        if pest_pressure > 0:
            status = "red" if pest_pressure >= 3 else "yellow"
            issues.append("pest")
        if water_info and water_info["status"] == "deficit":
            status = "red" if status == "yellow" else ("yellow" if status == "green" else status)
            issues.append("water")
        if soil_info and soil_info.get("visual_scale") and soil_info["visual_scale"] >= 4:
            status = "yellow" if status == "green" else status
            issues.append("soil")

        result.append({
            "id_campo": cid,
            "nombre": campo.nombre,
            "area_ha": campo.area_ha,
            "bloque": campo.bloque,
            "variedad": campo.variedad,
            "soil": soil_info,
            "scouting": scouting_info,
            "pest_pressure": pest_pressure,
            "water": water_info,
            "ot_open": ot_count,
            "cost_month": float(cost_month or 0),
            "status": status,
            "issues": issues,
        })

    return result


# ══════════════════════════════════════════════════════════════════════════════
# CALENDARIO AGRÍCOLA — Actividades sugeridas por mes (Manual FHIA 2008)
# ══════════════════════════════════════════════════════════════════════════════

CALENDARIO_AGRICOLA = {
    1: [
        {"actividad": "Control de maleza en época seca", "categoria": "maleza", "prioridad": "alta",
         "detalle": "Control manual o mecánico; una aplicación en época seca (ene-feb)"},
        {"actividad": "Monitoreo de ácaros y trips", "categoria": "sanidad", "prioridad": "alta",
         "detalle": "Araña blanca, arañuela roja y trips aumentan en época seca con baja humedad"},
        {"actividad": "Cosecha variedad Hass", "categoria": "cosecha", "prioridad": "alta",
         "detalle": "Hass en zonas de altura produce de octubre a febrero"},
        {"actividad": "Poda sanitaria", "categoria": "poda", "prioridad": "media",
         "detalle": "Eliminar ramas secas, enfermas o mal orientadas; desinfectar herramientas"},
    ],
    2: [
        {"actividad": "Control de maleza en época seca", "categoria": "maleza", "prioridad": "alta",
         "detalle": "Segunda intervención de control en seco; mantener corona limpia"},
        {"actividad": "Cosecha variedad Hass", "categoria": "cosecha", "prioridad": "alta",
         "detalle": "Últimos meses de cosecha Hass en zonas de altura"},
        {"actividad": "Monitoreo de ácaros", "categoria": "sanidad", "prioridad": "alta",
         "detalle": "Época seca favorece araña blanca y arañuela roja"},
        {"actividad": "Preparación de terreno para siembra", "categoria": "establecimiento", "prioridad": "baja",
         "detalle": "Abrir ahoyadura 1 mes antes de la siembra; dimensiones según tipo de suelo"},
    ],
    3: [
        {"actividad": "Inicio de floración", "categoria": "fenologia", "prioridad": "alta",
         "detalle": "Monitorear floración; humedad 75-80% es óptima para cuaje"},
        {"actividad": "Monitoreo de trips en flores", "categoria": "sanidad", "prioridad": "alta",
         "detalle": "Trips dañan órganos florales e inhiben polinización; acción si >5/brote"},
        {"actividad": "Aplicación de microelementos foliares", "categoria": "fertilizacion", "prioridad": "media",
         "detalle": "Cu, Zn, Mn y B por vía foliar; 1-2 veces al año según manual"},
    ],
    4: [
        {"actividad": "Floración y cuaje de frutos", "categoria": "fenologia", "prioridad": "alta",
         "detalle": "Solo 0.1% de flores se transforma en fruto; evitar estrés hídrico"},
        {"actividad": "Monitoreo de antracnosis en flores", "categoria": "sanidad", "prioridad": "alta",
         "detalle": "Colletotrichum causa manchas oscuras y caída de flores en periodos húmedos"},
        {"actividad": "Protección solar foliar", "categoria": "manejo", "prioridad": "media",
         "detalle": "Tronco y ramas primarias susceptibles a quemaduras de sol"},
    ],
    5: [
        {"actividad": "Desarrollo de frutos jóvenes", "categoria": "fenologia", "prioridad": "alta",
         "detalle": "Monitorear caída prematura de frutos; viento causa daño en frutos pequeños"},
        {"actividad": "Control de maleza (inicio de lluvias)", "categoria": "maleza", "prioridad": "alta",
         "detalle": "Primera intervención de control en época lluviosa"},
        {"actividad": "Monitoreo de barrenadores", "categoria": "sanidad", "prioridad": "media",
         "detalle": "Barrenador de ramas y taladrador del tronco activos; buscar aserrín blanco"},
    ],
    6: [
        {"actividad": "1ra fertilización edáfica", "categoria": "fertilizacion", "prioridad": "alta",
         "detalle": "Nitrato de Amonio + Superfosfato Triple + Cloruro de Potasio. Dosis según edad: 1-4 años: 299-896g N, 435-978g P, 167-750g K por planta"},
        {"actividad": "Época de siembra (trasplante)", "categoria": "establecimiento", "prioridad": "alta",
         "detalle": "Plantar cuando lluvias estén bien establecidas (junio-julio); suelos entre 1000-1600 msnm"},
        {"actividad": "Monitoreo de Phytophthora", "categoria": "sanidad", "prioridad": "alta",
         "detalle": "Lluvias favorecen pudrición de raíz; vigilar drenaje y follaje amarillento"},
        {"actividad": "Control de maleza (mediados de lluvia)", "categoria": "maleza", "prioridad": "alta",
         "detalle": "Control en calles con chapiadora + comaleo manual en zona de goteo"},
    ],
    7: [
        {"actividad": "2da fertilización edáfica", "categoria": "fertilizacion", "prioridad": "alta",
         "detalle": "Segunda dosis de fertilizante completo en periodo julio-agosto"},
        {"actividad": "Época de siembra (continuación)", "categoria": "establecimiento", "prioridad": "media",
         "detalle": "Último mes recomendado para trasplante a campo definitivo"},
        {"actividad": "Monitoreo de enfermedades de raíz", "categoria": "sanidad", "prioridad": "alta",
         "detalle": "Phytophthora, Rosellinia, Armillaria activos en suelos húmedos"},
        {"actividad": "Llenado de frutos", "categoria": "fenologia", "prioridad": "alta",
         "detalle": "Acumulación de aceites en fruto; no interrumpir riego si disponible"},
    ],
    8: [
        {"actividad": "2da fertilización (continuación)", "categoria": "fertilizacion", "prioridad": "alta",
         "detalle": "Completar segunda aplicación de fertilizantes si no se hizo en julio"},
        {"actividad": "Inicio cosecha variedad Fuerte", "categoria": "cosecha", "prioridad": "alta",
         "detalle": "Variedad Fuerte madura agosto-octubre; verificar contenido de materia seca"},
        {"actividad": "Monitoreo de sarna/roña", "categoria": "sanidad", "prioridad": "alta",
         "detalle": "Sphaceloma perseae: lesiones corchosas en frutos; aplicar cúpricos si >5% frutos afectados"},
    ],
    9: [
        {"actividad": "Cosecha variedad Fuerte", "categoria": "cosecha", "prioridad": "alta",
         "detalle": "Maduración de Fuerte; cosechar con 1-3 mm de pedúnculo para evitar pudrición"},
        {"actividad": "Control de maleza (finales de lluvia)", "categoria": "maleza", "prioridad": "alta",
         "detalle": "Última intervención de control en época lluviosa"},
        {"actividad": "Monitoreo de antracnosis en frutos", "categoria": "sanidad", "prioridad": "alta",
         "detalle": "Manchas oscuras en frutos desarrollados; periodos húmedos de mayor riesgo"},
    ],
    10: [
        {"actividad": "3ra fertilización edáfica", "categoria": "fertilizacion", "prioridad": "alta",
         "detalle": "Tercera y última aplicación del año; ajustar dosis según edad del árbol y análisis de suelo"},
        {"actividad": "Inicio cosecha variedad Hass", "categoria": "cosecha", "prioridad": "alta",
         "detalle": "Hass en zonas de altura produce octubre-febrero; verificar madurez por materia seca"},
        {"actividad": "Cosecha variedad Fuerte (fin)", "categoria": "cosecha", "prioridad": "media",
         "detalle": "Últimas recolecciones de Fuerte"},
    ],
    11: [
        {"actividad": "Cosecha variedad Hass", "categoria": "cosecha", "prioridad": "alta",
         "detalle": "Período principal de cosecha Hass"},
        {"actividad": "Monitoreo de oidio", "categoria": "sanidad", "prioridad": "media",
         "detalle": "Mildeu polvoso aparece en época de poca lluvia; micelio blanco sobre hojas"},
        {"actividad": "Poda de formación (árboles jóvenes)", "categoria": "poda", "prioridad": "media",
         "detalle": "Corte apical a 60 cm si no hay bifurcación; eliminar ramas bajas y entrecruzadas"},
    ],
    12: [
        {"actividad": "Cosecha variedad Hass", "categoria": "cosecha", "prioridad": "alta",
         "detalle": "Cosecha continúa; clasificar por calibre según mercado destino"},
        {"actividad": "Poda sanitaria post-cosecha", "categoria": "poda", "prioridad": "alta",
         "detalle": "Podar ramas secas y enfermas; desinfectar entre árboles; aplicar pasta cúprica en cortes"},
        {"actividad": "Planificación del próximo ciclo", "categoria": "planificacion", "prioridad": "media",
         "detalle": "Revisar rendimientos, analizar suelos, programar fertilización y compras de insumos"},
    ],
}


@router.get("/calendario-agricola")
def calendario_agricola(
    mes: Optional[int] = None,
    db: Session = Depends(get_db),
    _=Depends(auth.get_current_user),
):
    """Calendario de actividades agrícolas sugeridas por mes (Manual FHIA 2008)."""
    target_month = mes or datetime.now().month
    actividades = CALENDARIO_AGRICOLA.get(target_month, [])
    month_names = {
        1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril",
        5: "Mayo", 6: "Junio", 7: "Julio", 8: "Agosto",
        9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre"
    }
    return {
        "mes": target_month,
        "mes_nombre": month_names[target_month],
        "actividades": actividades,
        "fuente": "Manual Técnico del Cultivo del Aguacate Hass — FHIA, 2008",
    }
