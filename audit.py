"""
Audit trail — helper para registrar eventos críticos.
"""
from sqlalchemy.orm import Session
import models
import json as _json
from datetime import datetime


def log(db: Session, user, accion: str, entidad: str, entidad_id: str,
        detalle: str, datos: dict = None):
    """Registra un evento en el audit log."""
    entry = models.AuditLog(
        usuario_id=user.id if user else None,
        usuario_nombre=user.nombre if user else "Sistema",
        accion=accion,
        entidad=entidad,
        entidad_id=str(entidad_id),
        detalle=detalle,
        datos_json=_json.dumps(datos, default=str, ensure_ascii=False) if datos else None,
    )
    db.add(entry)
    # No commit here — caller commits as part of the transaction
