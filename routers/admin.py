"""Admin — perfiles de acceso y gestión de usuarios."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, joinedload
from database import get_db
from auth import get_current_user, require_admin, get_password_hash
import models

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ── Perfiles de Acceso ──────────────────────────────────────────────────────

@router.get("/perfiles")
def listar_perfiles(db: Session = Depends(get_db), user=Depends(require_admin)):
    perfiles = db.query(models.PerfilAcceso).order_by(models.PerfilAcceso.nombre).all()
    return [
        {"id": p.id, "nombre": p.nombre, "descripcion": p.descripcion,
         "permisos": p.permisos or {}, "activo": p.activo}
        for p in perfiles
    ]


@router.post("/perfiles")
def crear_perfil(data: dict, db: Session = Depends(get_db), user=Depends(require_admin)):
    if not data.get("nombre"):
        raise HTTPException(400, "nombre es requerido")
    exists = db.query(models.PerfilAcceso).filter(
        models.PerfilAcceso.nombre == data["nombre"]).first()
    if exists:
        raise HTTPException(400, f"Ya existe un perfil con nombre '{data['nombre']}'")
    p = models.PerfilAcceso(
        nombre=data["nombre"],
        descripcion=data.get("descripcion"),
        permisos=data.get("permisos", {}),
        activo=data.get("activo", True),
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return {"ok": True, "id": p.id}


@router.put("/perfiles/{perfil_id}")
def actualizar_perfil(perfil_id: int, data: dict, db: Session = Depends(get_db),
                      user=Depends(require_admin)):
    p = db.query(models.PerfilAcceso).get(perfil_id)
    if not p:
        raise HTTPException(404, "Perfil no encontrado")
    if "nombre" in data:
        dup = db.query(models.PerfilAcceso).filter(
            models.PerfilAcceso.nombre == data["nombre"],
            models.PerfilAcceso.id != perfil_id).first()
        if dup:
            raise HTTPException(400, f"Ya existe otro perfil con nombre '{data['nombre']}'")
        p.nombre = data["nombre"]
    if "descripcion" in data:
        p.descripcion = data["descripcion"]
    if "permisos" in data:
        p.permisos = data["permisos"]
    if "activo" in data:
        p.activo = data["activo"]
    db.commit()
    return {"ok": True}


@router.delete("/perfiles/{perfil_id}")
def eliminar_perfil(perfil_id: int, db: Session = Depends(get_db), user=Depends(require_admin)):
    p = db.query(models.PerfilAcceso).get(perfil_id)
    if not p:
        raise HTTPException(404, "Perfil no encontrado")
    used = db.query(models.Usuario).filter(models.Usuario.perfil_id == perfil_id).first()
    if used:
        raise HTTPException(400, "No se puede eliminar un perfil asignado a usuarios")
    db.delete(p)
    db.commit()
    return {"ok": True}


# ── Gestión de Usuarios ─────────────────────────────────────────────────────

@router.get("/usuarios")
def listar_usuarios(db: Session = Depends(get_db), user=Depends(require_admin)):
    usuarios = db.query(models.Usuario).options(joinedload(models.Usuario.perfil)).order_by(models.Usuario.nombre).all()
    return [
        {"id": u.id, "nombre": u.nombre, "email": u.email, "rol": u.rol,
         "perfil_id": u.perfil_id,
         "perfil_nombre": u.perfil.nombre if u.perfil else None,
         "activo": u.activo, "creado_en": u.creado_en.isoformat() if u.creado_en else None}
        for u in usuarios
    ]


@router.post("/usuarios")
def crear_usuario(data: dict, db: Session = Depends(get_db), user=Depends(require_admin)):
    if not data.get("nombre") or not data.get("email") or not data.get("password"):
        raise HTTPException(400, "nombre, email y password son requeridos")
    exists = db.query(models.Usuario).filter(models.Usuario.email == data["email"]).first()
    if exists:
        raise HTTPException(400, f"Ya existe un usuario con email '{data['email']}'")
    rol = data.get("rol", "operador")
    u = models.Usuario(
        nombre=data["nombre"],
        email=data["email"],
        hashed_password=get_password_hash(data["password"]),
        rol=rol,
        perfil_id=data.get("perfil_id"),
        activo=data.get("activo", True),
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return {"ok": True, "id": u.id}


@router.put("/usuarios/{usuario_id}")
def actualizar_usuario(usuario_id: int, data: dict, db: Session = Depends(get_db),
                       user=Depends(require_admin)):
    u = db.query(models.Usuario).get(usuario_id)
    if not u:
        raise HTTPException(404, "Usuario no encontrado")
    if "nombre" in data:
        u.nombre = data["nombre"]
    if "email" in data:
        dup = db.query(models.Usuario).filter(
            models.Usuario.email == data["email"],
            models.Usuario.id != usuario_id).first()
        if dup:
            raise HTTPException(400, f"Ya existe otro usuario con email '{data['email']}'")
        u.email = data["email"]
    if "rol" in data:
        u.rol = data["rol"]
    if "perfil_id" in data:
        u.perfil_id = data["perfil_id"]
    if "activo" in data:
        u.activo = data["activo"]
    db.commit()
    return {"ok": True}


@router.post("/usuarios/{usuario_id}/reset-password")
def reset_password(usuario_id: int, data: dict, db: Session = Depends(get_db),
                   user=Depends(require_admin)):
    u = db.query(models.Usuario).get(usuario_id)
    if not u:
        raise HTTPException(404, "Usuario no encontrado")
    if not data.get("password"):
        raise HTTPException(400, "password es requerido")
    u.hashed_password = get_password_hash(data["password"])
    db.commit()
    return {"ok": True, "msg": f"Contraseña de {u.nombre} actualizada"}
