"""Router sanidad — MIP: monitoreo plagas/enfermedades, trampas, spray logs."""
from datetime import datetime, timedelta
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import func, desc, and_, cast, Date, Integer  # H5: Integer moved to top

from database import get_db
import models
import auth
import audit  # H1: audit trail
from routers.sequences import get_next

from datetime import date as _date  # C1: date validation

router = APIRouter(prefix="/api/sanidad", tags=["sanidad"])


# ──────────────── date validation helper ────────────────── C1
def _parse_date(s: str, name: str) -> _date:
    try:
        return _date.fromisoformat(s)
    except (ValueError, TypeError):
        raise HTTPException(400, f"{name} formato inválido. Use YYYY-MM-DD")


# ══════════════════════════════════════════════════════════════════════════════
# Pydantic schemas
# ══════════════════════════════════════════════════════════════════════════════

class CatalogoIn(BaseModel):
    code: str
    common_name: str
    scientific_name: Optional[str] = None
    category: Optional[str] = None
    pest_type: Optional[str] = None
    importance: Optional[str] = None
    monitoring_method: Optional[str] = None
    sample_unit: Optional[str] = None
    sample_size: Optional[int] = None
    monitoring_frequency_days: Optional[int] = 7
    action_threshold: Optional[float] = None
    threshold_unit: Optional[str] = None
    threshold_description: Optional[str] = None
    affects_leaves: bool = False
    affects_fruit: bool = False
    affects_roots: bool = False
    affects_trunk: bool = False
    peak_months: Optional[str] = None
    activo: bool = True


class ObservacionIn(BaseModel):
    pest_id: int
    total_count: Optional[int] = None
    sample_units: Optional[int] = None
    average_per_unit: Optional[float] = None
    severity_scale: Optional[int] = 0
    affected_area_pct: Optional[float] = None
    affected_trees_pct: Optional[float] = None
    plant_part: Optional[str] = None
    canopy_position: Optional[str] = None
    recommended_action: Optional[str] = "ninguna"
    notes: Optional[str] = None


class VisitaIn(BaseModel):
    campo_id: str
    visit_date: str
    phenological_stage: Optional[str] = None
    trees_sampled: Optional[int] = None
    leaves_sampled: Optional[int] = None
    fruits_sampled: Optional[int] = None
    general_vigor: Optional[str] = None
    temperature_c: Optional[float] = None
    humidity_pct: Optional[float] = None
    wind_speed_kmh: Optional[float] = None
    recent_rain_mm: Optional[float] = None
    observations: Optional[str] = None
    pest_observations: List[ObservacionIn] = []


class TrapIn(BaseModel):
    trap_type: str
    campo_id: str
    target_pest_id: Optional[int] = None
    location_desc: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    installation_date: Optional[str] = None
    lure_type: Optional[str] = None
    lure_change_days: Optional[int] = 14
    status: str = "activa"


class TrapUpdate(BaseModel):
    trap_type: Optional[str] = None
    target_pest_id: Optional[int] = None
    location_desc: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    lure_type: Optional[str] = None
    lure_change_days: Optional[int] = None
    status: Optional[str] = None


class TrapReadingIn(BaseModel):
    reading_date: str
    visit_id: Optional[int] = None
    target_count: int = 0
    non_target_count: int = 0
    lure_replaced: bool = False
    notes: Optional[str] = None


class SprayLogIn(BaseModel):
    campo_id: str
    application_date: str
    ot_id: Optional[int] = None
    trigger_visit_id: Optional[int] = None
    target_pests: Optional[str] = None
    justification: Optional[str] = None
    products_json: Optional[str] = None
    water_volume_l_ha: Optional[float] = None
    method: Optional[str] = None
    area_treated_ha: Optional[float] = None
    temp_c: Optional[float] = None
    humidity_pct: Optional[float] = None
    wind_kmh: Optional[float] = None
    rei_hours: Optional[int] = None
    phi_days: Optional[int] = None
    efficacy_check_date: Optional[str] = None
    efficacy_rating: Optional[str] = None
    notes: Optional[str] = None


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _row_to_dict(row):
    """Convert a SQLAlchemy model instance to dict."""
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return datetime.strptime(s[:10], "%Y-%m-%d")


# ══════════════════════════════════════════════════════════════════════════════
# 1-3. CATÁLOGO DE PLAGAS/ENFERMEDADES
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/catalogo")
def list_catalogo(
    pest_type: Optional[str] = None,
    category: Optional[str] = None,
    db: Session = Depends(get_db),
    _user=Depends(auth.get_current_user),
):
    q = db.query(models.PestCatalog)
    if pest_type:
        q = q.filter(models.PestCatalog.pest_type == pest_type)
    if category:
        q = q.filter(models.PestCatalog.category == category)
    return [_row_to_dict(r) for r in q.order_by(models.PestCatalog.code).all()]


@router.post("/catalogo")
def create_catalogo(
    data: CatalogoIn,
    db: Session = Depends(get_db),
    _user=Depends(auth.require_supervisor),
):
    entry = models.PestCatalog(**data.model_dump())
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return _row_to_dict(entry)


@router.put("/catalogo/{pest_id}")
def update_catalogo(
    pest_id: int,
    data: CatalogoIn,
    db: Session = Depends(get_db),
    _user=Depends(auth.require_supervisor),
):
    entry = db.query(models.PestCatalog).filter(models.PestCatalog.id == pest_id).first()
    if not entry:
        raise HTTPException(404, "Plaga no encontrada en catálogo")
    for k, v in data.model_dump().items():
        setattr(entry, k, v)
    db.commit()
    db.refresh(entry)
    return _row_to_dict(entry)


