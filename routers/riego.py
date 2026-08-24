"""
Router RIEGO — Humedad de suelo, eventos de irrigación, balance hídrico.
Endpoints para el módulo de riego del ERP-Finca (350 ha Hass, goteo).
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel as _Base
from sqlalchemy.orm import Session
from sqlalchemy import func, and_, cast, Date
from database import get_db
import models, auth, audit
from typing import Optional, List
from datetime import datetime, date, timedelta
from datetime import date as _date
import json, csv, io

router = APIRouter(prefix="/api/riego", tags=["riego"])


# ──────────────── date validation helper ────────────────── C1
def _parse_date(s: str, name: str) -> _date:
    try:
        return _date.fromisoformat(s)
    except (ValueError, TypeError):
        raise HTTPException(400, f"{name} formato inválido. Use YYYY-MM-DD")


# ──────────────── Pydantic models ────────────────── C3
class LecturaIn(_Base):
    campo_id: str
    reading_datetime: Optional[str] = None  # ISO format
    depth_cm: int
    value_cbar: Optional[float] = None
    visual_scale: Optional[int] = None
    reading_type: str = 'tensiometro'
    location_zone: Optional[str] = None
    position: Optional[str] = None
    sensor_id: Optional[str] = None
    notes: Optional[str] = None


class EventoIn(_Base):
    campo_id: str
    start_time: str
    end_time: Optional[str] = None
    duration_minutes: Optional[int] = None
    volume_liters: Optional[float] = None
    volume_mm: Optional[float] = None
    fertigation: bool = False
    fert_product: Optional[str] = None
    fert_dose: Optional[str] = None
    source: str = 'manual'
    notes: Optional[str] = None


# ── helpers ────────────────────────────────────────────────────────────────────

def _soil_out(r: models.SoilReading) -> dict:
    return {
        "id": r.id,
        "campo_id": r.campo_id,
        "reading_datetime": r.reading_datetime.isoformat() if r.reading_datetime else None,
        "depth_cm": r.depth_cm,
        "value_cbar": r.value_cbar,
        "visual_scale": r.visual_scale,
        "reading_type": r.reading_type,
        "location_zone": r.location_zone,
        "position": r.position,
        "recorded_by": r.recorded_by,
        "sensor_id": r.sensor_id,
        "notes": r.notes,
        "creado_en": r.creado_en.isoformat() if r.creado_en else None,
    }


def _event_out(e: models.IrrigationEvent) -> dict:
    return {
        "id": e.id,
        "campo_id": e.campo_id,
        "start_time": e.start_time.isoformat() if e.start_time else None,
        "end_time": e.end_time.isoformat() if e.end_time else None,
        "duration_minutes": e.duration_minutes,
        "volume_liters": e.volume_liters,
        "volume_mm": e.volume_mm,
        "fertigation": e.fertigation,
        "fert_product": e.fert_product,
        "fert_dose": e.fert_dose,
        "source": e.source,
        "recorded_by": e.recorded_by,
        "notes": e.notes,
        "creado_en": e.creado_en.isoformat() if e.creado_en else None,
    }


def _balance_out(b: models.DailyWaterBalance) -> dict:
    return {
        "id": b.id,
        "campo_id": b.campo_id,
        "date": b.date.isoformat() if b.date else None,
        "et0_mm": b.et0_mm,
        "kc": b.kc,
        "etc_mm": b.etc_mm,
        "rainfall_mm": b.rainfall_mm,
        "irrigation_mm": b.irrigation_mm,
        "balance_mm": b.balance_mm,
        "cumulative_deficit_mm": b.cumulative_deficit_mm,
        "status": b.status,
    }


def _config_out(c: models.FieldIrrigationConfig) -> dict:
    kc = {}
    if c.kc_by_stage:
        try:
            kc = json.loads(c.kc_by_stage)
        except (json.JSONDecodeError, TypeError):
            kc = {}
    return {
        "id": c.id,
        "campo_id": c.campo_id,
        "irrigation_type": c.irrigation_type,
        "emitters_per_tree": c.emitters_per_tree,
        "emitter_flow_lph": c.emitter_flow_lph,
        "field_capacity_cbar": c.field_capacity_cbar,
        "refill_point_cbar": c.refill_point_cbar,
        "stress_point_cbar": c.stress_point_cbar,
        "waterlog_point_cbar": c.waterlog_point_cbar,
        "kc_by_stage": kc,
        "notes": c.notes,
    }


def _reading_status(value_cbar: Optional[float], visual_scale: Optional[int],
                     config: Optional[models.FieldIrrigationConfig]) -> str:
    """Determine soil moisture status from reading + config thresholds."""
    if visual_scale is not None:
        if visual_scale <= 1:
            return "saturado"
        elif visual_scale <= 2:
            return "optimo"
        elif visual_scale <= 3:
            return "optimo"
        elif visual_scale <= 4:
            return "deficit"
        else:
            return "estres"
    if value_cbar is not None:
        wl = config.waterlog_point_cbar if config else 5
        fc = config.field_capacity_cbar if config else 10
        rp = config.refill_point_cbar if config else 25
        sp = config.stress_point_cbar if config else 40
        if value_cbar <= wl:
            return "saturado"
        elif value_cbar <= fc:
            return "optimo"
        elif value_cbar <= rp:
            return "optimo"
        elif value_cbar <= sp:
            return "deficit"
        else:
            return "estres"
    return "sin_dato"


# ══════════════════════════════════════════════════════════════════════════════
# 1-2. LECTURAS DE SUELO
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/lecturas")
def list_lecturas(
    campo_id: Optional[str] = None,
    fecha_desde: Optional[str] = None,
    fecha_hasta: Optional[str] = None,
    depth_cm: Optional[int] = None,
    limit: int = Query(200, le=1000),
    db: Session = Depends(get_db),
    _=Depends(auth.get_current_user),
):
    q = db.query(models.SoilReading)
    if campo_id:
        q = q.filter(models.SoilReading.campo_id == campo_id)
    if depth_cm:
        q = q.filter(models.SoilReading.depth_cm == depth_cm)
    if fecha_desde:
        d_desde = _parse_date(fecha_desde, "fecha_desde")  # C1
        q = q.filter(models.SoilReading.reading_datetime >= d_desde)
    if fecha_hasta:
        d_hasta = _parse_date(fecha_hasta, "fecha_hasta")  # C1
        q = q.filter(models.SoilReading.reading_datetime <= d_hasta)
    rows = q.order_by(models.SoilReading.reading_datetime.desc()).limit(limit).all()
    return [_soil_out(r) for r in rows]


@router.post("/lecturas")
def create_lectura(
    data: LecturaIn,  # C3: Pydantic model instead of dict
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user),
):
    campo_id = data.campo_id
    campo = db.query(models.Campo).filter(models.Campo.id_campo == campo_id).first()
    if not campo:
        raise HTTPException(404, f"Campo {campo_id} no existe")

    # C4/C5: Pydantic handles type coercion; depth_cm is already int
    if data.depth_cm <= 0:
        raise HTTPException(400, "depth_cm debe ser > 0")

    if data.visual_scale is not None and data.visual_scale not in range(1, 6):
        raise HTTPException(400, "visual_scale debe estar entre 1 y 5")

    # C6: negative value_cbar validation
    if data.value_cbar is not None and data.value_cbar < 0:
        raise HTTPException(400, "value_cbar no puede ser negativo")

    if data.reading_type == "sensor" and not data.sensor_id:
        raise HTTPException(400, "sensor_id es requerido para lecturas tipo sensor")

    reading_dt = data.reading_datetime or datetime.now().isoformat()

    r = models.SoilReading(
        campo_id=campo_id,
        reading_datetime=reading_dt,
        depth_cm=data.depth_cm,
        value_cbar=data.value_cbar,
        visual_scale=data.visual_scale,
        reading_type=data.reading_type,
        location_zone=data.location_zone,
        position=data.position,
        recorded_by=user.id,
        sensor_id=data.sensor_id,
        notes=data.notes,
    )
    db.add(r)
    db.flush()
    # H1: audit trail
    audit.log(db, user, 'CREAR', 'LECTURA_SUELO', str(r.id),
              f"Lectura suelo campo {campo_id} depth={data.depth_cm}cm")
    db.commit()
    db.refresh(r)
    return _soil_out(r)


@router.put("/lecturas/{lectura_id}")
def update_lectura(
    lectura_id: int, data: LecturaIn,
    db: Session = Depends(get_db), user=Depends(auth.get_current_user),
):
    r = db.query(models.SoilReading).get(lectura_id)
    if not r:
        raise HTTPException(404, "Lectura no encontrada")
    if data.depth_cm <= 0:
        raise HTTPException(400, "depth_cm debe ser > 0")
    if data.visual_scale is not None and data.visual_scale not in range(1, 6):
        raise HTTPException(400, "visual_scale debe estar entre 1 y 5")
    if data.value_cbar is not None and data.value_cbar < 0:
        raise HTTPException(400, "value_cbar no puede ser negativo")
    if data.reading_type == "sensor" and not data.sensor_id:
        raise HTTPException(400, "sensor_id es requerido para lecturas tipo sensor")
    r.campo_id = data.campo_id
    r.reading_datetime = data.reading_datetime or r.reading_datetime
    r.depth_cm = data.depth_cm
    r.value_cbar = data.value_cbar
    r.visual_scale = data.visual_scale
    r.reading_type = data.reading_type
    r.location_zone = data.location_zone
    r.position = data.position
    r.sensor_id = data.sensor_id
    r.notes = data.notes
    audit.log(db, user, 'ACTUALIZAR', 'LECTURA_SUELO', str(r.id),
              f"Lectura suelo {r.id} actualizada")
    db.commit()
    db.refresh(r)
    return _soil_out(r)


@router.delete("/lecturas/{lectura_id}")
def delete_lectura(
    lectura_id: int,
    db: Session = Depends(get_db), user=Depends(auth.require_supervisor),
):
    r = db.query(models.SoilReading).get(lectura_id)
    if not r:
        raise HTTPException(404, "Lectura no encontrada")
    audit.log(db, user, 'ELIMINAR', 'LECTURA_SUELO', str(r.id),
              f"Lectura suelo {r.id} eliminada campo={r.campo_id}")
    db.delete(r)
    db.commit()
    return {"ok": True}


# ── 3. RESUMEN POR CAMPO ──────────────────────────────────────────────────────

@router.get("/lecturas/resumen")
def lecturas_resumen(
    db: Session = Depends(get_db),
    _=Depends(auth.get_current_user),
):
    """Latest reading per campo + status badge using config thresholds."""
    campos = db.query(models.Campo).filter(models.Campo.activo == True).order_by(models.Campo.id_campo).all()
    configs = {c.campo_id: c for c in db.query(models.FieldIrrigationConfig).all()}

    result = []
    for campo in campos:
        latest = (
            db.query(models.SoilReading)
            .filter(models.SoilReading.campo_id == campo.id_campo)
            .order_by(models.SoilReading.reading_datetime.desc())
            .first()
        )
        cfg = configs.get(campo.id_campo)
        if latest:
            status = _reading_status(latest.value_cbar, latest.visual_scale, cfg)
            result.append({
                "campo_id": campo.id_campo,
                "campo_nombre": campo.nombre,
                "area_ha": campo.area_ha,
                "latest_value_cbar": latest.value_cbar,
                "latest_visual_scale": latest.visual_scale,
                "latest_depth_cm": latest.depth_cm,
                "latest_reading_type": latest.reading_type,
                "latest_datetime": latest.reading_datetime.isoformat() if latest.reading_datetime else None,
                "status": status,
                "has_config": cfg is not None,
            })
        else:
            result.append({
                "campo_id": campo.id_campo,
                "campo_nombre": campo.nombre,
                "area_ha": campo.area_ha,
                "latest_value_cbar": None,
                "latest_visual_scale": None,
                "latest_depth_cm": None,
                "latest_reading_type": None,
                "latest_datetime": None,
                "status": "sin_dato",
                "has_config": cfg is not None,
            })
    return result


# ══════════════════════════════════════════════════════════════════════════════
# 4-5. EVENTOS DE RIEGO
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/eventos")
def list_eventos(
    campo_id: Optional[str] = None,
    fecha_desde: Optional[str] = None,
    fecha_hasta: Optional[str] = None,
    limit: int = Query(200, le=1000),
    db: Session = Depends(get_db),
    _=Depends(auth.get_current_user),
):
    q = db.query(models.IrrigationEvent)
    if campo_id:
        q = q.filter(models.IrrigationEvent.campo_id == campo_id)
    if fecha_desde:
        d_desde = _parse_date(fecha_desde, "fecha_desde")  # C1
        q = q.filter(models.IrrigationEvent.start_time >= d_desde)
    if fecha_hasta:
        d_hasta = _parse_date(fecha_hasta, "fecha_hasta")  # C1
        q = q.filter(models.IrrigationEvent.start_time <= d_hasta)
    rows = q.order_by(models.IrrigationEvent.start_time.desc()).limit(limit).all()
    return [_event_out(e) for e in rows]


@router.post("/eventos")
def create_evento(
    data: EventoIn,  # C3: Pydantic model instead of dict
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user),
):
    campo_id = data.campo_id
    campo = db.query(models.Campo).filter(models.Campo.id_campo == campo_id).first()
    if not campo:
        raise HTTPException(404, f"Campo {campo_id} no existe")

    start_time = data.start_time

    volume_liters = data.volume_liters
    volume_mm = data.volume_mm

    # Auto-calculate volume_mm if volume_liters provided and campo has area
    # M8: guard division by zero on area_ha
    if volume_liters and not volume_mm and campo.area_ha and campo.area_ha > 0:
        volume_mm = round(volume_liters / (campo.area_ha * 10000), 4)

    # Calculate duration if both start and end provided
    duration = data.duration_minutes
    end_time = data.end_time
    if end_time and start_time and not duration:
        try:
            dt_start = datetime.fromisoformat(start_time)
            dt_end = datetime.fromisoformat(end_time)
            duration = int((dt_end - dt_start).total_seconds() / 60)
        except (ValueError, TypeError):
            pass

    ev = models.IrrigationEvent(
        campo_id=campo_id,
        start_time=start_time,
        end_time=end_time,
        duration_minutes=int(duration) if duration else None,
        volume_liters=volume_liters,
        volume_mm=volume_mm,
        fertigation=data.fertigation,
        fert_product=data.fert_product,
        fert_dose=data.fert_dose,
        source=data.source,
        recorded_by=user.id,
        notes=data.notes,
    )
    db.add(ev)
    db.flush()
    # H1: audit trail
    audit.log(db, user, 'CREAR', 'EVENTO_RIEGO', str(ev.id),
              f"Riego campo {campo_id} inicio={start_time}")
    db.commit()
    db.refresh(ev)
    return _event_out(ev)


@router.put("/eventos/{evento_id}")
def update_evento(
    evento_id: int, data: EventoIn,
    db: Session = Depends(get_db), user=Depends(auth.get_current_user),
):
    ev = db.query(models.IrrigationEvent).get(evento_id)
    if not ev:
        raise HTTPException(404, "Evento no encontrado")
    campo = db.query(models.Campo).filter(models.Campo.id_campo == data.campo_id).first()
    if not campo:
        raise HTTPException(404, f"Campo {data.campo_id} no existe")
    ev.campo_id = data.campo_id
    ev.start_time = data.start_time
    ev.end_time = data.end_time
    ev.duration_minutes = data.duration_minutes
    ev.volume_liters = data.volume_liters
    ev.volume_mm = data.volume_mm
    if data.volume_liters and not data.volume_mm and campo.area_ha and campo.area_ha > 0:
        ev.volume_mm = round(data.volume_liters / (campo.area_ha * 10000), 4)
    ev.fertigation = data.fertigation
    ev.fert_product = data.fert_product
    ev.fert_dose = data.fert_dose
    ev.source = data.source
    ev.notes = data.notes
    audit.log(db, user, 'ACTUALIZAR', 'EVENTO_RIEGO', str(ev.id),
              f"Evento riego {ev.id} actualizado")
    db.commit()
    db.refresh(ev)
    return _event_out(ev)


@router.delete("/eventos/{evento_id}")
def delete_evento(
    evento_id: int,
    db: Session = Depends(get_db), user=Depends(auth.require_supervisor),
):
    ev = db.query(models.IrrigationEvent).get(evento_id)
    if not ev:
        raise HTTPException(404, "Evento no encontrado")
    audit.log(db, user, 'ELIMINAR', 'EVENTO_RIEGO', str(ev.id),
              f"Evento riego {ev.id} eliminado campo={ev.campo_id}")
    db.delete(ev)
    db.commit()
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════════
# 6-7. BALANCE HÍDRICO
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/balance")
def list_balance(
    campo_id: Optional[str] = None,
    fecha_desde: Optional[str] = None,
    fecha_hasta: Optional[str] = None,
    db: Session = Depends(get_db),
    _=Depends(auth.get_current_user),
):
    q = db.query(models.DailyWaterBalance)
    if campo_id:
        q = q.filter(models.DailyWaterBalance.campo_id == campo_id)
    if fecha_desde:
        d_desde = _parse_date(fecha_desde, "fecha_desde")  # C1
        q = q.filter(models.DailyWaterBalance.date >= d_desde)
    if fecha_hasta:
        d_hasta = _parse_date(fecha_hasta, "fecha_hasta")  # C1
        q = q.filter(models.DailyWaterBalance.date <= d_hasta)
    rows = q.order_by(models.DailyWaterBalance.date.desc()).limit(365).all()
    return [_balance_out(b) for b in rows]


@router.post("/balance/calcular")
def calcular_balance(
    data: dict,
    db: Session = Depends(get_db),
    current_user=Depends(auth.require_supervisor),
):
    """Calculate daily water balance for a campo over a date range. Upserts into DailyWaterBalance."""
    campo_id = data.get("campo_id")
    fecha_desde = data.get("fecha_desde")
    fecha_hasta = data.get("fecha_hasta")
    if not campo_id or not fecha_desde or not fecha_hasta:
        raise HTTPException(400, "campo_id, fecha_desde y fecha_hasta son requeridos")

    # C1: validate dates
    d_start = _parse_date(fecha_desde, "fecha_desde") if isinstance(fecha_desde, str) else fecha_desde
    d_end = _parse_date(fecha_hasta, "fecha_hasta") if isinstance(fecha_hasta, str) else fecha_hasta

    campo = db.query(models.Campo).filter(models.Campo.id_campo == campo_id).first()
    if not campo:
        raise HTTPException(404, f"Campo {campo_id} no existe")

    config = db.query(models.FieldIrrigationConfig).filter(
        models.FieldIrrigationConfig.campo_id == campo_id
    ).first()

    # Get Kc — from config or default
    default_kc = 0.80
    kc_map = {}
    if config and config.kc_by_stage:
        try:
            kc_map = json.loads(config.kc_by_stage)
        except (json.JSONDecodeError, TypeError):
            pass
    # Use campo.etapa to pick the right Kc, or fallback
    kc_value = default_kc
    if campo.etapa and kc_map:
        kc_value = kc_map.get(campo.etapa, default_kc)

    cumulative = 0.0
    results = []
    current = d_start
    while current <= d_end:
        day_start = datetime.combine(current, datetime.min.time())
        day_end = datetime.combine(current, datetime.max.time())

        # ET₀ from WeatherDailySummary
        weather = db.query(models.WeatherDailySummary).filter(
            cast(models.WeatherDailySummary.date, Date) == current
        ).first()
        et0 = weather.et0_mm if weather and weather.et0_mm else 0.0

        # ETc
        etc = round(et0 * kc_value, 2)

        # Rainfall
        rainfall = weather.rainfall_total_mm if weather and weather.rainfall_total_mm else 0.0

        # Irrigation total for this campo on this day
        irrig_sum = db.query(func.coalesce(func.sum(models.IrrigationEvent.volume_mm), 0)).filter(
            models.IrrigationEvent.campo_id == campo_id,
            models.IrrigationEvent.start_time >= day_start,
            models.IrrigationEvent.start_time <= day_end,
        ).scalar()
        irrigation_mm = float(irrig_sum) if irrig_sum else 0.0

        balance = round(rainfall + irrigation_mm - etc, 2)
        cumulative = round(cumulative + balance, 2)

        status = "adecuado"
        if cumulative < -5:
            status = "deficit"
        elif cumulative > 10:
            status = "exceso"

        # Upsert
        existing = db.query(models.DailyWaterBalance).filter(
            models.DailyWaterBalance.campo_id == campo_id,
            cast(models.DailyWaterBalance.date, Date) == current,
        ).first()

        if existing:
            existing.et0_mm = et0
            existing.kc = kc_value
            existing.etc_mm = etc
            existing.rainfall_mm = rainfall
            existing.irrigation_mm = irrigation_mm
            existing.balance_mm = balance
            existing.cumulative_deficit_mm = cumulative
            existing.status = status
            rec = existing
        else:
            rec = models.DailyWaterBalance(
                campo_id=campo_id,
                date=day_start,
                et0_mm=et0,
                kc=kc_value,
                etc_mm=etc,
                rainfall_mm=rainfall,
                irrigation_mm=irrigation_mm,
                balance_mm=balance,
                cumulative_deficit_mm=cumulative,
                status=status,
            )
            db.add(rec)

        results.append(_balance_out(rec))
        current += timedelta(days=1)

    # H1: audit trail
    audit.log(db, current_user, 'CREAR', 'BALANCE', str(campo_id),
              f"Balance hídrico {fecha_desde}→{fecha_hasta} campo {campo_id}")
    db.commit()
    return {"rows_calculated": len(results), "data": results}


# ══════════════════════════════════════════════════════════════════════════════
# 8-10. CONFIGURACIÓN DE RIEGO POR CAMPO
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/config")
def list_configs(
    db: Session = Depends(get_db),
    _=Depends(auth.get_current_user),
):
    rows = db.query(models.FieldIrrigationConfig).order_by(models.FieldIrrigationConfig.campo_id).all()
    return [_config_out(c) for c in rows]


@router.get("/config/{campo_id}")
def get_config(
    campo_id: str,
    db: Session = Depends(get_db),
    _=Depends(auth.get_current_user),
):
    cfg = db.query(models.FieldIrrigationConfig).filter(
        models.FieldIrrigationConfig.campo_id == campo_id
    ).first()
    if not cfg:
        raise HTTPException(404, f"No hay configuración para campo {campo_id}")
    return _config_out(cfg)


@router.put("/config/{campo_id}")
def upsert_config(
    campo_id: str,
    data: dict,
    db: Session = Depends(get_db),
    current_user=Depends(auth.require_supervisor),
):
    campo = db.query(models.Campo).filter(models.Campo.id_campo == campo_id).first()
    if not campo:
        raise HTTPException(404, f"Campo {campo_id} no existe")

    cfg = db.query(models.FieldIrrigationConfig).filter(
        models.FieldIrrigationConfig.campo_id == campo_id
    ).first()

    kc_stage = data.get("kc_by_stage")
    kc_json = json.dumps(kc_stage) if isinstance(kc_stage, dict) else kc_stage

    is_new = cfg is None
    if cfg:
        cfg.irrigation_type = data.get("irrigation_type", cfg.irrigation_type)
        cfg.emitters_per_tree = data.get("emitters_per_tree", cfg.emitters_per_tree)
        cfg.emitter_flow_lph = data.get("emitter_flow_lph", cfg.emitter_flow_lph)
        cfg.field_capacity_cbar = data.get("field_capacity_cbar", cfg.field_capacity_cbar)
        cfg.refill_point_cbar = data.get("refill_point_cbar", cfg.refill_point_cbar)
        cfg.stress_point_cbar = data.get("stress_point_cbar", cfg.stress_point_cbar)
        cfg.waterlog_point_cbar = data.get("waterlog_point_cbar", cfg.waterlog_point_cbar)
        if kc_json is not None:
            cfg.kc_by_stage = kc_json
        cfg.notes = data.get("notes", cfg.notes)
    else:
        cfg = models.FieldIrrigationConfig(
            campo_id=campo_id,
            irrigation_type=data.get("irrigation_type", "goteo"),
            emitters_per_tree=data.get("emitters_per_tree", 4),
            emitter_flow_lph=data.get("emitter_flow_lph", 4.0),
            field_capacity_cbar=data.get("field_capacity_cbar", 10),
            refill_point_cbar=data.get("refill_point_cbar", 25),
            stress_point_cbar=data.get("stress_point_cbar", 40),
            waterlog_point_cbar=data.get("waterlog_point_cbar", 5),
            kc_by_stage=kc_json or '{"floracion":0.65,"cuaje":0.75,"llenado":0.85,"maduracion":0.78}',
            notes=data.get("notes"),
        )
        db.add(cfg)

    db.flush()
    # H1: audit trail
    action = 'CREAR' if is_new else 'ACTUALIZAR'
    audit.log(db, current_user, action, 'CONFIG_RIEGO', str(campo_id),
              f"Config riego campo {campo_id}")
    db.commit()
    db.refresh(cfg)
    return _config_out(cfg)


# ══════════════════════════════════════════════════════════════════════════════
# 11. ALERTAS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/alertas")
def alertas(
    db: Session = Depends(get_db),
    _=Depends(auth.get_current_user),
):
    """Check all campos: latest reading vs config thresholds → alerts."""
    campos = db.query(models.Campo).filter(models.Campo.activo == True).all()
    configs = {c.campo_id: c for c in db.query(models.FieldIrrigationConfig).all()}
    alerts = []

    for campo in campos:
        latest = (
            db.query(models.SoilReading)
            .filter(models.SoilReading.campo_id == campo.id_campo)
            .order_by(models.SoilReading.reading_datetime.desc())
            .first()
        )
        if not latest:
            alerts.append({
                "campo_id": campo.id_campo,
                "campo_nombre": campo.nombre,
                "reading": None,
                "threshold": None,
                "status": "sin_dato",
                "severity": "info",
                "message": "Sin lecturas de suelo registradas",
            })
            continue

        cfg = configs.get(campo.id_campo)
        status = _reading_status(latest.value_cbar, latest.visual_scale, cfg)

        severity = "ok"
        message = "Humedad adecuada"
        reading_val = latest.value_cbar if latest.value_cbar is not None else latest.visual_scale
        threshold_val = None

        if status == "saturado":
            severity = "warning"
            message = "⚠️ Suelo saturado — riesgo de asfixia radicular"
            threshold_val = cfg.waterlog_point_cbar if cfg else 5
        elif status == "deficit":
            severity = "warning"
            message = "⚠️ Déficit hídrico — programar riego"
            threshold_val = cfg.refill_point_cbar if cfg else 25
        elif status == "estres":
            severity = "critical"
            message = "🔴 Estrés hídrico severo — riego urgente"
            threshold_val = cfg.stress_point_cbar if cfg else 40

        if severity != "ok":
            alerts.append({
                "campo_id": campo.id_campo,
                "campo_nombre": campo.nombre,
                "reading": reading_val,
                "threshold": threshold_val,
                "status": status,
                "severity": severity,
                "message": message,
            })

    return alerts


# ══════════════════════════════════════════════════════════════════════════════
# 12. DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/dashboard")
def dashboard(
    db: Session = Depends(get_db),
    _=Depends(auth.get_current_user),
):
    """Overview: all campos with latest reading, status, days since irrigation, deficit, alerts."""
    campos = db.query(models.Campo).filter(models.Campo.activo == True).order_by(models.Campo.id_campo).all()
    configs = {c.campo_id: c for c in db.query(models.FieldIrrigationConfig).all()}

    overview = []
    alert_count = 0

    for campo in campos:
        # Latest soil reading
        latest_reading = (
            db.query(models.SoilReading)
            .filter(models.SoilReading.campo_id == campo.id_campo)
            .order_by(models.SoilReading.reading_datetime.desc())
            .first()
        )

        # Latest irrigation event
        latest_irrig = (
            db.query(models.IrrigationEvent)
            .filter(models.IrrigationEvent.campo_id == campo.id_campo)
            .order_by(models.IrrigationEvent.start_time.desc())
            .first()
        )

        # Latest water balance
        latest_balance = (
            db.query(models.DailyWaterBalance)
            .filter(models.DailyWaterBalance.campo_id == campo.id_campo)
            .order_by(models.DailyWaterBalance.date.desc())
            .first()
        )

        cfg = configs.get(campo.id_campo)
        status = _reading_status(
            latest_reading.value_cbar if latest_reading else None,
            latest_reading.visual_scale if latest_reading else None,
            cfg
        )

        days_since_irrig = None
        if latest_irrig and latest_irrig.start_time:
            delta = datetime.now() - latest_irrig.start_time
            days_since_irrig = delta.days

        cumulative_deficit = latest_balance.cumulative_deficit_mm if latest_balance else None

        if status in ("deficit", "estres", "saturado"):
            alert_count += 1

        overview.append({
            "campo_id": campo.id_campo,
            "campo_nombre": campo.nombre,
            "area_ha": campo.area_ha,
            "n_plantas": campo.n_plantas,
            "etapa": campo.etapa if hasattr(campo, 'etapa') else None,
            # Reading
            "value_cbar": latest_reading.value_cbar if latest_reading else None,
            "visual_scale": latest_reading.visual_scale if latest_reading else None,
            "depth_cm": latest_reading.depth_cm if latest_reading else None,
            "reading_type": latest_reading.reading_type if latest_reading else None,
            "reading_datetime": latest_reading.reading_datetime.isoformat() if latest_reading and latest_reading.reading_datetime else None,
            "status": status,
            # Irrigation
            "days_since_irrigation": days_since_irrig,
            "last_irrigation": latest_irrig.start_time.isoformat() if latest_irrig and latest_irrig.start_time else None,
            "last_volume_mm": latest_irrig.volume_mm if latest_irrig else None,
            # Balance
            "cumulative_deficit_mm": cumulative_deficit,
            "balance_status": latest_balance.status if latest_balance else None,
        })

    return {"campos": overview, "alert_count": alert_count, "total_campos": len(campos)}


# ══════════════════════════════════════════════════════════════════════════════
# 13. RECOMENDACIONES DE RIEGO
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/recomendaciones")
def recomendaciones(
    db: Session = Depends(get_db),
    _=Depends(auth.get_current_user),
):
    """Genera recomendaciones de riego basadas en balance hídrico y umbrales."""
    campos = db.query(models.Campo).filter(models.Campo.activo == True).order_by(models.Campo.id_campo).all()
    configs = {c.campo_id: c for c in db.query(models.FieldIrrigationConfig).all()}
    recs = []

    for campo in campos:
        cfg = configs.get(campo.id_campo)
        latest = (
            db.query(models.SoilReading)
            .filter(models.SoilReading.campo_id == campo.id_campo)
            .order_by(models.SoilReading.reading_datetime.desc())
            .first()
        )
        latest_bal = (
            db.query(models.DailyWaterBalance)
            .filter(models.DailyWaterBalance.campo_id == campo.id_campo)
            .order_by(models.DailyWaterBalance.date.desc())
            .first()
        )
        status = _reading_status(
            latest.value_cbar if latest else None,
            latest.visual_scale if latest else None, cfg
        )
        if status not in ("deficit", "estres"):
            continue

        deficit_mm = abs(latest_bal.cumulative_deficit_mm) if latest_bal and latest_bal.cumulative_deficit_mm else 5.0
        area_ha = float(campo.area_ha or 1)
        litros_sugeridos = round(deficit_mm * area_ha * 10000, 0)
        duracion_min = None
        if cfg and cfg.emitters_per_tree and cfg.emitter_flow_lph and campo.n_plantas:
            flow_total_lph = cfg.emitters_per_tree * cfg.emitter_flow_lph * campo.n_plantas
            if flow_total_lph > 0:
                duracion_min = round(litros_sugeridos / flow_total_lph * 60, 0)

        recs.append({
            "campo_id": campo.id_campo,
            "campo_nombre": campo.nombre,
            "status": status,
            "deficit_mm": round(deficit_mm, 2),
            "litros_sugeridos": litros_sugeridos,
            "mm_sugeridos": round(deficit_mm, 2),
            "duracion_sugerida_min": duracion_min,
            "prioridad": "urgente" if status == "estres" else "normal",
        })

    recs.sort(key=lambda r: (0 if r["prioridad"] == "urgente" else 1, -r["deficit_mm"]))
    return recs


# ══════════════════════════════════════════════════════════════════════════════
# 14. RESUMEN DE CONSUMO
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/consumo")
def consumo_resumen(
    fecha_desde: Optional[str] = None,
    fecha_hasta: Optional[str] = None,
    db: Session = Depends(get_db),
    _=Depends(auth.get_current_user),
):
    d_desde = _parse_date(fecha_desde, "fecha_desde") if fecha_desde else (date.today() - timedelta(days=30))
    d_hasta = _parse_date(fecha_hasta, "fecha_hasta") if fecha_hasta else date.today()

    campos = db.query(models.Campo).filter(models.Campo.activo == True).order_by(models.Campo.id_campo).all()
    result = []
    total_litros = 0
    total_eventos = 0

    for campo in campos:
        q = db.query(
            func.count(models.IrrigationEvent.id),
            func.coalesce(func.sum(models.IrrigationEvent.volume_liters), 0),
            func.coalesce(func.sum(models.IrrigationEvent.volume_mm), 0),
        ).filter(
            models.IrrigationEvent.campo_id == campo.id_campo,
            models.IrrigationEvent.start_time >= datetime.combine(d_desde, datetime.min.time()),
            models.IrrigationEvent.start_time <= datetime.combine(d_hasta, datetime.max.time()),
        )
        count, vol_l, vol_mm = q.one()
        vol_l = float(vol_l)
        vol_mm = float(vol_mm)
        total_litros += vol_l
        total_eventos += count
        if count > 0:
            result.append({
                "campo_id": campo.id_campo,
                "campo_nombre": campo.nombre,
                "area_ha": campo.area_ha,
                "eventos": count,
                "volumen_litros": round(vol_l, 0),
                "volumen_m3": round(vol_l / 1000, 1),
                "lamina_mm": round(vol_mm, 2),
            })

    return {
        "desde": d_desde.isoformat(),
        "hasta": d_hasta.isoformat(),
        "total_eventos": total_eventos,
        "total_litros": round(total_litros, 0),
        "total_m3": round(total_litros / 1000, 1),
        "campos": result,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 15. EXPORTACIÓN CSV
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/lecturas/exportar")
def exportar_lecturas(
    campo_id: Optional[str] = None,
    fecha_desde: Optional[str] = None,
    fecha_hasta: Optional[str] = None,
    db: Session = Depends(get_db),
    _=Depends(auth.get_current_user),
):
    q = db.query(models.SoilReading)
    if campo_id:
        q = q.filter(models.SoilReading.campo_id == campo_id)
    if fecha_desde:
        q = q.filter(models.SoilReading.reading_datetime >= _parse_date(fecha_desde, "fecha_desde"))
    if fecha_hasta:
        q = q.filter(models.SoilReading.reading_datetime <= _parse_date(fecha_hasta, "fecha_hasta"))
    rows = q.order_by(models.SoilReading.reading_datetime.desc()).limit(5000).all()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ID", "Campo", "Fecha", "Profundidad_cm", "Valor_cbar", "Escala_visual", "Tipo", "Posición", "Zona", "Sensor", "Notas"])
    for r in rows:
        w.writerow([r.id, r.campo_id, r.reading_datetime, r.depth_cm, r.value_cbar, r.visual_scale, r.reading_type, r.position, r.location_zone, r.sensor_id, r.notes])
    buf.seek(0)
    return StreamingResponse(buf, media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=lecturas_suelo.csv"})


@router.get("/eventos/exportar")
def exportar_eventos(
    campo_id: Optional[str] = None,
    fecha_desde: Optional[str] = None,
    fecha_hasta: Optional[str] = None,
    db: Session = Depends(get_db),
    _=Depends(auth.get_current_user),
):
    q = db.query(models.IrrigationEvent)
    if campo_id:
        q = q.filter(models.IrrigationEvent.campo_id == campo_id)
    if fecha_desde:
        q = q.filter(models.IrrigationEvent.start_time >= _parse_date(fecha_desde, "fecha_desde"))
    if fecha_hasta:
        q = q.filter(models.IrrigationEvent.start_time <= _parse_date(fecha_hasta, "fecha_hasta"))
    rows = q.order_by(models.IrrigationEvent.start_time.desc()).limit(5000).all()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ID", "Campo", "Inicio", "Fin", "Duración_min", "Volumen_L", "Lámina_mm", "Fertiriego", "Producto", "Dosis", "Fuente", "Notas"])
    for e in rows:
        w.writerow([e.id, e.campo_id, e.start_time, e.end_time, e.duration_minutes, e.volume_liters, e.volume_mm, e.fertigation, e.fert_product, e.fert_dose, e.source, e.notes])
    buf.seek(0)
    return StreamingResponse(buf, media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=eventos_riego.csv"})


# ══════════════════════════════════════════════════════════════════════════════
# 16. PROGRAMAS DE RIEGO
# ══════════════════════════════════════════════════════════════════════════════

DIAS_SEMANA = {0: "Lun", 1: "Mar", 2: "Mié", 3: "Jue", 4: "Vie", 5: "Sáb", 6: "Dom"}


def _schedule_out(s: models.IrrigationSchedule) -> dict:
    return {
        "id": s.id,
        "campo_id": s.campo_id,
        "nombre": s.nombre,
        "dias_semana": s.dias_semana,
        "hora_inicio": s.hora_inicio,
        "duracion_minutos": s.duracion_minutos,
        "volumen_litros": s.volumen_litros,
        "fertiriego": s.fertiriego,
        "activo": s.activo,
        "notas": s.notas,
    }


@router.get("/programas")
def list_programas(
    campo_id: Optional[str] = None,
    db: Session = Depends(get_db),
    _=Depends(auth.get_current_user),
):
    q = db.query(models.IrrigationSchedule)
    if campo_id:
        q = q.filter(models.IrrigationSchedule.campo_id == campo_id)
    return [_schedule_out(s) for s in q.order_by(models.IrrigationSchedule.campo_id).all()]


@router.post("/programas")
def create_programa(
    data: dict,
    db: Session = Depends(get_db),
    user=Depends(auth.require_supervisor),
):
    if not data.get("campo_id") or not data.get("nombre"):
        raise HTTPException(400, "campo_id y nombre son requeridos")
    campo = db.query(models.Campo).filter(models.Campo.id_campo == data["campo_id"]).first()
    if not campo:
        raise HTTPException(404, f"Campo {data['campo_id']} no existe")
    s = models.IrrigationSchedule(
        campo_id=data["campo_id"],
        nombre=data["nombre"],
        dias_semana=data.get("dias_semana", ""),
        hora_inicio=data.get("hora_inicio", "06:00"),
        duracion_minutos=data.get("duracion_minutos"),
        volumen_litros=data.get("volumen_litros"),
        fertiriego=data.get("fertiriego", False),
        activo=data.get("activo", True),
        notas=data.get("notas"),
    )
    db.add(s)
    db.flush()
    audit.log(db, user, 'CREAR', 'PROGRAMA_RIEGO', str(s.id),
              f"Programa riego '{s.nombre}' campo {s.campo_id}")
    db.commit()
    db.refresh(s)
    return _schedule_out(s)


@router.put("/programas/{programa_id}")
def update_programa(
    programa_id: int, data: dict,
    db: Session = Depends(get_db),
    user=Depends(auth.require_supervisor),
):
    s = db.query(models.IrrigationSchedule).get(programa_id)
    if not s:
        raise HTTPException(404, "Programa no encontrado")
    for k in ("nombre", "dias_semana", "hora_inicio", "duracion_minutos",
              "volumen_litros", "fertiriego", "activo", "notas"):
        if k in data:
            setattr(s, k, data[k])
    if "campo_id" in data:
        s.campo_id = data["campo_id"]
    audit.log(db, user, 'ACTUALIZAR', 'PROGRAMA_RIEGO', str(s.id),
              f"Programa riego '{s.nombre}' actualizado")
    db.commit()
    db.refresh(s)
    return _schedule_out(s)


@router.delete("/programas/{programa_id}")
def delete_programa(
    programa_id: int,
    db: Session = Depends(get_db),
    user=Depends(auth.require_supervisor),
):
    s = db.query(models.IrrigationSchedule).get(programa_id)
    if not s:
        raise HTTPException(404, "Programa no encontrado")
    audit.log(db, user, 'ELIMINAR', 'PROGRAMA_RIEGO', str(s.id),
              f"Programa riego '{s.nombre}' eliminado")
    db.delete(s)
    db.commit()
    return {"ok": True}
