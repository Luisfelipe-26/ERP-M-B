from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from database import get_db
import models, schemas, auth
from routers.sequences import peek_next
from typing import List

router = APIRouter(prefix="/api/campos", tags=["campos"])


@router.get("/next-id")
def next_id_campo(db: Session = Depends(get_db), _=Depends(auth.get_current_user)):
    return {"next_id": peek_next("CAMPO", db)}


@router.get("", response_model=List[schemas.CampoOut])
def list_campos(db: Session = Depends(get_db), _=Depends(auth.get_current_user)):
    return db.query(models.Campo).filter(models.Campo.activo == True).order_by(models.Campo.id_campo).all()


@router.post("", response_model=schemas.CampoOut)
def create_campo(data: schemas.CampoCreate, db: Session = Depends(get_db), _=Depends(auth.require_admin)):
    if db.query(models.Campo).filter(models.Campo.id_campo == data.id_campo).first():
        raise HTTPException(status_code=400, detail="ID de campo ya existe")
    campo = models.Campo(**data.model_dump())
    db.add(campo)
    db.commit()
    db.refresh(campo)
    return campo


@router.get("/{id_campo}", response_model=schemas.CampoOut)
def get_campo(id_campo: str, db: Session = Depends(get_db), _=Depends(auth.get_current_user)):
    campo = db.query(models.Campo).filter(models.Campo.id_campo == id_campo).first()
    if not campo:
        raise HTTPException(status_code=404, detail="Campo no encontrado")
    return campo


@router.put("/{id_campo}", response_model=schemas.CampoOut)
def update_campo(id_campo: str, data: schemas.CampoCreate, db: Session = Depends(get_db), _=Depends(auth.require_admin)):
    campo = db.query(models.Campo).filter(models.Campo.id_campo == id_campo).first()
    if not campo:
        raise HTTPException(status_code=404, detail="Campo no encontrado")
    for k, v in data.model_dump().items():
        setattr(campo, k, v)
    db.commit()
    db.refresh(campo)
    return campo


@router.delete("/{id_campo}")
def delete_campo(id_campo: str, db: Session = Depends(get_db), _=Depends(auth.require_admin)):
    campo = db.query(models.Campo).filter(models.Campo.id_campo == id_campo).first()
    if not campo:
        raise HTTPException(status_code=404, detail="Campo no encontrado")
    campo.activo = False
    db.commit()
    return {"ok": True}