# ══════════════════════════════════════════════════════════════════════════════
# 4-7. MONITOREO (SCOUTING VISITS)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/monitoreo")
def list_visitas(
    campo_id: Optional[str] = None,
    desde: Optional[str] = None,
    hasta: Optional[str] = None,
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    _user=Depends(auth.get_current_user),
):
    q = db.query(models.ScoutingVisit)
    if campo_id:
        q = q.filter(models.ScoutingVisit.campo_id == campo_id)
    if desde:
        d_desde = _parse_date(desde, "desde")  # C1
        q = q.filter(models.ScoutingVisit.visit_date >= d_desde)
    if hasta:
        d_hasta = _parse_date(hasta, "hasta")  # C1
        q = q.filter(models.ScoutingVisit.visit_date <= d_hasta)
    if status:
        q = q.filter(models.ScoutingVisit.status == status)
    visits = q.order_by(desc(models.ScoutingVisit.visit_date)).all()
    result = []
    for v in visits:
        d = _row_to_dict(v)
        # campo name
        campo = db.query(models.Campo).filter(models.Campo.id_campo == v.campo_id).first()
        d["campo_nombre"] = campo.nombre if campo else v.campo_id
        # scout name
        scout = db.query(models.Usuario).filter(models.Usuario.id == v.scout_id).first() if v.scout_id else None
        d["scout_nombre"] = scout.nombre if scout else "—"
        # observation count
        obs_count = db.query(func.count(models.PestObservation.id)).filter(
            models.PestObservation.visit_id == v.id
        ).scalar()
        d["obs_count"] = obs_count or 0
        # alerts
        alerts = db.query(func.count(models.PestObservation.id)).filter(
            models.PestObservation.visit_id == v.id,
            models.PestObservation.threshold_exceeded == True,
        ).scalar()
        d["alerts"] = alerts or 0
        result.append(d)
    return result


@router.post("/monitoreo")
def create_visita(
    data: VisitaIn,
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user),
):
    visit_code = get_next("MON", db)
    visit = models.ScoutingVisit(
        visit_code=visit_code,
        campo_id=data.campo_id,
        scout_id=user.id,
        visit_date=_parse_dt(data.visit_date),
        phenological_stage=data.phenological_stage,
        trees_sampled=data.trees_sampled,
        leaves_sampled=data.leaves_sampled,
        fruits_sampled=data.fruits_sampled,
        general_vigor=data.general_vigor,
        temperature_c=data.temperature_c,
        humidity_pct=data.humidity_pct,
        wind_speed_kmh=data.wind_speed_kmh,
        recent_rain_mm=data.recent_rain_mm,
        observations=data.observations,
        status="borrador",
    )
    db.add(visit)
    db.flush()

    # Add pest observations
    for obs_data in data.pest_observations:
        pest = db.query(models.PestCatalog).filter(models.PestCatalog.id == obs_data.pest_id).first()
        if not pest:
            raise HTTPException(400, f"Plaga ID {obs_data.pest_id} no encontrada")

        avg = obs_data.average_per_unit
        if avg is None and obs_data.total_count is not None and obs_data.sample_units and obs_data.sample_units > 0:
            avg = obs_data.total_count / obs_data.sample_units

        threshold_exceeded = False
        if pest.action_threshold is not None and avg is not None:
            threshold_exceeded = avg >= pest.action_threshold

        obs = models.PestObservation(
            visit_id=visit.id,
            pest_id=obs_data.pest_id,
            total_count=obs_data.total_count,
            sample_units=obs_data.sample_units,
            average_per_unit=avg,
            severity_scale=obs_data.severity_scale,
            affected_area_pct=obs_data.affected_area_pct,
            affected_trees_pct=obs_data.affected_trees_pct,
            plant_part=obs_data.plant_part,
            canopy_position=obs_data.canopy_position,
            threshold_exceeded=threshold_exceeded,
            recommended_action=obs_data.recommended_action,
            notes=obs_data.notes,
        )
        db.add(obs)

    # H1: audit trail
    audit.log(db, user, 'CREAR', 'VISITA', str(visit.id),
              f"Visita {visit_code} en campo {data.campo_id}")
    db.commit()
    db.refresh(visit)
    return {**_row_to_dict(visit), "visit_code": visit.visit_code}


@router.get("/monitoreo/{visit_id}")
def get_visita(
    visit_id: int,
    db: Session = Depends(get_db),
    _user=Depends(auth.get_current_user),
):
    visit = db.query(models.ScoutingVisit).filter(models.ScoutingVisit.id == visit_id).first()
    if not visit:
        raise HTTPException(404, "Visita no encontrada")
    d = _row_to_dict(visit)
    campo = db.query(models.Campo).filter(models.Campo.id_campo == visit.campo_id).first()
    d["campo_nombre"] = campo.nombre if campo else visit.campo_id
    scout = db.query(models.Usuario).filter(models.Usuario.id == visit.scout_id).first() if visit.scout_id else None
    d["scout_nombre"] = scout.nombre if scout else "—"

    observations = db.query(models.PestObservation).filter(
        models.PestObservation.visit_id == visit.id
    ).all()
    obs_list = []
    for o in observations:
        od = _row_to_dict(o)
        pest = db.query(models.PestCatalog).filter(models.PestCatalog.id == o.pest_id).first()
        od["pest_name"] = pest.common_name if pest else f"ID-{o.pest_id}"
        od["pest_code"] = pest.code if pest else ""
        od["threshold_value"] = pest.action_threshold if pest else None
        od["threshold_unit"] = pest.threshold_unit if pest else None
        obs_list.append(od)
    d["observations"] = obs_list
    return d


