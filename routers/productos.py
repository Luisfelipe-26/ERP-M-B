from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from database import get_db
import models, schemas, auth
from typing import List

router = APIRouter(prefix="/api/productos", tags=["productos"])

TIPOS_MOVIMIENTO = ["entrada", "salida"]


@router.get("", response_model=List[schemas.ProductoOut])
def list_productos(db: Session = Depends(get_db), _=Depends(auth.get_current_user)):
    return db.query(models.Producto).filter(models.Producto.activo == True).order_by(models.Producto.producto).all()


@router.post("", response_model=schemas.ProductoOut)
def create_producto(data: schemas.ProductoCreate, db: Session = Depends(get_db), _=Depends(auth.require_admin)):
    if db.query(models.Producto).filter(models.Producto.id_prod == data.id_prod).first():
        raise HTTPException(status_code=400, detail="ID de producto ya existe")
    p = models.Producto(**data.model_dump())
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


@router.put("/{id_prod}", response_model=schemas.ProductoOut)
def update_producto(id_prod: str, data: schemas.ProductoCreate, db: Session = Depends(get_db), _=Depends(auth.require_admin)):
    p = db.query(models.Producto).filter(models.Producto.id_prod == id_prod).first()
    if not p:
        raise HTTPException(status_code=404, detail="Producto no encontrado")
    payload = data.model_dump()
    # Preserve stock_actual — update doesn't touch it
    payload.pop("stock_actual", None)
    for k, v in payload.items():
        setattr(p, k, v)
    db.commit()
    db.refresh(p)
    return p


@router.delete("/{id_prod}")
def delete_producto(id_prod: str, db: Session = Depends(get_db), _=Depends(auth.require_admin)):
    p = db.query(models.Producto).filter(models.Producto.id_prod == id_prod).first()
    if not p:
        raise HTTPException(status_code=404, detail="Producto no encontrado")
    p.activo = False
    db.commit()
    return {"ok": True}


@router.post("/{id_prod}/movimiento")
def registrar_movimiento(id_prod: str, data: schemas.MovimientoCreate, db: Session = Depends(get_db),
                          current_user: models.Usuario = Depends(auth.require_operador)):
    """Atajo desde la ficha del producto — delega en el módulo de inventario.

    Antes registraba el movimiento por su cuenta, sin número de documento, sin
    tipo_doc, sin recalcular el costo promedio y sin asiento contable: cada uso
    descuadraba el inventario contra contabilidad. Ahora pasa por el mismo camino
    que /inventario/gr y /inventario/gi, que sí hacen las cuatro cosas.
    """
    from routers.inventario import goods_receipt, goods_issue

    if data.tipo not in TIPOS_MOVIMIENTO:
        raise HTTPException(status_code=400, detail=f"Tipo debe ser: {TIPOS_MOVIMIENTO}")

    p = db.query(models.Producto).filter(
        models.Producto.id_prod == id_prod, models.Producto.activo == True).first()
    if not p:
        raise HTTPException(status_code=404, detail="Producto no encontrado")

    if data.tipo == "entrada":
        precio = data.costo_unitario
        if precio is None:
            precio = float(p.costo_promedio or p.costo_unitario or 0)
        return goods_receipt(schemas.GRCreate(
            producto_id=id_prod,
            cantidad=data.cantidad,
            precio_compra=precio,
            num_factura=data.referencia,
            observacion=data.observacion,
        ), db=db, current_user=current_user)

    # Una salida mueve stock y genera asiento: exige el mismo rol que /inventario/gi.
    auth.require_supervisor(current_user)
    return goods_issue(schemas.GICreate(
        producto_id=id_prod,
        cantidad=data.cantidad,
        motivo=data.motivo or "Otro",
        referencia=data.referencia,
        observacion=data.observacion,
    ), db=db, current_user=current_user)


@router.get("/{id_prod}/movimientos", response_model=List[schemas.MovimientoOut])
def get_movimientos(id_prod: str, db: Session = Depends(get_db), _=Depends(auth.get_current_user)):
    return db.query(models.MovimientoInventario).filter(
        models.MovimientoInventario.producto_id == id_prod
    ).order_by(models.MovimientoInventario.fecha.desc()).limit(100).all()
