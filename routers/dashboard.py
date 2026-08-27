from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import func, extract, desc, case, and_, Date, String as SaString
from database import get_db
import models, schemas, auth
from datetime import datetime, timedelta, date
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

    costo_mo_mes = db.query(
        func.coalesce(func.sum(models.OTManoObra.costo_mo), 0)
    ).filter(
        models.OTManoObra.fecha.isnot(None),
        extract('month', models.OTManoObra.fecha) == mes,
        extract('year', models.OTManoObra.fecha) == ano,
    ).scalar()

    costos_ot_mes = _filter_mes_ano(
        db.query(
            func.coalesce(func.sum(models.OrdenTrabajo.costo_insumos), 0),
            func.coalesce(func.sum(models.OrdenTrabajo.costo_equipo), 0),
        ),
        models.OrdenTrabajo.fecha_ejecucion, mes, ano
    ).first()

    costo_insumos = float(costos_ot_mes[0] or 0)
    costo_equipo = float(costos_ot_mes[1] or 0)
    costo_mo = float(costo_mo_mes or 0)

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
        costo_mo_mes=costo_mo,
        costo_insumos_mes=costo_insumos,
        costo_total_mes=costo_mo + costo_insumos + costo_equipo,
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
    desde: str = Query(None), hasta: str = Query(None),
    trabajador: str = Query(None),
    db: Session = Depends(get_db), _=Depends(auth.get_current_user)
):
    now = datetime.now()

    q = db.query(
        models.Trabajador.id_trab,
        models.Trabajador.nombre,
        models.Trabajador.cargo,
        func.coalesce(func.sum(models.OTManoObra.costo_mo), 0).label("total_ganado"),
        func.count(models.OTManoObra.id).label("num_jornadas"),
        func.coalesce(func.sum(models.OTManoObra.horas_netas), 0).label("total_horas"),
    ).outerjoin(
        models.OTManoObra,
        models.Trabajador.id_trab == models.OTManoObra.trabajador_id
    ).filter(
        models.Trabajador.activo == True
    )

    if trabajador:
        q = q.filter(
            models.Trabajador.nombre.ilike(f"%{trabajador}%") |
            models.Trabajador.id_trab.ilike(f"%{trabajador}%")
        )

    if desde and hasta:
        q = q.filter(
            (models.OTManoObra.id.is_(None)) |
            (
                models.OTManoObra.fecha.isnot(None) &
                (func.cast(models.OTManoObra.fecha, Date) >= desde) &
                (func.cast(models.OTManoObra.fecha, Date) <= hasta)
            )
        )
    else:
        filtro_mes = mes or now.month
        filtro_ano = ano or now.year
        q = q.filter(
            (models.OTManoObra.id.is_(None)) |
            (
                models.OTManoObra.fecha.isnot(None) &
                (extract('month', models.OTManoObra.fecha) == filtro_mes) &
                (extract('year', models.OTManoObra.fecha) == filtro_ano)
            )
        )

    rows = q.group_by(
        models.Trabajador.id_trab, models.Trabajador.nombre, models.Trabajador.cargo
    ).all()

    return [
        {"id_trab": r[0], "nombre": r[1], "cargo": r[2],
         "total_ganado": float(r[3]), "num_jornadas": int(r[4]),
         "total_horas": round(float(r[5]), 2)}
        for r in rows
    ]


