"""Router clima — weather monitoring for ERP-Finca avocado farm."""

import json
import math
import os
import time
import urllib.request
from datetime import datetime, date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel as _Base
from sqlalchemy.orm import Session
from sqlalchemy import func as sqlfunc, desc

from database import get_db
import models
import auth
import audit

router = APIRouter(prefix="/api/clima", tags=["clima"])

# Farm coordinates (Rancho Arriba, Dominican Republic) — M1: from env vars
LAT = float(os.environ.get('FARM_LAT', '18.49'))
LON = float(os.environ.get('FARM_LON', '-69.98'))
TZ = os.environ.get('FARM_TZ', "America/Santo_Domingo")

# ════════════════════ Cache & Rate Limiting ════════════════════

_cache = {}  # { "url": (timestamp, data) }

def _cached_fetch(url: str, ttl_seconds: int = 900) -> dict:
    """Fetch JSON with in-memory cache. 429 errors get retry with backoff."""
    now = time.time()
    if url in _cache:
        cached_at, cached_data = _cache[url]
        if now - cached_at < ttl_seconds:
            return cached_data

    # Retry with exponential backoff for rate limits
    max_retries = 3
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ERP-Finca/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                _cache[url] = (now, data)
                return data
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < max_retries - 1:
                wait = 2 ** attempt  # 1s, 2s, 4s
                time.sleep(wait)
                continue
            raise Exception(f"HTTP Error {e.code}: {e.reason}")
        except Exception as e:
            raise Exception(f"Error al consultar Open-Meteo: {e}")

    raise Exception("Error al consultar Open-Meteo: demasiados intentos")

# Sync cooldown — prevent rapid re-syncs
_last_sync = 0.0
SYNC_COOLDOWN = 600  # 10 minutes minimum between syncs


# ──────────────── date validation helper ────────────────── C1
from datetime import date as _date
def _parse_date(s: str, name: str) -> _date:
    try:
        return _date.fromisoformat(s)
    except (ValueError, TypeError):
        raise HTTPException(400, f"{name} formato inválido. Use YYYY-MM-DD")


# ──────────────── Pydantic model for alert rules ────────── H8
class AlertRuleIn(_Base):
    name: str
    variable: str
    condition: str
    threshold_value: float
    threshold_value_2: Optional[float] = None
    duration_minutes: int = 0
    severity: str = 'warning'
    is_active: bool = True
    cooldown_minutes: int = 60


# ──────────────────── helpers ────────────────────

def _calc_vpd(temp_c: float, humidity_pct: float) -> float:
    """Vapor Pressure Deficit (kPa)."""
    es = 0.6108 * math.exp((17.27 * temp_c) / (temp_c + 237.3))
    ea = es * (humidity_pct / 100.0)
    return round(es - ea, 3)


# ──────────────────── 1. Current weather ────────────────────

@router.get("/current")
def get_current_weather(
    _user=Depends(auth.get_current_user),
):
    """Fetch current weather from Open-Meteo API (cached 15 min)."""
    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={LAT}&longitude={LON}"
        f"&current=temperature_2m,relative_humidity_2m,precipitation,"
        f"wind_speed_10m,wind_direction_10m,wind_gusts_10m,uv_index"
        f"&timezone={TZ}"
    )
    try:
        data = _cached_fetch(url, ttl_seconds=900)  # 15 min cache
    except Exception as e:
        raise HTTPException(502, str(e))

    c = data.get("current", {})
    return {
        "timestamp": c.get("time"),
        "temperature_c": c.get("temperature_2m"),
        "humidity_pct": c.get("relative_humidity_2m"),
        "precipitation_mm": c.get("precipitation"),
        "wind_speed_kmh": c.get("wind_speed_10m"),
        "wind_direction_deg": c.get("wind_direction_10m"),
        "wind_gust_kmh": c.get("wind_gusts_10m"),
        "uv_index": c.get("uv_index"),
        "vpd_kpa": _calc_vpd(
            c.get("temperature_2m", 25),
            c.get("relative_humidity_2m", 70),
        ),
    }


