"""Proveedores — CRUD + resumen financiero."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func as sqlfunc
from database import get_db
import models, auth, re
from pydantic import BaseModel, field_validator
from typing import Optional, List

router = APIRouter(prefix="/api/proveedores", tags=["proveedores"])

RNC_RE = re.compile(r"^\d{9}$|^\d{11}$|^\d{3}-\d{7}-\d$")


RETENCIONES_DEFAULT = {
    ("juridica", "formal"):   {"isr": 0,  "itbis": 30, "ncf": "E31"},
    ("juridica", "informal"): {"isr": 2,  "itbis": 0,  "ncf": "E41"},
    ("fisica", "formal"):     {"isr": 15, "itbis": 100, "ncf": "E41"},
    ("fisica", "informal"):   {"isr": 2,  "itbis": 0,  "ncf": "E41"},
}


class ProveedorCreate(BaseModel):
    nombre: str
    rnc: Optional[str] = None
    email: Optional[str] = None
    telefono: Optional[str] = None
    contacto: Optional[str] = None
    direccion: Optional[str] = None
    tipo_persona: str = "juridica"
    tipo_contribuyente: str = "formal"
    moneda_default: str = "DOP"
    retencion_isr_pct: Optional[float] = None
    retencion_itbis_pct: Optional[float] = None
    condicion_pago_dias: int = 30
    tipo_ncf_default: Optional[str] = None
    cuenta_cxp_id: Optional[int] = None

    @field_validator("rnc")
    @classmethod
    def validar_rnc(cls, v):
        if v and v.strip():
            limpio = v.strip()
            if not RNC_RE.match(limpio):
                raise ValueError("RNC debe ser 9 dígitos, 11 dígitos, o formato 000-0000000-0")
        return v

    @field_validator("tipo_persona")
    @classmethod
    def validar_tipo_persona(cls, v):
        if v not in ("juridica", "fisica"):
            raise ValueError("tipo_persona debe ser 'juridica' o 'fisica'")
        return v

    @field_validator("tipo_contribuyente")
    @classmethod
    def validar_tipo_contribuyente(cls, v):
        if v not in ("formal", "informal"):
            raise ValueError("tipo_contribuyente debe ser 'formal' o 'informal'")
        return v


class ProveedorOut(BaseModel):
    id: int
    nombre: str
    rnc: Optional[str] = None
    email: Optional[str] = None
    telefono: Optional[str] = None
    contacto: Optional[str] = None
    direccion: Optional[str] = None
    tipo_persona: str = "juridica"
    tipo_contribuyente: str = "formal"
    moneda_default: str = "DOP"
    retencion_isr_pct: Optional[float] = 0
    retencion_itbis_pct: Optional[float] = 30
    condicion_pago_dias: int = 30
    tipo_ncf_default: Optional[str] = "E31"
    cuenta_cxp_id: Optional[int] = None
    activo: bool
    model_config = {"from_attributes": True}


@router.get("", response_model=List[ProveedorOut])
def list_proveedores(db: Session = Depends(get_db), _=Depends(auth.get_current_user)):
    return db.query(models.Proveedor).filter(
        models.Proveedor.activo == True
    ).order_by(models.Proveedor.nombre).all()


def _apply_retention_defaults(data: ProveedorCreate) -> dict:
    """Fill in retention defaults based on tipo_persona + tipo_contribuyente."""
    d = data.model_dump()
    defaults = RETENCIONES_DEFAULT.get(
        (d["tipo_persona"], d["tipo_contribuyente"]),
        {"isr": 0, "itbis": 30, "ncf": "E31"},
    )
    if d.get("retencion_isr_pct") is None:
        d["retencion_isr_pct"] = defaults["isr"]
    if d.get("retencion_itbis_pct") is None:
        d["retencion_itbis_pct"] = defaults["itbis"]
    if d.get("tipo_ncf_default") is None:
        d["tipo_ncf_default"] = defaults["ncf"]
    return d


@router.post("", response_model=ProveedorOut)
def create_proveedor(data: ProveedorCreate, db: Session = Depends(get_db),
                     _=Depends(auth.require_admin)):
    existing = db.query(models.Proveedor).filter(
        models.Proveedor.nombre == data.nombre).first()
    if existing:
        if not existing.activo:
            existing.activo = True
            for k, v in _apply_retention_defaults(data).items():
                setattr(existing, k, v)
            db.commit()
            db.refresh(existing)
            return existing
        raise HTTPException(400, "Ya existe un proveedor con ese nombre")
    prov = models.Proveedor(**_apply_retention_defaults(data))
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

    nombre_viejo = prov.nombre
    nombre_nuevo = data.nombre.strip()
    if nombre_nuevo != nombre_viejo:
        ocs_vinculadas = db.query(models.OrdenCompra).filter(
            models.OrdenCompra.proveedor == nombre_viejo,
            models.OrdenCompra.estado.in_(["Borrador", "Aprobada", "Parcial"]),
        ).count()
        if ocs_vinculadas > 0:
            raise HTTPException(
                400,
                f"No se puede renombrar: tiene {ocs_vinculadas} OC(s) pendientes vinculadas por nombre. "
                "Cierre o cancele las OCs primero.",
            )
        db.query(models.OrdenCompra).filter(
            models.OrdenCompra.proveedor == nombre_viejo,
        ).update({"proveedor": nombre_nuevo}, synchronize_session=False)

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

    saldo_pend = float(db.query(
        sqlfunc.coalesce(sqlfunc.sum(models.CuentaPorPagar.saldo_pendiente), 0)
    ).filter(
        models.CuentaPorPagar.proveedor_id == prov_id,
        models.CuentaPorPagar.estado.in_(["pendiente", "parcial"]),
    ).scalar() or 0)
    if saldo_pend > 0:
        raise HTTPException(
            400, f"No se puede desactivar: tiene CxP pendientes por RD$ {saldo_pend:,.2f}"
        )

    ocs_abiertas = db.query(models.OrdenCompra).filter(
        models.OrdenCompra.proveedor == prov.nombre,
        models.OrdenCompra.estado.in_(["Borrador", "Aprobada", "Parcial"]),
    ).count()
    if ocs_abiertas > 0:
        raise HTTPException(
            400, f"No se puede desactivar: tiene {ocs_abiertas} OC(s) pendientes"
        )

    count = db.query(models.Producto).filter(models.Producto.proveedor == prov.nombre).count()
    cxp_count = db.query(models.CuentaPorPagar).filter(
        models.CuentaPorPagar.proveedor_id == prov_id
    ).count()
    if count > 0 or cxp_count > 0:
        prov.activo = False
        db.commit()
        motivos = []
        if count > 0:
            motivos.append(f"{count} productos")
        if cxp_count > 0:
            motivos.append(f"{cxp_count} CxP históricas")
        return {"ok": True, "message": f"Desactivado ({', '.join(motivos)})"}
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

    cxp_items = db.query(models.CuentaPorPagar).filter(
        models.CuentaPorPagar.proveedor_id == prov_id,
    ).order_by(models.CuentaPorPagar.fecha_factura.desc()).limit(20).all()

    from datetime import date as date_type
    hoy = date_type.today()
    cxp_out = []
    for c in cxp_items:
        dias_venc = (hoy - c.fecha_vencimiento).days if c.fecha_vencimiento else 0
        pagos = [{
            "numero": p.numero, "fecha": str(p.fecha), "monto": float(p.monto or 0),
            "metodo": p.metodo_pago or "—", "referencia": p.referencia_bancaria or "",
        } for p in (c.pagos or [])]
        cxp_out.append({
            "id": c.id, "numero": c.numero,
            "fecha_factura": str(c.fecha_factura) if c.fecha_factura else None,
            "fecha_vencimiento": str(c.fecha_vencimiento) if c.fecha_vencimiento else None,
            "num_factura": c.num_factura_proveedor,
            "tipo_ncf": c.tipo_ncf, "ncf": c.ncf,
            "total": float(c.total or 0),
            "saldo": float(c.saldo_pendiente or 0),
            "estado": c.estado,
            "dias_vencido": dias_venc if dias_venc > 0 and c.estado in ("pendiente", "parcial") else 0,
            "pagos": pagos,
        })

    return {
        "saldo_cxp": saldo_cxp,
        "total_compras": total_compras,
        "num_ocs": num_ocs,
        "ultimas_ocs": ocs_out,
        "cuentas_por_pagar": cxp_out,
    }