@router.put("/monitoreo/{visit_id}/status")
def update_visit_status(
    visit_id: int,
    status: str = Query(...),
    db: Session = Depends(get_db),
    current_user=Depends(auth.require_supervisor),  # M5: require_supervisor
):
    visit = db.query(models.ScoutingVisit).filter(models.ScoutingVisit.id == visit_id).first()
    if not visit:
        raise HTTPException(404, "Visita no encontrada")
    if status not in ("borrador", "completado", "revisado"):
        raise HTTPException(400, "Estado inválido")
    visit.status = status
    db.commit()
    return {"ok": True, "status": status}


# ══════════════════════════════════════════════════════════════════════════════
# 8-12. TRAMPAS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/trampas")
def list_trampas(
    campo_id: Optional[str] = None,
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    _user=Depends(auth.get_current_user),
):
    q = db.query(models.Trap)
    if campo_id:
        q = q.filter(models.Trap.campo_id == campo_id)
    if status:
        q = q.filter(models.Trap.status == status)
    traps = q.order_by(models.Trap.trap_code).all()
    result = []
    for t in traps:
        d = _row_to_dict(t)
        campo = db.query(models.Campo).filter(models.Campo.id_campo == t.campo_id).first()
        d["campo_nombre"] = campo.nombre if campo else t.campo_id
        pest = db.query(models.PestCatalog).filter(models.PestCatalog.id == t.target_pest_id).first() if t.target_pest_id else None
        d["target_pest_name"] = pest.common_name if pest else "—"
        # Last reading
        last_read = db.query(models.TrapReading).filter(
            models.TrapReading.trap_id == t.id
        ).order_by(desc(models.TrapReading.reading_date)).first()
        d["last_reading_date"] = last_read.reading_date.isoformat() if last_read and last_read.reading_date else None
        d["last_ftd"] = last_read.catches_per_trap_day if last_read else None
        d["last_threshold_exceeded"] = last_read.threshold_exceeded if last_read else None
        result.append(d)
    return result


@router.post("/trampas")
def create_trampa(
    data: TrapIn,
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user),
):
    trap_code = get_next("TR", db)
    trap = models.Trap(
        trap_code=trap_code,
        trap_type=data.trap_type,
        campo_id=data.campo_id,
        target_pest_id=data.target_pest_id,
        location_desc=data.location_desc,
        latitude=data.latitude,
        longitude=data.longitude,
        installation_date=_parse_dt(data.installation_date),
        lure_type=data.lure_type,
        lure_change_days=data.lure_change_days,
        last_lure_change=_parse_dt(data.installation_date),
        status=data.status,
    )
    db.add(trap)
    db.flush()
    # H1: audit trail
    audit.log(db, user, 'CREAR', 'TRAMPA', str(trap.id),
              f"Trampa {trap_code} en campo {data.campo_id}")
    db.commit()
    db.refresh(trap)
    return _row_to_dict(trap)


@router.put("/trampas/{trap_id}")
def update_trampa(
    trap_id: int,
    data: TrapUpdate,
    db: Session = Depends(get_db),
    _user=Depends(auth.get_current_user),
):
    trap = db.query(models.Trap).filter(models.Trap.id == trap_id).first()
    if not trap:
        raise HTTPException(404, "Trampa no encontrada")
    for k, v in data.model_dump(exclude_none=True).items():
        setattr(trap, k, v)
    db.commit()
    db.refresh(trap)
    return _row_to_dict(trap)


@router.post("/trampas/{trap_id}/lectura")
def add_trap_reading(
    trap_id: int,
    data: TrapReadingIn,
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user),
):
    trap = db.query(models.Trap).filter(models.Trap.id == trap_id).first()
    if not trap:
        raise HTTPException(404, "Trampa no encontrada")

    reading_date = _parse_dt(data.reading_date)

    # Calculate days since last reading
    last_read = db.query(models.TrapReading).filter(
        models.TrapReading.trap_id == trap_id
    ).order_by(desc(models.TrapReading.reading_date)).first()

    days_since = None
    if last_read and last_read.reading_date and reading_date:
        delta = reading_date - last_read.reading_date
        days_since = max(delta.days, 1)

    ctd = None
    if days_since and days_since > 0:
        ctd = data.target_count / days_since
    elif reading_date and trap.installation_date:
        days_installed = max((reading_date - trap.installation_date).days, 1)
        ctd = data.target_count / days_installed
        days_since = days_installed

    # Check threshold
    threshold_exceeded = False
    if trap.target_pest_id and ctd is not None:
        pest = db.query(models.PestCatalog).filter(models.PestCatalog.id == trap.target_pest_id).first()
        if pest and pest.action_threshold is not None:
            threshold_exceeded = ctd >= pest.action_threshold

    reading = models.TrapReading(
        trap_id=trap_id,
        visit_id=data.visit_id,
        reading_date=reading_date,
        reader_id=user.id,
        target_count=data.target_count,
        non_target_count=data.non_target_count,
        days_since_last=days_since,
        catches_per_trap_day=round(ctd, 4) if ctd is not None else None,
        lure_replaced=data.lure_replaced,
        threshold_exceeded=threshold_exceeded,
        notes=data.notes,
    )
    db.add(reading)

    if data.lure_replaced:
        trap.last_lure_change = reading_date

    db.flush()
    # H1: audit trail
    audit.log(db, user, 'CREAR', 'TRAP_READING', str(reading.id),
              f"Lectura trampa {trap_id}: target={data.target_count}")
    db.commit()
    db.refresh(reading)
    return _row_to_dict(reading)