# ──────────────────── 2. 7-day Forecast ────────────────────

@router.get("/forecast")
def get_forecast(
    _user=Depends(auth.get_current_user),
):
    """7-day daily forecast from Open-Meteo (cached 30 min)."""
    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={LAT}&longitude={LON}"
        f"&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,"
        f"wind_speed_10m_max,uv_index_max,et0_fao_evapotranspiration"
        f"&timezone={TZ}&forecast_days=7"
    )
    try:
        data = _cached_fetch(url, ttl_seconds=1800)  # 30 min cache
    except Exception as e:
        raise HTTPException(502, str(e))

    daily = data.get("daily", {})
    days = []
    dates = daily.get("time", [])
    for i, d in enumerate(dates):
        days.append({
            "date": d,
            "temp_max": daily["temperature_2m_max"][i],
            "temp_min": daily["temperature_2m_min"][i],
            "precipitation_mm": daily["precipitation_sum"][i],
            "wind_max_kmh": daily["wind_speed_10m_max"][i],
            "uv_max": daily["uv_index_max"][i],
            "et0_mm": daily["et0_fao_evapotranspiration"][i],
        })
    return days


# ──────────────────── 3. History ────────────────────

@router.get("/history")
def get_history(
    fecha_desde: Optional[str] = Query(None, description="YYYY-MM-DD"),
    fecha_hasta: Optional[str] = Query(None, description="YYYY-MM-DD"),
    db: Session = Depends(get_db),
    _user=Depends(auth.get_current_user),
):
    """Query WeatherDailySummary with optional date range."""
    q = db.query(models.WeatherDailySummary).order_by(desc(models.WeatherDailySummary.date))

    if fecha_desde:
        d_desde = _parse_date(fecha_desde, "fecha_desde")  # C1
        q = q.filter(models.WeatherDailySummary.date >= d_desde)
    if fecha_hasta:
        d_hasta = _parse_date(fecha_hasta, "fecha_hasta")  # C1
        q = q.filter(models.WeatherDailySummary.date < d_hasta + timedelta(days=1))  # C2/H7: proper date math

    rows = q.limit(90).all()
    return [
        {
            "id": r.id,
            "date": r.date.strftime("%Y-%m-%d") if r.date else None,
            "temp_min": r.temp_min,
            "temp_max": r.temp_max,
            "temp_avg": r.temp_avg,
            "humidity_avg": r.humidity_avg,
            "rainfall_total_mm": r.rainfall_total_mm,
            "wind_avg_kmh": r.wind_avg_kmh,
            "et0_mm": r.et0_mm,
            "gdd_base10": r.gdd_base10,
            "gdd_cumulative": r.gdd_cumulative,
            "frost_hours": r.frost_hours,
            "heat_stress_hours": r.heat_stress_hours,
            "vpd_avg": r.vpd_avg,
            "anthracnose_risk": r.anthracnose_risk,
            "phytophthora_risk": r.phytophthora_risk,
        }
        for r in rows
    ]


# ──────────────────── 4. Sync ────────────────────

