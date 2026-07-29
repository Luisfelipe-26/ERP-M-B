from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from database import get_db
import models, schemas, auth
from routers.sequences import peek_next
from typing import List

router = APIRouter(prefix="/api/trabajadores", tags=["trabajadores"])


@router.get("/next-id")
def next_id_trabajador(db: Session = Depends(get_db), _=Depends(auth.get_current_user)):
    return {"next_id": peek_next("TRAB", db)}


@router.get("", response_model=List[schemas.TrabajadorOut])
def list_trabajadores(db: Session = Depends(get_db), _=Depends(auth.get_current_user)):
    return db.query(models.Trabajador).filter(models.Trabajador.activo == True).order_by(models.Trabajador.nombre).all()


@router.post("", response_model=schemas.TrabajadorOut)
def create_trabajador(data: schemas.TrabajadorCreate, db: Session = Depends(get_db), _=Depends(auth.require_admin)):
    if db.query(models.Trabajador).filter(models.Trabajador.id_trab == data.id_trab).first():
        raise HTTPException(status_code=400, detail="ID de trabajador ya existe")
    t = models.Trabajador(**data.model_dump())
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


@router.get("/{id_trab}", response_model=schemas.TrabajadorOut)
def get_trabajador(id_trab: str, db: Session = Depends(get_db), _=Depends(auth.get_current_user)):
    t = db.query(models.Trabajador).filter(models.Trabajador.id_trab == id_trab).first()
    if not t:
        raise HTTPException(status_code=404, detail="Trabajador no encontrado")
    return t


@router.put("/{id_trab}", response_model=schemas.TrabajadorOut)
def update_trabajador(id_trab: str, data: schemas.TrabajadorCreate, db: Session = Depends(get_db), _=Depends(auth.require_admin)):
    t = db.query(models.Trabajador).filter(models.Trabajador.id_trab == id_trab).first()
    if not t:
        raise HTTPException(status_code=404, detail="Trabajador no encontrado")
    for k, v in data.model_dump().items():
        setattr(t, k, v)
    db.commit()
    db.refresh(t)
    return t


@router.delete("/{id_trab}")
def delete_trabajador(id_trab: str, db: Session = Depends(get_db), _=Depends(auth.require_admin)):
    t = db.query(models.Trabajador).filter(models.Trabajador.id_trab == id_trab).first()
    if not t:
        raise HTTPException(status_code=404, detail="Trabajador no encontrado")
    t.activo = False
    db.commit()
    return {"ok": True}
