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


@router.post("/{id_prod}/movimiento", response_model=schemas.MovimientoOut)
def registrar_movimiento(id_prod: str, data: schemas.MovimientoCreate, db: Session = Depends(get_db),
                          current_user: models.Usuario = Depends(auth.get_current_user)):
    if data.tipo not in TIPOS_MOVIMIENTO:
        raise HTTPException(status_code=400, detail=f"Tipo debe ser: {TIPOS_MOVIMIENTO}")
    if not data.cantidad or data.cantidad <= 0:
        raise HTTPException(status_code=400, detail="La cantidad debe ser mayor a 0")

    p = db.query(models.Producto).filter(models.Producto.id_prod == id_prod, models.Producto.activo == True).first()
    if not p:
        raise HTTPException(status_code=404, detail="Producto no encontrado")

    if data.tipo == "salida" and (p.stock_actual or 0) < data.cantidad:
        raise HTTPException(status_code=400, detail=f"Stock insuficiente. Stock actual: {p.stock_actual} {p.unidad}")

    mov = models.MovimientoInventario(
        producto_id=id_prod,
        tipo=data.tipo,
        cantidad=data.cantidad,
        costo_unitario=data.costo_unitario or p.costo_unitario,
        referencia=data.referencia,
        observacion=data.observacion,
        usuario_id=current_user.id
    )
    db.add(mov)

    if data.tipo == "entrada":
        p.stock_actual = (p.stock_actual or 0) + data.cantidad
    else:
        p.stock_actual = (p.stock_actual or 0) - data.cantidad

    db.commit()
    db.refresh(mov)
    return mov


@router.get("/{id_prod}/movimientos", response_model=List[schemas.MovimientoOut])
def get_movimientos(id_prod: str, db: Session = Depends(get_db), _=Depends(auth.get_current_user)):
    return db.query(models.MovimientoInventario).filter(
        models.MovimientoInventario.producto_id == id_prod
    ).order_by(models.MovimientoInventario.fecha.desc()).limit(100).all()