@router.get("/nomina-detalle/{id_trab}")
def nomina_detalle_trabajador(
    id_trab: str, mes: int = Query(None), ano: int = Query(None),
    desde: str = Query(None), hasta: str = Query(None),
    db: Session = Depends(get_db), _=Depends(auth.get_current_user)
):
    q = db.query(
        models.OTManoObra,
        models.OrdenTrabajo.campo_id,
        models.OrdenTrabajo.actividad_id,
        models.OrdenTrabajo.supervisor,
        models.OrdenTrabajo.estado,
        models.OrdenTrabajo.fecha_ejecucion,
    ).join(
        models.OrdenTrabajo,
        models.OTManoObra.ot_id == models.OrdenTrabajo.ot_id
    ).filter(
        models.OTManoObra.trabajador_id == id_trab,
        models.OTManoObra.fecha.isnot(None),
    )

    if desde and hasta:
        q = q.filter(
            func.cast(models.OTManoObra.fecha, Date) >= desde,
            func.cast(models.OTManoObra.fecha, Date) <= hasta,
        )
    elif mes and ano:
        q = q.filter(
            extract('month', models.OTManoObra.fecha) == mes,
            extract('year', models.OTManoObra.fecha) == ano,
        )

    rows = q.order_by(models.OTManoObra.fecha.desc()).all()

    return [
        {
            "mo_id": r[0].id,
            "ot_id": r[0].ot_id, "fecha": r[0].fecha.isoformat() if r[0].fecha else None,
            "hora_inicio": r[0].hora_inicio, "hora_fin": r[0].hora_fin,
            "pausa_min": r[0].pausa_min or 0,
            "horas_netas": r[0].horas_netas, "modalidad": r[0].modalidad,
            "costo_hora": float(r[0].costo_hora or 0), "costo_mo": float(r[0].costo_mo or 0),
            "campo_id": r[1], "actividad_id": r[2], "supervisor": r[3],
            "estado_ot": r[4], "fecha_ot": r[5].isoformat() if r[5] else None,
        }
        for r in rows
    ]


