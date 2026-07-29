"""
Reportes — Costos por Campo × Actividad
Endpoint matricial con filtros de fecha, desglose MO/Insumos, export CSV.
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import func, case, and_
from database import get_db
import models, auth
from datetime import datetime, date
from typing import Optional
import csv, io
from fastapi.responses import StreamingResponse

router = APIRouter(prefix="/api/reportes", tags=["reportes"])


def _build_costos_query(db: Session, fecha_desde: Optional[str], fecha_hasta: Optional[str],
                         campo_id: Optional[str], bloque: Optional[str]):
    """
    Returns a list of dicts with the cross-tabulation:
      campo_id, campo_nombre, bloque, area_ha, actividad_id, actividad_nombre, tipo_actividad,
      num_ordenes, horas_mo, costo_mo, costo_insumos, costo_total, costo_ha
    """
    q = db.query(
        models.OrdenTrabajo.campo_id,
        models.Campo.nombre.label("campo_nombre"),
        models.Campo.bloque,
        models.Campo.area_ha,
        models.OrdenTrabajo.actividad_id,
        models.Actividad.actividad.label("actividad_nombre"),
        models.Actividad.tipo.label("tipo_actividad"),
        func.count(models.OrdenTrabajo.id).label("num_ordenes"),
        func.coalesce(func.sum(models.OrdenTrabajo.horas_mo), 0).label("horas_mo"),
        func.coalesce(func.sum(models.OrdenTrabajo.costo_mano_obra), 0).label("costo_mo"),
        func.coalesce(func.sum(models.OrdenTrabajo.costo_insumos), 0).label("costo_insumos"),
        func.coalesce(func.sum(models.OrdenTrabajo.costo_total), 0).label("costo_total"),
    ).join(
        models.Campo, models.OrdenTrabajo.campo_id == models.Campo.id_campo, isouter=True
    ).join(
        models.Actividad, models.OrdenTrabajo.actividad_id == models.Actividad.id_act, isouter=True
    )

    if fecha_desde:
        q = q.filter(models.OrdenTrabajo.fecha_ejecucion >= fecha_desde)
    if fecha_hasta:
        q = q.filter(models.OrdenTrabajo.fecha_ejecucion <= fecha_hasta)
    if campo_id:
        q = q.filter(models.OrdenTrabajo.campo_id == campo_id)
    if bloque:
        q = q.filter(models.Campo.bloque == bloque)

    rows = q.group_by(
        models.OrdenTrabajo.campo_id,
        models.Campo.nombre,
        models.Campo.bloque,
        models.Campo.area_ha,
        models.OrdenTrabajo.actividad_id,
        models.Actividad.actividad,
        models.Actividad.tipo,
    ).order_by(
        models.OrdenTrabajo.campo_id,
        models.OrdenTrabajo.actividad_id,
    ).all()

    return [{
        "campo_id": r[0],
        "campo_nombre": r[1] or r[0],
        "bloque": r[2] or "",
        "area_ha": float(r[3] or 0),
        "actividad_id": r[4],
        "actividad_nombre": r[5] or r[4],
        "tipo_actividad": r[6] or "",
        "num_ordenes": int(r[7]),
        "horas_mo": round(float(r[8]), 1),
        "costo_mo": round(float(r[9]), 2),
        "costo_insumos": round(float(r[10]), 2),
        "costo_total": round(float(r[11]), 2),
        "costo_ha": round(float(r[11]) / float(r[3]) if r[3] and float(r[3]) > 0 else 0, 2),
    } for r in rows]


@router.get("/costos-campo-actividad")
def costos_campo_actividad(
    fecha_desde: Optional[str] = Query(None, description="YYYY-MM-DD"),
    fecha_hasta: Optional[str] = Query(None, description="YYYY-MM-DD"),
    campo_id: Optional[str] = Query(None),
    bloque: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _=Depends(auth.get_current_user),
):
    """
    Returns flat rows of campo × actividad with cost breakdown.
    Frontend pivots this into the matrix view.
    """
    rows = _build_costos_query(db, fecha_desde, fecha_hasta, campo_id, bloque)

    # Also return lists of unique campos and actividades for the matrix axes
    campos_set = {}
    actividades_set = {}
    for r in rows:
        if r["campo_id"] not in campos_set:
            campos_set[r["campo_id"]] = {
                "campo_id": r["campo_id"],
                "campo_nombre": r["campo_nombre"],
                "bloque": r["bloque"],
                "area_ha": r["area_ha"],
            }
        if r["actividad_id"] not in actividades_set:
            actividades_set[r["actividad_id"]] = {
                "actividad_id": r["actividad_id"],
                "actividad_nombre": r["actividad_nombre"],
                "tipo_actividad": r["tipo_actividad"],
            }

    return {
        "rows": rows,
        "campos": sorted(campos_set.values(), key=lambda c: c["campo_id"]),
        "actividades": sorted(actividades_set.values(), key=lambda a: a["actividad_id"]),
        "totales": {
            "costo_mo": round(sum(r["costo_mo"] for r in rows), 2),
            "costo_insumos": round(sum(r["costo_insumos"] for r in rows), 2),
            "costo_total": round(sum(r["costo_total"] for r in rows), 2),
            "num_ordenes": sum(r["num_ordenes"] for r in rows),
            "horas_mo": round(sum(r["horas_mo"] for r in rows), 1),
        }
    }


@router.get("/costos-campo-actividad/csv")
def export_costos_csv(
    fecha_desde: Optional[str] = Query(None),
    fecha_hasta: Optional[str] = Query(None),
    campo_id: Optional[str] = Query(None),
    bloque: Optional[str] = Query(None),
    vista: str = Query("detalle", description="detalle | matriz"),
    db: Session = Depends(get_db),
    _=Depends(auth.get_current_user),
):
    rows = _build_costos_query(db, fecha_desde, fecha_hasta, campo_id, bloque)
    output = io.StringIO()
    output.write('\ufeff')  # BOM UTF-8 for Excel español
    writer = csv.writer(output)

    if vista == "matriz":
        # Pivot: rows=campos, cols=actividades, cells=costo_total
        act_ids = sorted(set(r["actividad_id"] for r in rows))
        act_names = {r["actividad_id"]: r["actividad_nombre"] for r in rows}

        header = ["Campo", "Bloque", "Área (ha)"] + [act_names.get(a, a) for a in act_ids] + ["TOTAL"]
        writer.writerow(header)

        campo_ids = sorted(set(r["campo_id"] for r in rows))
        lookup = {}
        for r in rows:
            lookup[(r["campo_id"], r["actividad_id"])] = r

        grand_totals = {a: 0 for a in act_ids}
        grand_total = 0

        for cid in campo_ids:
            campo_info = next((r for r in rows if r["campo_id"] == cid), {})
            row_total = 0
            row_data = [campo_info.get("campo_nombre", cid), campo_info.get("bloque", ""),
                        campo_info.get("area_ha", "")]
            for aid in act_ids:
                val = lookup.get((cid, aid), {}).get("costo_total", 0)
                row_data.append(round(val, 2) if val else "")
                row_total += val
                grand_totals[aid] += val
            row_data.append(round(row_total, 2))
            grand_total += row_total
            writer.writerow(row_data)

        # Totals row
        totals_row = ["TOTAL", "", ""]
        for aid in act_ids:
            totals_row.append(round(grand_totals[aid], 2))
        totals_row.append(round(grand_total, 2))
        writer.writerow(totals_row)
    else:
        # Detail view
        writer.writerow(["Campo", "Bloque", "Área (ha)", "Actividad", "Tipo",
                         "# OTs", "Horas MO", "Costo MO", "Costo Insumos", "Costo Total", "Costo/ha"])
        total_mo = total_ins = total_tot = 0
        for r in rows:
            writer.writerow([
                r["campo_nombre"], r["bloque"], r["area_ha"],
                r["actividad_nombre"], r["tipo_actividad"],
                r["num_ordenes"], r["horas_mo"],
                r["costo_mo"], r["costo_insumos"], r["costo_total"], r["costo_ha"]
            ])
            total_mo += r["costo_mo"]
            total_ins += r["costo_insumos"]
            total_tot += r["costo_total"]
        writer.writerow([])
        writer.writerow(["", "", "", "", "TOTAL", "", "",
                         round(total_mo, 2), round(total_ins, 2), round(total_tot, 2), ""])

    output.seek(0)
    rango = ""
    if fecha_desde:
        rango += f"_{fecha_desde}"
    if fecha_hasta:
        rango += f"_{fecha_hasta}"
    filename = f"costos_campo_actividad{rango}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


@router.get("/resumen-campo/{campo_id}")
def resumen_campo(
    campo_id: str,
    fecha_desde: Optional[str] = Query(None),
    fecha_hasta: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _=Depends(auth.get_current_user),
):
    """Detalle completo de un campo: actividades, OTs, costos acumulados."""
    campo = db.query(models.Campo).filter(models.Campo.id_campo == campo_id).first()
    if not campo:
        from fastapi import HTTPException
        raise HTTPException(404, "Campo no encontrado")

    # Actividades con costos
    rows = _build_costos_query(db, fecha_desde, fecha_hasta, campo_id, None)

    # Últimas 20 OTs de este campo
    q_ots = db.query(models.OrdenTrabajo).filter(
        models.OrdenTrabajo.campo_id == campo_id
    )
    if fecha_desde:
        q_ots = q_ots.filter(models.OrdenTrabajo.fecha_ejecucion >= fecha_desde)
    if fecha_hasta:
        q_ots = q_ots.filter(models.OrdenTrabajo.fecha_ejecucion <= fecha_hasta)
    ots = q_ots.order_by(models.OrdenTrabajo.fecha_ejecucion.desc()).limit(20).all()

    return {
        "campo": {
            "id_campo": campo.id_campo,
            "nombre": campo.nombre,
            "bloque": campo.bloque,
            "area_ha": campo.area_ha,
            "n_plantas": campo.n_plantas,
            "variedad": campo.variedad,
            "ano_siembra": campo.ano_siembra,
            "etapa": campo.etapa,
        },
        "actividades": rows,
        "ordenes": [{
            "ot_id": o.ot_id,
            "fecha": o.fecha_ejecucion.isoformat() if o.fecha_ejecucion else None,
            "actividad_id": o.actividad_id,
            "estado": o.estado,
            "costo_mo": round(o.costo_mano_obra or 0, 2),
            "costo_insumos": round(o.costo_insumos or 0, 2),
            "costo_total": round(o.costo_total or 0, 2),
            "supervisor": o.supervisor,
        } for o in ots],
        "totales": {
            "costo_mo": round(sum(r["costo_mo"] for r in rows), 2),
            "costo_insumos": round(sum(r["costo_insumos"] for r in rows), 2),
            "costo_total": round(sum(r["costo_total"] for r in rows), 2),
            "costo_ha": round(
                sum(r["costo_total"] for r in rows) / campo.area_ha
                if campo.area_ha and campo.area_ha > 0 else 0, 2
            ),
            "num_ordenes": sum(r["num_ordenes"] for r in rows),
        }
    }