@router.get("/trampas/{trap_id}/lecturas")
def list_trap_readings(
    trap_id: int,
    db: Session = Depends(get_db),
    _user=Depends(auth.get_current_user),
):
    readings = db.query(models.TrapReading).filter(
        models.TrapReading.trap_id == trap_id
    ).order_by(desc(models.TrapReading.reading_date)).all()
    result = []
    for r in readings:
        d = _row_to_dict(r)
        reader = db.query(models.Usuario).filter(models.Usuario.id == r.reader_id).first() if r.reader_id else None
        d["reader_name"] = reader.nombre if reader else "—"
        result.append(d)
    return result


# ══════════════════════════════════════════════════════════════════════════════
# 13-15. SPRAY LOGS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/spray-logs")
def list_spray_logs(
    campo_id: Optional[str] = None,
    desde: Optional[str] = None,
    hasta: Optional[str] = None,
    db: Session = Depends(get_db),
    _user=Depends(auth.get_current_user),
):
    q = db.query(models.SprayLog)
    if campo_id:
        q = q.filter(models.SprayLog.campo_id == campo_id)
    if desde:
        d_desde = _parse_date(desde, "desde")  # C1
        q = q.filter(models.SprayLog.application_date >= d_desde)
    if hasta:
        d_hasta = _parse_date(hasta, "hasta")  # C1
        q = q.filter(models.SprayLog.application_date <= d_hasta)
    logs = q.order_by(desc(models.SprayLog.application_date)).all()
    result = []
    for sl in logs:
        d = _row_to_dict(sl)
        campo = db.query(models.Campo).filter(models.Campo.id_campo == sl.campo_id).first()
        d["campo_nombre"] = campo.nombre if campo else sl.campo_id
        applicator = db.query(models.Usuario).filter(models.Usuario.id == sl.applicator_id).first() if sl.applicator_id else None
        d["applicator_name"] = applicator.nombre if applicator else "—"
        result.append(d)
    return result


@router.post("/spray-logs")
def create_spray_log(
    data: SprayLogIn,
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user),
):
    spray_code = get_next("SPR", db)
    app_date = _parse_dt(data.application_date)

    rei_expiry = None
    if data.rei_hours and app_date:
        rei_expiry = app_date + timedelta(hours=data.rei_hours)

    earliest_harvest = None
    if data.phi_days and app_date:
        earliest_harvest = app_date + timedelta(days=data.phi_days)

    sl = models.SprayLog(
        spray_code=spray_code,
        ot_id=data.ot_id,
        campo_id=data.campo_id,
        application_date=app_date,
        applicator_id=user.id,
        trigger_visit_id=data.trigger_visit_id,
        target_pests=data.target_pests,
        justification=data.justification,
        products_json=data.products_json,
        water_volume_l_ha=data.water_volume_l_ha,
        method=data.method,
        area_treated_ha=data.area_treated_ha,
        temp_c=data.temp_c,
        humidity_pct=data.humidity_pct,
        wind_kmh=data.wind_kmh,
        rei_hours=data.rei_hours,
        rei_expiry=rei_expiry,
        phi_days=data.phi_days,
        earliest_harvest=earliest_harvest,
        efficacy_check_date=_parse_dt(data.efficacy_check_date),
        efficacy_rating=data.efficacy_rating,
        notes=data.notes,
    )
    db.add(sl)
    db.flush()
    # H1: audit trail
    audit.log(db, user, 'CREAR', 'SPRAY_LOG', str(sl.id),
              f"Aplicación {spray_code} en campo {data.campo_id}")
    db.commit()
    db.refresh(sl)
    return _row_to_dict(sl)


@router.get("/spray-logs/{log_id}")
def get_spray_log(
    log_id: int,
    db: Session = Depends(get_db),
    _user=Depends(auth.get_current_user),
):
    sl = db.query(models.SprayLog).filter(models.SprayLog.id == log_id).first()
    if not sl:
        raise HTTPException(404, "Registro de aplicación no encontrado")
    d = _row_to_dict(sl)
    campo = db.query(models.Campo).filter(models.Campo.id_campo == sl.campo_id).first()
    d["campo_nombre"] = campo.nombre if campo else sl.campo_id
    applicator = db.query(models.Usuario).filter(models.Usuario.id == sl.applicator_id).first() if sl.applicator_id else None
    d["applicator_name"] = applicator.nombre if applicator else "—"
    return d


