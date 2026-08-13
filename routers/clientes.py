"""
Módulo Contabilidad — Clientes CRUD con secuencia auto CLI-xxxx.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from database import get_db
from auth import get_current_user, require_admin
import models, schemas

router = APIRouter(prefix="/api/clientes", tags=["clientes"])


def get_next_cli(db: Session) -> str:
    seq = db.query(models.Sequence).filter(models.Sequence.tipo == "CLI").first()
    if not seq:
        seq = models.Sequence(tipo="CLI", ultimo_numero=0)
        db.add(seq)
    seq.ultimo_numero += 1
    db.flush()
    return f"CLI-{seq.ultimo_numero:04d}"


@router.get("")
def listar_clientes(activo: bool = True, db: Session = Depends(get_db),
                    user=Depends(get_current_user)):
    q = db.query(models.Cliente)
    if activo:
        q = q.filter(models.Cliente.activo == True)
    return [schemas.ClienteOut.model_validate(c) for c in q.order_by(models.Cliente.nombre).all()]


@router.get("/{id}")
def ver_cliente(id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    c = db.query(models.Cliente).get(id)
    if not c:
        raise HTTPException(404, "Cliente no encontrado")
    return schemas.ClienteOut.model_validate(c)


@router.post("")
def crear_cliente(data: schemas.ClienteCreate, db: Session = Depends(get_db),
                  user=Depends(get_current_user)):
    if user.rol == "operador":
        raise HTTPException(403, "Acceso denegado")
    id_cliente = get_next_cli(db)
    cliente = models.Cliente(id_cliente=id_cliente, **data.model_dump())
    db.add(cliente)
    db.commit()
    db.refresh(cliente)
    return schemas.ClienteOut.model_validate(cliente)


@router.put("/{id}")
def actualizar_cliente(id: int, data: schemas.ClienteCreate,
                       db: Session = Depends(get_db), user=Depends(get_current_user)):
    if user.rol == "operador":
        raise HTTPException(403, "Acceso denegado")
    c = db.query(models.Cliente).get(id)
    if not c:
        raise HTTPException(404, "Cliente no encontrado")
    for k, v in data.model_dump().items():
        setattr(c, k, v)
    db.commit()
    db.refresh(c)
    return schemas.ClienteOut.model_validate(c)


@router.delete("/{id}")
def desactivar_cliente(id: int, db: Session = Depends(get_db), user=Depends(require_admin)):
    c = db.query(models.Cliente).get(id)
    if not c:
        raise HTTPException(404, "Cliente no encontrado")
    c.activo = False
    db.commit()
    return {"ok": True, "msg": f"Cliente {c.id_cliente} desactivado"}
