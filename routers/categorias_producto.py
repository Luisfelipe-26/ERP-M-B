"""Categorías de producto — CRUD.

Una categoría agrupa productos y les presta sus cuentas contables de inventario,
costo e ingreso, para configurarlas una vez por familia y no artículo por artículo.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from database import get_db
import models, auth
from pydantic import BaseModel
from typing import Optional, List

router = APIRouter(prefix="/api/categorias-producto", tags=["categorias-producto"])


class CategoriaCreate(BaseModel):
    nombre: str
    cuenta_inventario_id: Optional[int] = None
    cuenta_costo_id: Optional[int] = None
    cuenta_ingreso_id: Optional[int] = None


class CategoriaOut(CategoriaCreate):
    id: int
    activo: bool
    num_productos: int = 0
    model_config = {"from_attributes": True}


def _validar_cuentas(db: Session, data: CategoriaCreate):
    for campo in ("cuenta_inventario_id", "cuenta_costo_id", "cuenta_ingreso_id"):
        cid = getattr(data, campo)
        if cid and not db.query(models.CuentaContable.id).filter_by(id=cid).first():
            raise HTTPException(400, f"{campo}: la cuenta contable {cid} no existe")


def _out(db: Session, c: models.CategoriaProducto) -> CategoriaOut:
    out = CategoriaOut.model_validate(c)
    out.num_productos = db.query(models.Producto).filter(
        models.Producto.categoria_id == c.id, models.Producto.activo == True).count()
    return out


@router.get("", response_model=List[CategoriaOut])
def list_categorias(db: Session = Depends(get_db), _=Depends(auth.get_current_user)):
    cats = db.query(models.CategoriaProducto).filter(
        models.CategoriaProducto.activo == True
    ).order_by(models.CategoriaProducto.nombre).all()
    return [_out(db, c) for c in cats]


@router.post("", response_model=CategoriaOut)
def create_categoria(data: CategoriaCreate, db: Session = Depends(get_db),
                     _=Depends(auth.require_admin)):
    _validar_cuentas(db, data)
    existing = db.query(models.CategoriaProducto).filter(
        models.CategoriaProducto.nombre == data.nombre.strip()).first()
    if existing:
        if not existing.activo:
            existing.activo = True
            for k, v in data.model_dump().items():
                setattr(existing, k, v)
            db.commit()
            db.refresh(existing)
            return _out(db, existing)
        raise HTTPException(400, "Ya existe una categoría con ese nombre")
    c = models.CategoriaProducto(**data.model_dump())
    c.nombre = c.nombre.strip()
    db.add(c)
    db.commit()
    db.refresh(c)
    return _out(db, c)


@router.put("/{cat_id}", response_model=CategoriaOut)
def update_categoria(cat_id: int, data: CategoriaCreate, db: Session = Depends(get_db),
                     _=Depends(auth.require_admin)):
    c = db.query(models.CategoriaProducto).get(cat_id)
    if not c:
        raise HTTPException(404, "Categoría no encontrada")
    _validar_cuentas(db, data)
    otra = db.query(models.CategoriaProducto).filter(
        models.CategoriaProducto.nombre == data.nombre.strip(),
        models.CategoriaProducto.id != cat_id).first()
    if otra:
        raise HTTPException(400, "Ya existe otra categoría con ese nombre")
    for k, v in data.model_dump().items():
        setattr(c, k, v)
    c.nombre = c.nombre.strip()
    db.commit()
    db.refresh(c)
    return _out(db, c)


@router.delete("/{cat_id}")
def delete_categoria(cat_id: int, db: Session = Depends(get_db),
                     _=Depends(auth.require_admin)):
    c = db.query(models.CategoriaProducto).get(cat_id)
    if not c:
        raise HTTPException(404, "Categoría no encontrada")
    count = db.query(models.Producto).filter(models.Producto.categoria_id == cat_id).count()
    if count > 0:
        c.activo = False
        db.commit()
        return {"ok": True, "message": f"Desactivada (usada por {count} productos)"}
    db.delete(c)
    db.commit()
    return {"ok": True}
