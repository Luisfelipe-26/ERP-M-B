"""Tipos de Producto — CRUD."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from database import get_db
import models, auth
from pydantic import BaseModel
from typing import Optional, List

router = APIRouter(prefix="/api/tipos-producto", tags=["tipos-producto"])


class TipoProductoCreate(BaseModel):
    nombre: str
    descripcion: Optional[str] = None


class TipoProductoOut(BaseModel):
    id: int
    nombre: str
    descripcion: Optional[str] = None
    activo: bool
    model_config = {"from_attributes": True}


@router.get("", response_model=List[TipoProductoOut])
def list_tipos(db: Session = Depends(get_db), _=Depends(auth.get_current_user)):
    return db.query(models.TipoProducto).filter(
        models.TipoProducto.activo == True
    ).order_by(models.TipoProducto.nombre).all()


@router.post("", response_model=TipoProductoOut)
def create_tipo(data: TipoProductoCreate, db: Session = Depends(get_db),
                _=Depends(auth.require_admin)):
    existing = db.query(models.TipoProducto).filter(
        models.TipoProducto.nombre == data.nombre).first()
    if existing:
        if not existing.activo:
            existing.activo = True
            existing.descripcion = data.descripcion
            db.commit()
            db.refresh(existing)
            return existing
        raise HTTPException(400, "Ya existe un tipo con ese nombre")
    tp = models.TipoProducto(nombre=data.nombre, descripcion=data.descripcion)
    db.add(tp)
    db.commit()
    db.refresh(tp)
    return tp


@router.put("/{tipo_id}", response_model=TipoProductoOut)
def update_tipo(tipo_id: int, data: TipoProductoCreate, db: Session = Depends(get_db),
                _=Depends(auth.require_admin)):
    tp = db.query(models.TipoProducto).filter(models.TipoProducto.id == tipo_id).first()
    if not tp:
        raise HTTPException(404, "Tipo no encontrado")
    tp.nombre = data.nombre
    tp.descripcion = data.descripcion
    db.commit()
    db.refresh(tp)
    return tp


@router.delete("/{tipo_id}")
def delete_tipo(tipo_id: int, db: Session = Depends(get_db),
                _=Depends(auth.require_admin)):
    tp = db.query(models.TipoProducto).filter(models.TipoProducto.id == tipo_id).first()
    if not tp:
        raise HTTPException(404, "Tipo no encontrado")
    # Check if any products use this type
    count = db.query(models.Producto).filter(models.Producto.tipo == tp.nombre).count()
    if count > 0:
        tp.activo = False
        db.commit()
        return {"ok": True, "message": f"Desactivado (usado por {count} productos)"}
    db.delete(tp)
    db.commit()
    return {"ok": True}