@router.get("/nomina-horas-diarias")
def nomina_horas_diarias(
    desde: str = Query(...), hasta: str = Query(...),
    trabajador: str = Query(None),
    db: Session = Depends(get_db), _=Depends(auth.get_current_user)
):
    q = db.query(
        func.cast(models.OTManoObra.fecha, Date).label("dia"),
        models.OTManoObra.trabajador_id,
        models.Trabajador.nombre,
        models.Trabajador.cargo,
        func.coalesce(func.sum(models.OTManoObra.horas_netas), 0).label("horas"),
        func.coalesce(func.sum(models.OTManoObra.costo_mo), 0).label("costo"),
        func.count(models.OTManoObra.id).label("jornadas"),
        func.string_agg(
            func.cast(models.OTManoObra.ot_id, SaString),
            func.cast(',', SaString)
        ).label("ots"),
    ).join(
        models.Trabajador,
        models.OTManoObra.trabajador_id == models.Trabajador.id_trab
    ).filter(
        models.OTManoObra.fecha.isnot(None),
        func.cast(models.OTManoObra.fecha, Date) >= desde,
        func.cast(models.OTManoObra.fecha, Date) <= hasta,
    )

    if trabajador:
        q = q.filter(
            models.Trabajador.nombre.ilike(f"%{trabajador}%") |
            models.Trabajador.id_trab.ilike(f"%{trabajador}%")
        )

    rows = q.group_by(
        func.cast(models.OTManoObra.fecha, Date),
        models.OTManoObra.trabajador_id,
        models.Trabajador.nombre,
        models.Trabajador.cargo,
    ).order_by(
        func.cast(models.OTManoObra.fecha, Date).desc(),
        models.Trabajador.nombre,
    ).all()

    return [
        {
            "fecha": str(r[0]),
            "id_trab": r[1], "nombre": r[2], "cargo": r[3],
            "horas": round(float(r[4]), 2),
            "costo": round(float(r[5]), 2),
            "jornadas": int(r[6]),
            "ots": r[7] or "",
        }
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
    # MO from OTManoObra.fecha to match Nómina
    costo_mo_mes = db.query(
        func.coalesce(func.sum(models.OTManoObra.costo_mo), 0)
    ).filter(
        models.OTManoObra.fecha.isnot(None),
        extract('month', models.OTManoObra.fecha) == mes,
        extract('year', models.OTManoObra.fecha) == ano,
    ).scalar()

    costos_ot_mes = db.query(
        func.coalesce(func.sum(models.OrdenTrabajo.costo_insumos), 0),
        func.coalesce(func.sum(models.OrdenTrabajo.costo_equipo), 0),
    ).filter(
        models.OrdenTrabajo.fecha_ejecucion.isnot(None),
        extract('month', models.OrdenTrabajo.fecha_ejecucion) == mes,
        extract('year', models.OrdenTrabajo.fecha_ejecucion) == ano,
    ).first()

    f_mo = float(costo_mo_mes or 0)
    f_insumos = float(costos_ot_mes[0] or 0)
    f_equipo = float(costos_ot_mes[1] or 0)
    f_total = f_mo + f_insumos + f_equipo

    total_ha = db.query(func.coalesce(func.sum(models.Campo.area_ha), 0)).filter(
        models.Campo.activo == True
    ).scalar()

    financial = {
        "costo_total_mes": f_total,
        "costo_mo_mes": f_mo,
        "costo_insumos_mes": f_insumos,
        "total_ha": float(total_ha or 0),
        "costo_por_ha": round(f_total / float(total_ha), 2) if total_ha else 0,
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


@router.get("/produccion")
def reporte_produccion(temporada: str = None, db: Session = Depends(get_db), user=Depends(auth.get_current_user)):
    """Reportes de producción: rendimiento por campo, costo por kg."""
    campos = db.query(models.Campo).all()
    hoy = date.today()
    temp = temporada or f"{hoy.year}"

    result = []
    for campo in campos:
        ots = db.query(models.OrdenTrabajo).filter(
            models.OrdenTrabajo.campo_id == campo.id_campo,
            models.OrdenTrabajo.estado == "cerrada",
        ).all()

        costo_mo = sum(float(ot.costo_mano_obra or 0) for ot in ots)
        costo_insumos = sum(float(ot.costo_insumos or 0) for ot in ots)
        costo_total = costo_mo + costo_insumos

        liq = db.query(models.CosechaLiquidacion).filter(
            models.CosechaLiquidacion.campo_id == campo.id_campo,
            models.CosechaLiquidacion.temporada.like(f"%{temp}%"),
        ).all()
        kg_total = sum(float(l.kg_cosechados or 0) for l in liq)

        cxc = db.query(models.CuentaPorCobrar).filter(
            models.CuentaPorCobrar.campo_id == campo.id_campo,
        ).all()
        ingresos = sum(float(c.total or 0) for c in cxc)
        kg_vendidos = sum(float(c.kg_vendidos or 0) for c in cxc if c.kg_vendidos)

        result.append({
            "campo_id": campo.id_campo,
            "nombre": campo.nombre or campo.id_campo,
            "area_ha": campo.area_ha,
            "n_plantas": campo.n_plantas,
            "costo_mo": round(costo_mo, 2),
            "costo_insumos": round(costo_insumos, 2),
            "costo_total": round(costo_total, 2),
            "kg_cosechados": round(kg_total, 2),
            "kg_vendidos": round(kg_vendidos, 2),
            "costo_por_kg": round(costo_total / kg_total, 2) if kg_total > 0 else 0,
            "costo_por_ha": round(costo_total / campo.area_ha, 2) if campo.area_ha and campo.area_ha > 0 else 0,
            "kg_por_ha": round(kg_total / campo.area_ha, 2) if campo.area_ha and campo.area_ha > 0 else 0,
            "ingresos": round(ingresos, 2),
            "utilidad": round(ingresos - costo_total, 2),
            "margen": round(((ingresos - costo_total) / ingresos) * 100, 1) if ingresos > 0 else 0,
        })
    totales = {
        "costo_total": round(sum(r["costo_total"] for r in result), 2),
        "kg_cosechados": round(sum(r["kg_cosechados"] for r in result), 2),
        "ingresos": round(sum(r["ingresos"] for r in result), 2),
        "utilidad": round(sum(r["utilidad"] for r in result), 2),
    }
    totales["costo_por_kg"] = round(totales["costo_total"] / totales["kg_cosechados"], 2) if totales["kg_cosechados"] > 0 else 0
    totales["margen"] = round((totales["utilidad"] / totales["ingresos"]) * 100, 1) if totales["ingresos"] > 0 else 0
    return {"campos": result, "totales": totales, "temporada": temp}
