"""
Órdenes de Compra (OC) — CRUD completo con líneas de producto.
H-16 FIX: Implementación del ciclo de compra vinculado a GR.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from database import get_db
import models, schemas, auth
from routers.sequences import get_next, peek_next
from typing import List, Optional
from datetime import datetime
import audit

router = APIRouter(prefix="/api/ordenes-compra", tags=["ordenes-compra"])

ESTADOS_OC = ["Pendiente", "Parcial", "Recibida", "Cancelada"]


@router.get("/preview/next-id")
def next_oc_id_preview(db: Session = Depends(get_db), _=Depends(auth.get_current_user)):
    return {"next_oc_id": peek_next("OC", db)}


@router.get("", response_model=List[schemas.OrdenCompraOut])
def list_ocs(
    estado: Optional[str] = None,
    proveedor: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    _=Depends(auth.get_current_user)
):
    q = db.query(models.OrdenCompra)
    if estado:
        q = q.filter(models.OrdenCompra.estado == estado)
    if proveedor:
        q = q.filter(models.OrdenCompra.proveedor.ilike(f"%{proveedor}%"))
    return q.order_by(models.OrdenCompra.fecha.desc()).offset(skip).limit(limit).all()


@router.post("", response_model=schemas.OrdenCompraOut)
def create_oc(data: schemas.OrdenCompraCreate, db: Session = Depends(get_db),
              current_user: models.Usuario = Depends(auth.require_supervisor)):
    if not data.lineas:
        raise HTTPException(status_code=400, detail="La orden de compra debe tener al menos una línea")

    oc_id = get_next("OC", db)
    total = 0.0

    oc = models.OrdenCompra(
        oc_id=oc_id,
        fecha=data.fecha or datetime.now(),
        proveedor=data.proveedor,
        campo_id=data.campo_id,
        estado="Pendiente",
        observaciones=data.observaciones,
    )
    db.add(oc)
    db.flush()

    for linea in data.lineas:
        prod = db.query(models.Producto).filter(
            models.Producto.id_prod == linea.producto_id, models.Producto.activo == True
        ).first()
        if not prod:
            raise HTTPException(status_code=400, detail=f"Producto '{linea.producto_id}' no existe")
        if linea.cantidad <= 0:
            raise HTTPException(status_code=400, detail=f"Cantidad debe ser mayor a 0 para '{prod.producto}'")
        if linea.precio_unitario < 0:
            raise HTTPException(status_code=400, detail=f"Precio no puede ser negativo para '{prod.producto}'")

        subtotal = round(linea.cantidad * linea.precio_unitario, 2)
        total += subtotal

        db.add(models.OrdenCompraLinea(
            oc_id=oc_id,
            producto_id=linea.producto_id,
            cantidad=linea.cantidad,
            cantidad_recibida=0,
            precio_unitario=linea.precio_unitario,
            subtotal=subtotal,
        ))

    oc.total_estimado = round(total, 2)

    audit.log(db, current_user, "CREAR", "OC", oc_id,
              f"OC {oc_id} creada: {data.proveedor or 'Sin proveedor'} — Total: RD$ {total:,.2f}",
              {"proveedor": data.proveedor, "campo_id": data.campo_id, "total_estimado": total,
               "num_lineas": len(data.lineas)})

    db.commit()
    db.refresh(oc)
    return oc


@router.get("/{oc_id}")
def get_oc(oc_id: str, db: Session = Depends(get_db), _=Depends(auth.get_current_user)):
    oc = db.query(models.OrdenCompra).filter(models.OrdenCompra.oc_id == oc_id).first()
    if not oc:
        raise HTTPException(status_code=404, detail="Orden de compra no encontrada")
    lineas = db.query(models.OrdenCompraLinea).filter(models.OrdenCompraLinea.oc_id == oc_id).all()

    lineas_out = []
    for l in lineas:
        prod = db.query(models.Producto).filter(models.Producto.id_prod == l.producto_id).first()
        lineas_out.append({
            "id": l.id,
            "oc_id": l.oc_id,
            "producto_id": l.producto_id,
            "producto_nombre": prod.producto if prod else l.producto_id,
            "unidad": prod.unidad if prod else "",
            "cantidad": l.cantidad,
            "cantidad_recibida": l.cantidad_recibida or 0,
            "cantidad_pendiente": round(l.cantidad - (l.cantidad_recibida or 0), 4),
            "precio_unitario": l.precio_unitario,
            "subtotal": l.subtotal,
        })

    return {
        "orden": schemas.OrdenCompraOut.model_validate(oc),
        "lineas": lineas_out,
    }


@router.put("/{oc_id}/estado")
def update_oc_estado(oc_id: str, estado: str = Query(...),
                      db: Session = Depends(get_db), _=Depends(auth.require_supervisor)):
    if estado not in ESTADOS_OC:
        raise HTTPException(status_code=400, detail=f"Estado inválido. Permitidos: {ESTADOS_OC}")
    oc = db.query(models.OrdenCompra).filter(models.OrdenCompra.oc_id == oc_id).first()
    if not oc:
        raise HTTPException(status_code=404, detail="Orden de compra no encontrada")
    oc.estado = estado
    db.commit()
    return {"ok": True, "estado": estado}


from pydantic import BaseModel as PydanticBase
from typing import List as TList

class RecepcionLinea(PydanticBase):
    linea_id: int
    cantidad_recibida: float

class RecepcionPayload(PydanticBase):
    num_factura: Optional[str] = None
    lineas: TList[RecepcionLinea] = []


@router.post("/{oc_id}/recepcion")
def recibir_oc(oc_id: str, data: RecepcionPayload, db: Session = Depends(get_db),
               current_user: models.Usuario = Depends(auth.require_supervisor)):
    """Receive items against an OC — updates quantities, generates GR for inventariables."""
    oc = db.query(models.OrdenCompra).filter(models.OrdenCompra.oc_id == oc_id).first()
    if not oc:
        raise HTTPException(status_code=404, detail="Orden de compra no encontrada")
    if oc.estado == "Cancelada":
        raise HTTPException(status_code=400, detail="No se puede recibir una OC cancelada")

    from routers.inventario import _recalc_avg_cost
    from routers.sequences import get_next

    total_recibido_now = 0.0
    for item in data.lineas:
        if item.cantidad_recibida <= 0:
            continue
        linea = db.query(models.OrdenCompraLinea).filter(
            models.OrdenCompraLinea.id == item.linea_id,
            models.OrdenCompraLinea.oc_id == oc_id
        ).first()
        if not linea:
            continue

        # Update received quantity
        linea.cantidad_recibida = (linea.cantidad_recibida or 0) + item.cantidad_recibida
        total_recibido_now += item.cantidad_recibida * linea.precio_unitario

        # Generate GR for inventariable products
        prod = db.query(models.Producto).filter(
            models.Producto.id_prod == linea.producto_id, models.Producto.activo == True
        ).first()
        if prod and prod.es_inventariable:
            nuevo_costo = _recalc_avg_cost(prod, item.cantidad_recibida, linea.precio_unitario)
            nuevo_stock = (prod.stock_actual or 0) + item.cantidad_recibida
            num_doc = get_next("GR", db)

            mov = models.MovimientoInventario(
                num_documento=num_doc,
                producto_id=linea.producto_id,
                tipo_doc="GR",
                tipo="entrada",
                motivo="Compra",
                cantidad=item.cantidad_recibida,
                costo_unitario=linea.precio_unitario,
                costo_promedio_post=round(nuevo_costo, 4),
                stock_post=round(nuevo_stock, 4),
                proveedor=oc.proveedor,
                fecha=datetime.now(),
                oc_referencia=oc_id,
                usuario_id=current_user.id,
            )
            db.add(mov)
            prod.stock_actual = round(nuevo_stock, 4)
            prod.costo_promedio = round(nuevo_costo, 4)

    # Update OC totals
    oc.total_recibido = (oc.total_recibido or 0) + total_recibido_now
    oc.fecha_recepcion = datetime.now()
    if data.num_factura:
        oc.num_factura = data.num_factura

    # Auto-determine estado
    lineas_all = db.query(models.OrdenCompraLinea).filter(models.OrdenCompraLinea.oc_id == oc_id).all()
    all_received = all((l.cantidad_recibida or 0) >= l.cantidad for l in lineas_all)
    any_received = any((l.cantidad_recibida or 0) > 0 for l in lineas_all)

    if all_received:
        oc.estado = "Recibida"
    elif any_received:
        oc.estado = "Parcial"

    db.commit()
    db.refresh(oc)
    return {"ok": True, "estado": oc.estado, "total_recibido": oc.total_recibido, "num_factura": oc.num_factura}


@router.put("/{oc_id}")
def update_oc(oc_id: str, data: schemas.OrdenCompraCreate, db: Session = Depends(get_db),
              current_user: models.Usuario = Depends(auth.get_current_user)):
    """Edit OC — admin can edit even closed OCs."""
    oc = db.query(models.OrdenCompra).filter(models.OrdenCompra.oc_id == oc_id).first()
    if not oc:
        raise HTTPException(status_code=404, detail="Orden de compra no encontrada")

    # Only admin can edit closed/received OCs
    if oc.estado in ["Recibida", "Cancelada"] and current_user.rol != "admin":
        raise HTTPException(status_code=403, detail="Solo un administrador puede editar OCs cerradas")

    oc.fecha = data.fecha or oc.fecha
    oc.proveedor = data.proveedor or oc.proveedor
    oc.campo_id = data.campo_id
    oc.observaciones = data.observaciones

    # Replace lines if provided
    if data.lineas:
        db.query(models.OrdenCompraLinea).filter(models.OrdenCompraLinea.oc_id == oc_id).delete()
        total = 0.0
        for linea in data.lineas:
            prod = db.query(models.Producto).filter(
                models.Producto.id_prod == linea.producto_id, models.Producto.activo == True
            ).first()
            if not prod:
                raise HTTPException(status_code=400, detail=f"Producto '{linea.producto_id}' no existe")
            subtotal = round(linea.cantidad * linea.precio_unitario, 2)
            total += subtotal
            db.add(models.OrdenCompraLinea(
                oc_id=oc_id, producto_id=linea.producto_id,
                cantidad=linea.cantidad, cantidad_recibida=0,
                precio_unitario=linea.precio_unitario, subtotal=subtotal,
            ))
        oc.total_estimado = round(total, 2)

    audit.log(db, current_user, "MODIFICAR", "OC", oc_id,
              f"OC {oc_id} editada: proveedor={oc.proveedor}, campo={oc.campo_id}",
              {"proveedor": oc.proveedor, "campo_id": oc.campo_id,
               "total_estimado": float(oc.total_estimado or 0)})

    db.commit()
    db.refresh(oc)
    return oc


@router.delete("/{oc_id}")
def delete_oc(oc_id: str, db: Session = Depends(get_db),
              current_user: models.Usuario = Depends(auth.require_admin)):
    oc = db.query(models.OrdenCompra).filter(models.OrdenCompra.oc_id == oc_id).first()
    if not oc:
        raise HTTPException(status_code=404, detail="Orden de compra no encontrada")

    audit.log(db, current_user, "ELIMINAR", "OC", oc_id,
              f"OC {oc_id} eliminada: {oc.proveedor or 'Sin proveedor'} — Total era: RD$ {oc.total_estimado or 0:,.2f}",
              {"proveedor": oc.proveedor, "estado": oc.estado,
               "total_estimado": float(oc.total_estimado or 0)})

    db.query(models.OrdenCompraLinea).filter(models.OrdenCompraLinea.oc_id == oc_id).delete()
    db.delete(oc)
    db.commit()
    return {"ok": True}
