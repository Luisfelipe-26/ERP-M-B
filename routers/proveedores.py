"""Proveedores — CRUD + sync Odoo."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func as sqlfunc
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


@router.get("/{prov_id}/resumen")
def resumen_proveedor(prov_id: int, db: Session = Depends(get_db),
                      _=Depends(auth.get_current_user)):
    prov = db.query(models.Proveedor).filter(models.Proveedor.id == prov_id).first()
    if not prov:
        raise HTTPException(404, "Proveedor no encontrado")

    ocs = db.query(models.OrdenCompra).filter(
        models.OrdenCompra.proveedor == prov.nombre,
    ).order_by(models.OrdenCompra.fecha.desc()).limit(10).all()

    ocs_out = [{
        "oc_id": o.oc_id, "fecha": str(o.fecha.date()) if o.fecha else None,
        "estado": o.estado, "total": float(o.total_estimado or 0),
    } for o in ocs]

    saldo_cxp = float(db.query(
        sqlfunc.coalesce(sqlfunc.sum(models.CuentaPorPagar.saldo_pendiente), 0)
    ).filter(
        models.CuentaPorPagar.proveedor_id == prov_id,
        models.CuentaPorPagar.estado.in_(["pendiente", "parcial"]),
    ).scalar() or 0)

    total_compras = float(db.query(
        sqlfunc.coalesce(sqlfunc.sum(models.OrdenCompra.total_estimado), 0)
    ).filter(models.OrdenCompra.proveedor == prov.nombre).scalar() or 0)

    num_ocs = db.query(models.OrdenCompra).filter(
        models.OrdenCompra.proveedor == prov.nombre
    ).count()

    return {
        "saldo_cxp": saldo_cxp,
        "total_compras": total_compras,
        "num_ocs": num_ocs,
        "ultimas_ocs": ocs_out,
    }