@router.post("/sync")
def sync_weather(
    db: Session = Depends(get_db),
    current_user=Depends(auth.require_supervisor),
):
    # Check sync cooldown
    global _last_sync
    now = time.time()
    if now - _last_sync < SYNC_COOLDOWN:
        wait = round(SYNC_COOLDOWN - (now - _last_sync))
        raise HTTPException(429, f"Sync en cooldown. Espere {wait}s (mínimo {SYNC_COOLDOWN//60} min entre sincronizaciones)")

    """Fetch last 7 days hourly data, store in WeatherReading, aggregate into DailySummary."""
    end = date.today()
    start = end - timedelta(days=7)

    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={LAT}&longitude={LON}"
        f"&hourly=temperature_2m,relative_humidity_2m,precipitation,"
        f"wind_speed_10m,wind_direction_10m,wind_gusts_10m,"
        f"uv_index,et0_fao_evapotranspiration"
        f"&timezone={TZ}"
        f"&start_date={start}&end_date={end}"
    )
    try:
        data = _cached_fetch(url, ttl_seconds=0)  # no cache, retries on 429
    except Exception as e:
        raise HTTPException(502, str(e))

    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    if not times:
        raise HTTPException(404, "No hourly data returned from Open-Meteo")

    # Update last_sync timestamp
    _last_sync = time.time()

    # Delete existing readings in the range to avoid duplicates
    db.query(models.WeatherReading).filter(
        models.WeatherReading.recorded_at >= str(start),
        models.WeatherReading.recorded_at <= str(end) + "T23:59:59",
        models.WeatherReading.source == "api",
    ).delete(synchronize_session=False)

    readings_count = 0
    for i, t in enumerate(times):
        temp = hourly.get("temperature_2m", [None])[i]
        hum = hourly.get("relative_humidity_2m", [None])[i]
        rain = hourly.get("precipitation", [None])[i]
        wind = hourly.get("wind_speed_10m", [None])[i]
        wdir = hourly.get("wind_direction_10m", [None])[i]
        gust = hourly.get("wind_gusts_10m", [None])[i]
        uv = hourly.get("uv_index", [None])[i]
        et0 = hourly.get("et0_fao_evapotranspiration", [None])[i]

        vpd = _calc_vpd(temp or 25, hum or 70) if temp is not None and hum is not None else None

        reading = models.WeatherReading(
            recorded_at=datetime.fromisoformat(t),
            source="api",
            station_code="open_meteo",
            temperature_c=temp,
            humidity_pct=hum,
            rainfall_mm=rain,
            wind_speed_kmh=wind,
            wind_direction_deg=int(wdir) if wdir is not None else None,
            wind_gust_kmh=gust,
            uv_index=uv,
            et0_mm=et0,
            vpd_kpa=vpd,
        )
        db.add(reading)
        readings_count += 1

    db.flush()

    # Aggregate daily summaries
    days_processed = 0
    summaries = []  # H9: collect for GDD accumulation
    current = start
    while current <= end:
        day_start = datetime.combine(current, datetime.min.time())
        day_end = datetime.combine(current, datetime.max.time())

        day_readings = (
            db.query(models.WeatherReading)
            .filter(
                models.WeatherReading.recorded_at >= day_start,
                models.WeatherReading.recorded_at <= day_end,
                models.WeatherReading.source == "api",
            )
            .all()
        )

        if not day_readings:
            current += timedelta(days=1)
            continue

        temps = [r.temperature_c for r in day_readings if r.temperature_c is not None]
        hums = [r.humidity_pct for r in day_readings if r.humidity_pct is not None]
        rains = [r.rainfall_mm or 0 for r in day_readings]
        winds = [r.wind_speed_kmh for r in day_readings if r.wind_speed_kmh is not None]
        gusts = [r.wind_gust_kmh for r in day_readings if r.wind_gust_kmh is not None]
        uvs = [r.uv_index for r in day_readings if r.uv_index is not None]
        et0s = [r.et0_mm or 0 for r in day_readings]

        temp_min = min(temps) if temps else None
        temp_max = max(temps) if temps else None
        temp_avg = round(sum(temps) / len(temps), 1) if temps else None
        rain_total = round(sum(rains), 1)

        # GDD base 10°C
        gdd = max(0, (temp_avg or 10) - 10) if temp_avg is not None else 0

        # Frost hours: temp < 2°C
        frost_hrs = sum(1 for t in temps if t < 2)
        # Heat stress hours: temp > 35°C
        heat_hrs = sum(1 for t in temps if t > 35)

        # Anthracnose risk: humidity > 85% AND temp 20-28°C
        anthrac_hours = sum(
            1 for r in day_readings
            if r.humidity_pct is not None and r.humidity_pct > 85
            and r.temperature_c is not None and 20 <= r.temperature_c <= 28
        )
        anthrac_risk = min(10, round(anthrac_hours / 2.4, 1))  # 24h favorable = 10

        # Phytophthora risk: rain > 20mm AND temp 25-30°C
        phyto_temp_hours = sum(
            1 for r in day_readings
            if r.temperature_c is not None and 25 <= r.temperature_c <= 30
        )
        phyto_risk = 0.0
        if rain_total > 20 and phyto_temp_hours > 0:
            phyto_risk = min(10, round((rain_total / 10) * (phyto_temp_hours / 6), 1))

        # VPD average
        vpds = [r.vpd_kpa for r in day_readings if r.vpd_kpa is not None]
        vpd_avg = round(sum(vpds) / len(vpds), 2) if vpds else None

        # Upsert daily summary
        existing = (
            db.query(models.WeatherDailySummary)
            .filter(
                sqlfunc.date(models.WeatherDailySummary.date) == current,
                models.WeatherDailySummary.station_code == "open_meteo",
            )
            .first()
        )

        if existing:
            summary = existing
        else:
            summary = models.WeatherDailySummary(
                date=day_start,
                station_code="open_meteo",
            )
            db.add(summary)

        summary.temp_min = temp_min
        summary.temp_max = temp_max
        summary.temp_avg = temp_avg
        summary.humidity_min = min(hums) if hums else None
        summary.humidity_max = max(hums) if hums else None
        summary.humidity_avg = round(sum(hums) / len(hums), 1) if hums else None
        summary.rainfall_total_mm = rain_total
        summary.wind_avg_kmh = round(sum(winds) / len(winds), 1) if winds else None
        summary.wind_max_gust_kmh = max(gusts) if gusts else None
        summary.uv_max = max(uvs) if uvs else None
        summary.et0_mm = round(sum(et0s), 2)
        summary.gdd_base10 = round(gdd, 1)
        summary.frost_hours = frost_hrs
        summary.heat_stress_hours = heat_hrs
        summary.vpd_avg = vpd_avg
        summary.anthracnose_risk = anthrac_risk
        summary.phytophthora_risk = phyto_risk

        summaries.append((current, summary))  # H9
        days_processed += 1
        current += timedelta(days=1)

    # H9: compute gdd_cumulative across sorted days
    sorted_summaries = sorted(summaries, key=lambda x: x[0])
    running_gdd = 0.0
    for _, s in sorted_summaries:
        running_gdd += s.gdd_base10 or 0
        s.gdd_cumulative = round(running_gdd, 1)

    # H1: audit trail
    audit.log(db, current_user, 'CREAR', 'WEATHER_SYNC', str(start),
              f"Sync {start}→{end}: {readings_count} lecturas, {days_processed} días")

    db.commit()

    return {
        "message": "Sincronización completada",
        "readings_stored": readings_count,
        "days_summarized": days_processed,
    }


