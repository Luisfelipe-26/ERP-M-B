"""Proveedores — CRUD + sync Odoo."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from database import get_db
import models, auth
from pydantic import BaseModel
from typing import Optional, List

router = APIRouter(prefix="/api/proveedores", tags=["proveedores"])


class ProveedorCreate(BaseModel):
    nombre: str
    rnc: Optional[str] = None
    email: Optional[str] = None
    telefono: Optional[str] = None
    contacto: Optional[str] = None
    direccion: Optional[str] = None
    condicion_pago_dias: int = 30
    tipo_ncf_default: str = "B11"
    cuenta_cxp_id: Optional[int] = None


class ProveedorOut(BaseModel):
    id: int
    nombre: str
    rnc: Optional[str] = None
    email: Optional[str] = None
    telefono: Optional[str] = None
    contacto: Optional[str] = None
    direccion: Optional[str] = None
    odoo_id: Optional[int] = None
    condicion_pago_dias: int = 30
    tipo_ncf_default: Optional[str] = "B11"
    cuenta_cxp_id: Optional[int] = None
    activo: bool
    model_config = {"from_attributes": True}


@router.get("", response_model=List[ProveedorOut])
def list_proveedores(db: Session = Depends(get_db), _=Depends(auth.get_current_user)):
    return db.query(models.Proveedor).filter(
        models.Proveedor.activo == True
    ).order_by(models.Proveedor.nombre).all()


@router.post("", response_model=ProveedorOut)
def create_proveedor(data: ProveedorCreate, db: Session = Depends(get_db),
                     _=Depends(auth.require_admin)):
    existing = db.query(models.Proveedor).filter(
        models.Proveedor.nombre == data.nombre).first()
    if existing:
        if not existing.activo:
            existing.activo = True
            existing.email = data.email
            existing.telefono = data.telefono
            existing.rnc = data.rnc
            existing.contacto = data.contacto
            db.commit()
            db.refresh(existing)
            return existing
        raise HTTPException(400, "Ya existe un proveedor con ese nombre")
    prov = models.Proveedor(**data.model_dump())
    db.add(prov)
    db.commit()
    db.refresh(prov)
    return prov


@router.put("/{prov_id}", response_model=ProveedorOut)
def update_proveedor(prov_id: int, data: ProveedorCreate, db: Session = Depends(get_db),
                     _=Depends(auth.require_admin)):
    prov = db.query(models.Proveedor).filter(models.Proveedor.id == prov_id).first()
    if not prov:
        raise HTTPException(404, "Proveedor no encontrado")
    for k, v in data.model_dump().items():
        setattr(prov, k, v)
    db.commit()
    db.refresh(prov)
    return prov


@router.delete("/{prov_id}")
def delete_proveedor(prov_id: int, db: Session = Depends(get_db),
                     _=Depends(auth.require_admin)):
    prov = db.query(models.Proveedor).filter(models.Proveedor.id == prov_id).first()
    if not prov:
        raise HTTPException(404, "Proveedor no encontrado")
    # Check if used in products
    count = db.query(models.Producto).filter(models.Producto.proveedor == prov.nombre).count()
    if count > 0:
        prov.activo = False
        db.commit()
        return {"ok": True, "message": f"Desactivado (asignado a {count} productos)"}
    db.delete(prov)
    db.commit()
    return {"ok": True}
