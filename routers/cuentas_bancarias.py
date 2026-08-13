"""
Módulo Contabilidad — Cuentas Bancarias CRUD.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from database import get_db
from auth import get_current_user, require_admin
import models, schemas

router = APIRouter(prefix="/api/cuentas-bancarias", tags=["cuentas_bancarias"])


@router.get("")
def listar_cuentas_bancarias(activo: bool = True, db: Session = Depends(get_db),
                             user=Depends(get_current_user)):
    q = db.query(models.CuentaBancaria)
    if activo:
        q = q.filter(models.CuentaBancaria.activo == True)
    return [schemas.CuentaBancariaOut.model_validate(c) for c in q.order_by(models.CuentaBancaria.banco).all()]


@router.post("")
def crear_cuenta_bancaria(data: schemas.CuentaBancariaCreate, db: Session = Depends(get_db),
                          user=Depends(require_admin)):
    cb = models.CuentaBancaria(**data.model_dump())
    db.add(cb)
    db.commit()
    db.refresh(cb)
    return schemas.CuentaBancariaOut.model_validate(cb)


@router.put("/{id}")
def actualizar_cuenta_bancaria(id: int, data: schemas.CuentaBancariaCreate,
                               db: Session = Depends(get_db), user=Depends(require_admin)):
    cb = db.query(models.CuentaBancaria).get(id)
    if not cb:
        raise HTTPException(404, "Cuenta bancaria no encontrada")
    for k, v in data.model_dump().items():
        setattr(cb, k, v)
    db.commit()
    db.refresh(cb)
    return schemas.CuentaBancariaOut.model_validate(cb)


@router.delete("/{id}")
def desactivar_cuenta_bancaria(id: int, db: Session = Depends(get_db), user=Depends(require_admin)):
    cb = db.query(models.CuentaBancaria).get(id)
    if not cb:
        raise HTTPException(404, "Cuenta bancaria no encontrada")
    cb.activo = False
    db.commit()
    return {"ok": True, "msg": f"Cuenta bancaria {cb.numero_cuenta} desactivada"}