# ──────────────────── 5. Alerts ────────────────────

@router.get("/alerts")
def list_alerts(
    db: Session = Depends(get_db),
    _user=Depends(auth.get_current_user),
):
    """List recent 50 weather alerts."""
    rows = (
        db.query(models.WeatherAlert)
        .order_by(desc(models.WeatherAlert.triggered_at))
        .limit(50)
        .all()
    )
    return [
        {
            "id": a.id,
            "rule_id": a.rule_id,
            "triggered_at": a.triggered_at.isoformat() if a.triggered_at else None,
            "resolved_at": a.resolved_at.isoformat() if a.resolved_at else None,
            "trigger_value": a.trigger_value,
            "severity": a.severity,
            "message": a.message,
            "acknowledged_by": a.acknowledged_by,
            "acknowledged_at": a.acknowledged_at.isoformat() if a.acknowledged_at else None,
            "action_taken": a.action_taken,
        }
        for a in rows
    ]


# ──────────────────── 6. Alert Rules - List ────────────────────

@router.get("/alert-rules")
def list_alert_rules(
    db: Session = Depends(get_db),
    _user=Depends(auth.get_current_user),
):
    """List all weather alert rules."""
    rows = db.query(models.WeatherAlertRule).order_by(models.WeatherAlertRule.id).all()
    return [
        {
            "id": r.id,
            "name": r.name,
            "variable": r.variable,
            "condition": r.condition,
            "threshold_value": r.threshold_value,
            "threshold_value_2": r.threshold_value_2,
            "duration_minutes": r.duration_minutes,
            "severity": r.severity,
            "is_active": r.is_active,
            "cooldown_minutes": r.cooldown_minutes,
        }
        for r in rows
    ]


