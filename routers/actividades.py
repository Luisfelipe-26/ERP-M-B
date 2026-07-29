from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from database import get_db
import models, schemas, auth
from routers.sequences import peek_next
from typing import List

router = APIRouter(prefix="/api/actividades", tags=["actividades"])


@router.get("/next-id")
def next_id_actividad(db: Session = Depends(get_db), _=Depends(auth.get_current_user)):
    return {"next_id": peek_next("ACT", db)}


@router.get("", response_model=List[schemas.ActividadOut])
def list_actividades(db: Session = Depends(get_db), _=Depends(auth.get_current_user)):
    return db.query(models.Actividad).filter(models.Actividad.activo == True).order_by(models.Actividad.actividad).all()


@router.post("", response_model=schemas.ActividadOut)
def create_actividad(data: schemas.ActividadCreate, db: Session = Depends(get_db), _=Depends(auth.require_admin)):
    if db.query(models.Actividad).filter(models.Actividad.id_act == data.id_act).first():
        raise HTTPException(status_code=400, detail="ID de actividad ya existe")
    a = models.Actividad(**data.model_dump())
    db.add(a)
    db.commit()
    db.refresh(a)
    return a


@router.put("/{id_act}", response_model=schemas.ActividadOut)
def update_actividad(id_act: str, data: schemas.ActividadCreate, db: Session = Depends(get_db), _=Depends(auth.require_admin)):
    a = db.query(models.Actividad).filter(models.Actividad.id_act == id_act).first()
    if not a:
        raise HTTPException(status_code=404, detail="Actividad no encontrada")
    for k, v in data.model_dump().items():
        setattr(a, k, v)
    db.commit()
    db.refresh(a)
    return a


@router.delete("/{id_act}")
def delete_actividad(id_act: str, db: Session = Depends(get_db), _=Depends(auth.require_admin)):
    a = db.query(models.Actividad).filter(models.Actividad.id_act == id_act).first()
    if not a:
        raise HTTPException(status_code=404, detail="Actividad no encontrada")
    a.activo = False
    db.commit()
    return {"ok": True}