# ══════════════════════════════════════════════════════════════════════════════
# 16. DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/dashboard")
def dashboard(
    db: Session = Depends(get_db),
    _user=Depends(auth.get_current_user),
):
    now = datetime.now()
    first_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # Visits this month
    visits_month = db.query(func.count(models.ScoutingVisit.id)).filter(
        models.ScoutingVisit.visit_date >= first_of_month
    ).scalar() or 0

    # Alerts (threshold_exceeded observations this month)
    alerts_count = db.query(func.count(models.PestObservation.id)).join(
        models.ScoutingVisit, models.PestObservation.visit_id == models.ScoutingVisit.id
    ).filter(
        models.ScoutingVisit.visit_date >= first_of_month,
        models.PestObservation.threshold_exceeded == True,
    ).scalar() or 0

    # Active traps
    active_traps = db.query(func.count(models.Trap.id)).filter(
        models.Trap.status == "activa"
    ).scalar() or 0

    # Spray logs this month
    sprays_month = db.query(func.count(models.SprayLog.id)).filter(
        models.SprayLog.application_date >= first_of_month
    ).scalar() or 0

    # Top pests detected (all time, top 10)
    top_pests = db.query(
        models.PestCatalog.common_name,
        models.PestCatalog.code,
        func.count(models.PestObservation.id).label("detections"),
        func.sum(
            func.cast(models.PestObservation.threshold_exceeded == True, Integer)
        ).label("alerts"),
    ).join(
        models.PestObservation, models.PestCatalog.id == models.PestObservation.pest_id
    ).group_by(
        models.PestCatalog.common_name, models.PestCatalog.code
    ).order_by(desc("detections")).limit(10).all()

    pest_stats = [
        {"name": p.common_name, "code": p.code, "detections": p.detections, "alerts": p.alerts or 0}
        for p in top_pests
    ]

    # Recent alerts (last 20 threshold exceeded)
    recent_alerts = db.query(
        models.PestObservation, models.ScoutingVisit, models.PestCatalog
    ).join(
        models.ScoutingVisit, models.PestObservation.visit_id == models.ScoutingVisit.id
    ).join(
        models.PestCatalog, models.PestObservation.pest_id == models.PestCatalog.id
    ).filter(
        models.PestObservation.threshold_exceeded == True
    ).order_by(desc(models.ScoutingVisit.visit_date)).limit(20).all()

    alerts_list = []
    for obs, visit, pest in recent_alerts:
        campo = db.query(models.Campo).filter(models.Campo.id_campo == visit.campo_id).first()
        alerts_list.append({
            "visit_code": visit.visit_code,
            "visit_date": visit.visit_date.isoformat() if visit.visit_date else None,
            "campo": campo.nombre if campo else visit.campo_id,
            "pest_name": pest.common_name,
            "pest_code": pest.code,
            "average_per_unit": obs.average_per_unit,
            "threshold": pest.action_threshold,
            "threshold_unit": pest.threshold_unit,
            "severity": obs.severity_scale,
            "recommended_action": obs.recommended_action,
        })

    return {
        "visits_month": visits_month,
        "alerts_count": alerts_count,
        "active_traps": active_traps,
        "sprays_month": sprays_month,
        "pest_stats": pest_stats,
        "recent_alerts": alerts_list,
    }


# ══════════════════════════════════════════════════════════════════════════════
# SEED — Catálogo de plagas/enfermedades del Manual de Aguacate Hass (FHIA 2008)
# ══════════════════════════════════════════════════════════════════════════════