# ──────────────────── 7. Alert Rules - Create ────────────────────

@router.post("/alert-rules")
def create_alert_rule(
    payload: AlertRuleIn,  # H8: Pydantic model instead of dict
    db: Session = Depends(get_db),
    current_user=Depends(auth.require_supervisor),
):
    """Create a new alert rule (supervisor only)."""
    rule = models.WeatherAlertRule(
        name=payload.name,
        variable=payload.variable,
        condition=payload.condition,
        threshold_value=payload.threshold_value,
        threshold_value_2=payload.threshold_value_2,
        duration_minutes=payload.duration_minutes,
        severity=payload.severity,
        is_active=payload.is_active,
        cooldown_minutes=payload.cooldown_minutes,
    )
    db.add(rule)
    db.flush()
    # H1: audit
    audit.log(db, current_user, 'CREAR', 'ALERT_RULE', str(rule.id),
              f"Regla '{rule.name}' creada")
    db.commit()
    db.refresh(rule)
    return {"id": rule.id, "message": "Regla creada"}


# ──────────────────── 8. Alert Rules - Update ────────────────────

@router.put("/alert-rules/{rule_id}")
def update_alert_rule(
    rule_id: int,
    payload: AlertRuleIn,  # H8: Pydantic model
    db: Session = Depends(get_db),
    current_user=Depends(auth.require_supervisor),
):
    """Update an existing alert rule."""
    rule = db.query(models.WeatherAlertRule).filter(models.WeatherAlertRule.id == rule_id).first()
    if not rule:
        raise HTTPException(404, "Regla no encontrada")

    for field in ["name", "variable", "condition", "threshold_value", "threshold_value_2",
                   "duration_minutes", "severity", "is_active", "cooldown_minutes"]:
        setattr(rule, field, getattr(payload, field))

    # H1: audit
    audit.log(db, current_user, 'ACTUALIZAR', 'ALERT_RULE', str(rule_id),
              f"Regla '{rule.name}' actualizada")
    db.commit()
    return {"message": "Regla actualizada"}


# ──────────────────── 9. Alert Rules - Delete ────────────────────

@router.delete("/alert-rules/{rule_id}")
def delete_alert_rule(
    rule_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(auth.require_supervisor),
):
    """Delete an alert rule."""
    rule = db.query(models.WeatherAlertRule).filter(models.WeatherAlertRule.id == rule_id).first()
    if not rule:
        raise HTTPException(404, "Regla no encontrada")
    rule_name = rule.name
    db.delete(rule)
    # H1: audit
    audit.log(db, current_user, 'ELIMINAR', 'ALERT_RULE', str(rule_id),
              f"Regla '{rule_name}' eliminada")
    db.commit()
    return {"message": "Regla eliminada"}


# ──────────────────── 10. Acknowledge Alert ────────────────────

@router.post("/alerts/{alert_id}/acknowledge")
def acknowledge_alert(
    alert_id: int,
    payload: dict,
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user),
):
    """Acknowledge a weather alert."""
    alert = db.query(models.WeatherAlert).filter(models.WeatherAlert.id == alert_id).first()
    if not alert:
        raise HTTPException(404, "Alerta no encontrada")

    alert.acknowledged_by = user.id
    alert.acknowledged_at = datetime.now()  # H6: replaced utcnow()
    alert.action_taken = payload.get("action_taken", "")
    # H1: audit
    audit.log(db, user, 'CREAR', 'ALERT_ACK', str(alert_id),
              f"Alerta {alert_id} reconocida")
    db.commit()
    return {"message": "Alerta reconocida"}
