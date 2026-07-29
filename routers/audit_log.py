"""Audit log — endpoint de consulta."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from database import get_db
import models, auth
from typing import Optional

router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("")
def list_audit_log(
    entidad: Optional[str] = None,
    accion: Optional[str] = None,
    usuario_id: Optional[int] = None,
    fecha_desde: Optional[str] = None,
    fecha_hasta: Optional[str] = None,
    limit: int = Query(100, le=500),
    db: Session = Depends(get_db),
    _=Depends(auth.require_supervisor),
):
    q = db.query(models.AuditLog)
    if entidad:
        q = q.filter(models.AuditLog.entidad == entidad)
    if accion:
        q = q.filter(models.AuditLog.accion == accion)
    if usuario_id:
        q = q.filter(models.AuditLog.usuario_id == usuario_id)
    if fecha_desde:
        q = q.filter(models.AuditLog.fecha >= fecha_desde)
    if fecha_hasta:
        q = q.filter(models.AuditLog.fecha <= fecha_hasta + "T23:59:59")
    logs = q.order_by(models.AuditLog.fecha.desc()).limit(limit).all()
    return [{
        "id": l.id,
        "fecha": l.fecha.isoformat() if l.fecha else None,
        "usuario": l.usuario_nombre,
        "accion": l.accion,
        "entidad": l.entidad,
        "entidad_id": l.entidad_id,
        "detalle": l.detalle,
    } for l in logs]