MANUAL_PEST_CATALOG = [
    # ── PLAGAS (insectos y ácaros) ──
    dict(code="PL-001", common_name="Trips del aguacate", scientific_name="Frankliniella spp.",
         category="INSECTO", pest_type="plaga", importance="primaria",
         monitoring_method="visual_conteo", sample_unit="brote", monitoring_frequency_days=7,
         action_threshold=5, threshold_unit="individuos/brote",
         threshold_description="Más de 5 trips por brote en muestreo de 20 brotes",
         affects_leaves=True, affects_fruit=True, affects_roots=False, affects_trunk=False,
         peak_months="1,2,3,11,12"),
    dict(code="PL-002", common_name="Perforador del fruto", scientific_name="Stenoma catenifer",
         category="INSECTO", pest_type="plaga", importance="primaria",
         monitoring_method="visual_conteo", sample_unit="fruto", monitoring_frequency_days=7,
         action_threshold=2, threshold_unit="% frutos afectados",
         threshold_description="Más del 2% de frutos con perforaciones",
         affects_leaves=False, affects_fruit=True, affects_roots=False, affects_trunk=False,
         peak_months="6,7,8,9,10"),
    dict(code="PL-003", common_name="Araña blanca cristalina", scientific_name="Oligonychus perseae",
         category="ACARO", pest_type="plaga", importance="primaria",
         monitoring_method="visual_conteo", sample_unit="hoja", monitoring_frequency_days=7,
         action_threshold=10, threshold_unit="colonias/hoja",
         threshold_description="Más de 10 colonias por hoja; defoliación visible",
         affects_leaves=True, affects_fruit=False, affects_roots=False, affects_trunk=False,
         peak_months="1,2,3,4,11,12"),
    dict(code="PL-004", common_name="Barrenador de ramas", scientific_name="Copturus aguacatae",
         category="INSECTO", pest_type="plaga", importance="secundaria",
         monitoring_method="visual_conteo", sample_unit="arbol", monitoring_frequency_days=14,
         action_threshold=1, threshold_unit="ramas afectadas/árbol",
         threshold_description="Presencia de aserrín blanco y galerías en ramas",
         affects_leaves=False, affects_fruit=False, affects_roots=False, affects_trunk=True,
         peak_months="5,6,7,8"),
    dict(code="PL-005", common_name="Barrenador del hueso", scientific_name="Conotrachelus perseae",
         category="INSECTO", pest_type="plaga", importance="primaria",
         monitoring_method="visual_conteo", sample_unit="fruto", monitoring_frequency_days=7,
         action_threshold=1, threshold_unit="% frutos afectados",
         threshold_description="Presencia de larvas en la semilla del fruto",
         affects_leaves=False, affects_fruit=True, affects_roots=False, affects_trunk=False,
         peak_months="7,8,9,10"),
    dict(code="PL-006", common_name="Taladrador del tronco", scientific_name="Copturomimus persea",
         category="INSECTO", pest_type="plaga", importance="primaria",
         monitoring_method="visual_conteo", sample_unit="arbol", monitoring_frequency_days=14,
         action_threshold=1, threshold_unit="perforaciones/árbol",
         threshold_description="Aserrín blanco fuera del orificio en tronco; puede matar el árbol",
         affects_leaves=False, affects_fruit=False, affects_roots=False, affects_trunk=True,
         peak_months="3,4,5,6"),
    dict(code="PL-007", common_name="Mosca blanca", scientific_name="Tetraleurodes spp.",
         category="INSECTO", pest_type="plaga", importance="secundaria",
         monitoring_method="visual_conteo", sample_unit="hoja", monitoring_frequency_days=7,
         action_threshold=5, threshold_unit="ninfas/hoja",
         threshold_description="Ninfas en envés de hojas tiernas; secreción de mielecilla y fumagina",
         affects_leaves=True, affects_fruit=True, affects_roots=False, affects_trunk=False,
         peak_months="1,2,3,4,5,11,12"),
    dict(code="PL-008", common_name="Gusano arrollador", scientific_name="Platynota sp.",
         category="INSECTO", pest_type="plaga", importance="secundaria",
         monitoring_method="visual_conteo", sample_unit="arbol", monitoring_frequency_days=14,
         action_threshold=3, threshold_unit="hojas enrolladas/árbol",
         threshold_description="Larvas enrollan hojas tiernas y brotes, roen la epidermis del fruto",
         affects_leaves=True, affects_fruit=True, affects_roots=False, affects_trunk=False,
         peak_months="6,7,8,9"),
    dict(code="PL-009", common_name="Chinche del aguacate", scientific_name="Monalonion velezangeli",
         category="INSECTO", pest_type="plaga", importance="secundaria",
         monitoring_method="visual_conteo", sample_unit="arbol", monitoring_frequency_days=14,
         action_threshold=2, threshold_unit="individuos/árbol",
         threshold_description="Perforan frutos jóvenes causando deformaciones y caída prematura",
         affects_leaves=False, affects_fruit=True, affects_roots=False, affects_trunk=False,
         peak_months="5,6,7,8"),
    dict(code="PL-010", common_name="Escamas", scientific_name="Coccus hesperidum / Protopulvinaria spp.",
         category="INSECTO", pest_type="plaga", importance="secundaria",
         monitoring_method="visual_conteo", sample_unit="hoja", monitoring_frequency_days=14,
         action_threshold=5, threshold_unit="escamas/hoja",
         threshold_description="Succionan savia; debilitan la planta y favorecen fumagina",
         affects_leaves=True, affects_fruit=True, affects_roots=False, affects_trunk=True,
         peak_months="1,2,3,4,11,12"),
    dict(code="PL-011", common_name="Agalla de hoja", scientific_name="Trioza anceps",
         category="INSECTO", pest_type="plaga", importance="secundaria",
         monitoring_method="visual_conteo", sample_unit="hoja", monitoring_frequency_days=14,
         action_threshold=10, threshold_unit="agallas/hoja",
         threshold_description="Cápsulas en cara superior de la hoja, de verde a parduzco",
         affects_leaves=True, affects_fruit=False, affects_roots=False, affects_trunk=False,
         peak_months="6,7,8,9"),
    dict(code="PL-012", common_name="Arañuela roja", scientific_name="Tetranichus telarius",
         category="ACARO", pest_type="plaga", importance="secundaria",
         monitoring_method="visual_conteo", sample_unit="hoja", monitoring_frequency_days=7,
         action_threshold=5, threshold_unit="ácaros/hoja",
         threshold_description="Teje tela fina; puntos amarillo-rojizo que se tornan café oscuro",
         affects_leaves=True, affects_fruit=False, affects_roots=False, affects_trunk=False,
         peak_months="1,2,3,4,11,12"),
    dict(code="PL-013", common_name="Pulgones", scientific_name="Aphis gossypi",
         category="INSECTO", pest_type="plaga", importance="secundaria",
         monitoring_method="visual_conteo", sample_unit="brote", monitoring_frequency_days=7,
         action_threshold=10, threshold_unit="individuos/brote",
         threshold_description="Pulgón verde en brotes y hojas; frecuente en vivero",
         affects_leaves=True, affects_fruit=False, affects_roots=False, affects_trunk=False,
         peak_months="5,6,7,8"),
    dict(code="PL-014", common_name="Gusano telarañero", scientific_name="Gracilaria perseae",
         category="INSECTO", pest_type="plaga", importance="secundaria",
         monitoring_method="visual_conteo", sample_unit="arbol", monitoring_frequency_days=14,
         action_threshold=3, threshold_unit="hojas con telaraña/árbol",
         threshold_description="Palomilla que produce telaraña enrollando hojas; roe tejidos",
         affects_leaves=True, affects_fruit=True, affects_roots=False, affects_trunk=False,
         peak_months="6,7,8,9"),
    dict(code="PL-015", common_name="Gusano confetti", scientific_name="Phyrropyge chalybea",
         category="INSECTO", pest_type="plaga", importance="secundaria",
         monitoring_method="visual_conteo", sample_unit="arbol", monitoring_frequency_days=14,
         action_threshold=5, threshold_unit="hojas perforadas/árbol",
         threshold_description="Perforaciones en todo el follaje; defoliación en ataques intensos",
         affects_leaves=True, affects_fruit=False, affects_roots=False, affects_trunk=False,
         peak_months="6,7,8,9"),

    # ── ENFERMEDADES (hongos) ──
    dict(code="EN-001", common_name="Pudrición de raíz (Phytophthora)", scientific_name="Phytophthora cinnamomi",
         category="HONGO", pest_type="enfermedad", importance="primaria",
         monitoring_method="visual_sintomas", sample_unit="arbol", monitoring_frequency_days=14,
         action_threshold=1, threshold_unit="árboles con síntomas",
         threshold_description="Follaje verde claro/amarillento, marchitez progresiva, raíces necróticas negras",
         affects_leaves=True, affects_fruit=False, affects_roots=True, affects_trunk=False,
         peak_months="6,7,8,9,10"),
    dict(code="EN-002", common_name="Pudrición por Armillaria", scientific_name="Armillaria mellea",
         category="HONGO", pest_type="enfermedad", importance="secundaria",
         monitoring_method="visual_sintomas", sample_unit="arbol", monitoring_frequency_days=14,
         action_threshold=1, threshold_unit="árboles con síntomas",
         threshold_description="Similar a Phytophthora; micelio blanco entre corteza y madera",
         affects_leaves=True, affects_fruit=False, affects_roots=True, affects_trunk=True,
         peak_months="6,7,8,9,10"),
    dict(code="EN-003", common_name="Pudrición por Rosellinia", scientific_name="Rosellinia necatrix / R. bunodes",
         category="HONGO", pest_type="enfermedad", importance="secundaria",
         monitoring_method="visual_sintomas", sample_unit="arbol", monitoring_frequency_days=14,
         action_threshold=1, threshold_unit="árboles con síntomas",
         threshold_description="Clorosis general, rizomorfos blancos→negros sobre raíces principales",
         affects_leaves=True, affects_fruit=False, affects_roots=True, affects_trunk=False,
         peak_months="6,7,8,9,10"),
    dict(code="EN-004", common_name="Antracnosis", scientific_name="Colletotrichum gloeosporioides",
         category="HONGO", pest_type="enfermedad", importance="primaria",
         monitoring_method="visual_sintomas", sample_unit="fruto", monitoring_frequency_days=7,
         action_threshold=2, threshold_unit="% frutos con lesiones",
         threshold_description="Manchas oscuras circulares en frutos; muerte de inflorescencias en periodos húmedos",
         affects_leaves=True, affects_fruit=True, affects_roots=False, affects_trunk=True,
         peak_months="6,7,8,9,10"),
    dict(code="EN-005", common_name="Marchitamiento por Verticillium", scientific_name="Verticillium sp.",
         category="HONGO", pest_type="enfermedad", importance="primaria",
         monitoring_method="visual_sintomas", sample_unit="arbol", monitoring_frequency_days=14,
         action_threshold=1, threshold_unit="árboles con síntomas",
         threshold_description="Marchitez súbita total o parcial; hojas quedan prendidas después de morir",
         affects_leaves=True, affects_fruit=False, affects_roots=False, affects_trunk=True,
         peak_months="3,4,5,6,7"),
    dict(code="EN-006", common_name="Cancro del tallo", scientific_name="Phytophthora boehmeriae",
         category="HONGO", pest_type="enfermedad", importance="secundaria",
         monitoring_method="visual_sintomas", sample_unit="arbol", monitoring_frequency_days=14,
         action_threshold=1, threshold_unit="árboles con lesiones",
         threshold_description="Mancha necrótica café oscuro en corteza; exudación con olor a vinagre",
         affects_leaves=False, affects_fruit=False, affects_roots=False, affects_trunk=True,
         peak_months="6,7,8,9,10"),
    dict(code="EN-007", common_name="Sarna o Roña", scientific_name="Sphaceloma perseae",
         category="HONGO", pest_type="enfermedad", importance="primaria",
         monitoring_method="visual_sintomas", sample_unit="fruto", monitoring_frequency_days=7,
         action_threshold=5, threshold_unit="% frutos con lesiones",
         threshold_description="Lesiones corchosas café claro en frutos; daño estético reduce calidad comercial",
         affects_leaves=True, affects_fruit=True, affects_roots=False, affects_trunk=False,
         peak_months="6,7,8,9"),
    dict(code="EN-008", common_name="Cercosporiosis", scientific_name="Cercospora purpurea",
         category="HONGO", pest_type="enfermedad", importance="secundaria",
         monitoring_method="visual_sintomas", sample_unit="hoja", monitoring_frequency_days=7,
         action_threshold=10, threshold_unit="% hojas con manchas",
         threshold_description="Manchas redondas o angulares café en hojas y frutos; agrietamiento irregular",
         affects_leaves=True, affects_fruit=True, affects_roots=False, affects_trunk=False,
         peak_months="6,7,8,9,10"),
    dict(code="EN-009", common_name="Oidio / Mildeu polvoso", scientific_name="Oidium perseae",
         category="HONGO", pest_type="enfermedad", importance="secundaria",
         monitoring_method="visual_sintomas", sample_unit="arbol", monitoring_frequency_days=7,
         action_threshold=5, threshold_unit="% follaje con micelio",
         threshold_description="Micelio blanco/grisáceo sobre hojas y racimos de flores; época de poca lluvia",
         affects_leaves=True, affects_fruit=True, affects_roots=False, affects_trunk=False,
         peak_months="1,2,3,4,11,12"),
    dict(code="EN-010", common_name="Fusariosis", scientific_name="Fusarium sp.",
         category="HONGO", pest_type="enfermedad", importance="secundaria",
         monitoring_method="visual_sintomas", sample_unit="arbol", monitoring_frequency_days=14,
         action_threshold=1, threshold_unit="árboles con síntomas",
         threshold_description="Marchitamiento vascular; necrosis interna del tallo",
         affects_leaves=True, affects_fruit=False, affects_roots=True, affects_trunk=True,
         peak_months="6,7,8,9,10"),
    dict(code="EN-014", common_name="Fumagina", scientific_name="Capnodium sp.",
         category="HONGO", pest_type="enfermedad", importance="secundaria",
         monitoring_method="visual_sintomas", sample_unit="arbol", monitoring_frequency_days=14,
         action_threshold=10, threshold_unit="% follaje cubierto",
         threshold_description="Costra negra sobre hojas tallos y frutos; asociada a secreciones de mosca blanca y escamas",
         affects_leaves=True, affects_fruit=True, affects_roots=False, affects_trunk=True,
         peak_months="1,2,3,4,5,11,12"),
    dict(code="EN-015", common_name="Mancha algal", scientific_name="Cephaleuros virescens",
         category="HONGO", pest_type="enfermedad", importance="secundaria",
         monitoring_method="visual_sintomas", sample_unit="arbol", monitoring_frequency_days=14,
         action_threshold=15, threshold_unit="% hojas con manchas",
         threshold_description="Manchas anaranjadas o verdosas aterciopeladas en hojas y tallos",
         affects_leaves=True, affects_fruit=False, affects_roots=False, affects_trunk=True,
         peak_months="6,7,8,9,10"),
    dict(code="EN-016", common_name="Pudricion del corazon", scientific_name="Poria lactomarginata",
         category="HONGO", pest_type="enfermedad", importance="secundaria",
         monitoring_method="visual_sintomas", sample_unit="arbol", monitoring_frequency_days=30,
         action_threshold=1, threshold_unit="arboles con sintomas",
         threshold_description="Pudricion interna del tronco y raices mayores en arboles viejos o con heridas de poda",
         affects_leaves=False, affects_fruit=False, affects_roots=True, affects_trunk=True,
         peak_months="6,7,8,9,10"),
    dict(code="EN-017", common_name="Avocado Sunblotch (Viroide)", scientific_name="ASBVd (Avocado sunblotch viroid)",
         category="BACTERIA", pest_type="enfermedad", importance="cuarentena",
         monitoring_method="visual_sintomas", sample_unit="arbol", monitoring_frequency_days=30,
         action_threshold=1, threshold_unit="arboles con sintomas",
         threshold_description="Manchas amarillas hundidas en frutos y estrias en corteza. Sin cura; eliminar arboles afectados",
         affects_leaves=True, affects_fruit=True, affects_roots=False, affects_trunk=True,
         peak_months="1,2,3,4,5,6,7,8,9,10,11,12"),
    dict(code="EN-018", common_name="Pudricion peduncular", scientific_name="Lasiodiplodia theobromae",
         category="HONGO", pest_type="enfermedad", importance="primaria",
         monitoring_method="visual_sintomas", sample_unit="fruto", monitoring_frequency_days=7,
         action_threshold=2, threshold_unit="% frutos afectados",
         threshold_description="Pudricion oscura desde el pedunculo hacia el interior. Poscosecha; cosechar con 1-3 mm de pedunculo",
         affects_leaves=False, affects_fruit=True, affects_roots=False, affects_trunk=False,
         peak_months="8,9,10,11,12,1,2"),
    dict(code="EN-019", common_name="Marchitez por Phytophthora palmivora", scientific_name="Phytophthora palmivora",
         category="HONGO", pest_type="enfermedad", importance="primaria",
         monitoring_method="visual_sintomas", sample_unit="fruto", monitoring_frequency_days=7,
         action_threshold=2, threshold_unit="% frutos con lesiones",
         threshold_description="Ataca frutos directamente causando manchas acuosas oscuras. Favorecida por lluvias",
         affects_leaves=False, affects_fruit=True, affects_roots=True, affects_trunk=False,
         peak_months="6,7,8,9,10"),
    dict(code="EN-020", common_name="Muerte de plantulas (Damping-off)", scientific_name="Rhizoctonia solani / Corticium rolfsii",
         category="HONGO", pest_type="enfermedad", importance="secundaria",
         monitoring_method="visual_sintomas", sample_unit="arbol", monitoring_frequency_days=7,
         action_threshold=1, threshold_unit="plantulas afectadas",
         threshold_description="Pudricion del cuello de plantulas en vivero. Estrangulamiento a nivel del suelo",
         affects_leaves=False, affects_fruit=False, affects_roots=True, affects_trunk=True,
         peak_months="6,7,8,9"),
    dict(code="EN-021", common_name="Pudricion acuosa del fruto", scientific_name="Phytophthora citricola",
         category="HONGO", pest_type="enfermedad", importance="secundaria",
         monitoring_method="visual_sintomas", sample_unit="fruto", monitoring_frequency_days=7,
         action_threshold=2, threshold_unit="% frutos con lesiones",
         threshold_description="Manchas acuosas marron claro en frutos maduros cercanos al suelo",
         affects_leaves=False, affects_fruit=True, affects_roots=False, affects_trunk=False,
         peak_months="7,8,9,10"),
]


@router.post("/catalogo/seed-manual")
def seed_catalogo_from_manual(
    db: Session = Depends(get_db),
    current_user=Depends(auth.require_admin),
):
    """Poblar el catálogo con plagas/enfermedades del Manual de Aguacate Hass (FHIA 2008)."""
    created = 0
    skipped = 0
    for entry_data in MANUAL_PEST_CATALOG:
        existing = db.query(models.PestCatalog).filter(
            models.PestCatalog.code == entry_data["code"]
        ).first()
        if existing:
            skipped += 1
            continue
        entry = models.PestCatalog(**entry_data)
        db.add(entry)
        created += 1
    db.commit()
    audit.log(db, current_user, "SEED", "CATALOGO_SANIDAD", "MANUAL_FHIA",
              f"Catálogo poblado desde Manual FHIA 2008: {created} creados, {skipped} existentes")
    return {"message": f"Catálogo actualizado: {created} registros creados, {skipped} ya existían",
            "created": created, "skipped": skipped}
