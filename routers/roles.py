"""Roles y Permisos — CRUD completo con asignación de permisos a roles y usuarios."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, joinedload
from database import get_db
from auth import get_current_user, require_admin, require_permission, get_password_hash, _get_user_permissions
import models, schemas

router = APIRouter(prefix="/api/rbac", tags=["roles-y-permisos"])


# ── Permisor (read-only — managed by system) ──────────────────────────────

@router.get("/permisos")
def listar_permisos(
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    permisos = db.query(models.Permiso).order_by(models.Permiso.modulo, models.Permiso.accion).all()
    return [
        {"id": p.id, "codigo": p.codigo, "modulo": p.modulo, "accion": p.accion,
         "descripcion": p.descripcion, "activo": p.activo}
        for p in permisos
    ]


@router.get("/permisos/agrupados")
def permisos_agrupados(
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    """Return permissions grouped by module for easier UI display."""
    permisos = db.query(models.Permiso).order_by(models.Permiso.modulo, models.Permiso.accion).all()
    grouped = {}
    for p in permisos:
        if p.modulo not in grouped:
            grouped[p.modulo] = []
        grouped[p.modulo].append({
            "id": p.id, "codigo": p.codigo, "accion": p.accion,
            "descripcion": p.descripcion, "activo": p.activo,
        })
    return grouped


# ── Roles ─────────────────────────────────────────────────────────────────

@router.get("/roles")
def listar_roles(
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    roles = db.query(models.Rol).options(
        joinedload(models.Rol.permisos)
    ).order_by(models.Rol.nombre).all()
    return [
        {
            "id": r.id, "nombre": r.nombre, "descripcion": r.descripcion,
            "es_sistema": r.es_sistema, "activo": r.activo,
            "permisos": [
                {"id": p.id, "codigo": p.codigo, "modulo": p.modulo, "accion": p.accion}
                for p in r.permisos
            ],
            "num_usuarios": len(r.usuarios),
        }
        for r in roles
    ]


@router.post("/roles")
def crear_rol(data: schemas.RolCreate, db: Session = Depends(get_db), _=Depends(require_admin)):
    if db.query(models.Rol).filter(models.Rol.nombre == data.nombre).first():
        raise HTTPException(400, f"Ya existe un rol con nombre '{data.nombre}'")
    rol = models.Rol(nombre=data.nombre, descripcion=data.descripcion)
    if data.permiso_ids:
        permisos = db.query(models.Permiso).filter(models.Permiso.id.in_(data.permiso_ids)).all()
        rol.permisos = permisos
    db.add(rol)
    db.commit()
    db.refresh(rol)
    return {"ok": True, "id": rol.id}


@router.put("/roles/{rol_id}")
def actualizar_rol(rol_id: int, data: schemas.RolUpdate, db: Session = Depends(get_db),
                   _=Depends(require_admin)):
    rol = db.query(models.Rol).get(rol_id)
    if not rol:
        raise HTTPException(404, "Rol no encontrado")
    if data.nombre is not None:
        dup = db.query(models.Rol).filter(
            models.Rol.nombre == data.nombre, models.Rol.id != rol_id).first()
        if dup:
            raise HTTPException(400, f"Ya existe otro rol con nombre '{data.nombre}'")
        rol.nombre = data.nombre
    if data.descripcion is not None:
        rol.descripcion = data.descripcion
    if data.activo is not None:
        rol.activo = data.activo
    if data.permiso_ids is not None:
        permisos = db.query(models.Permiso).filter(models.Permiso.id.in_(data.permiso_ids)).all()
        rol.permisos = permisos
    db.commit()
    return {"ok": True}


@router.delete("/roles/{rol_id}")
def eliminar_rol(rol_id: int, db: Session = Depends(get_db), _=Depends(require_admin)):
    rol = db.query(models.Rol).get(rol_id)
    if not rol:
        raise HTTPException(404, "Rol no encontrado")
    if rol.es_sistema:
        raise HTTPException(400, "No se puede eliminar un rol del sistema")
    if rol.usuarios:
        raise HTTPException(400, "No se puede eliminar un rol asignado a usuarios")
    db.delete(rol)
    db.commit()
    return {"ok": True}


# ── Asignación de roles a usuarios ────────────────────────────────────────

@router.get("/usuarios/{usuario_id}/roles")
def obtener_roles_usuario(usuario_id: int, db: Session = Depends(get_db), _=Depends(require_admin)):
    user = db.query(models.Usuario).options(
        joinedload(models.Usuario.roles).joinedload(models.Rol.permisos)
    ).filter(models.Usuario.id == usuario_id).first()
    if not user:
        raise HTTPException(404, "Usuario no encontrado")
    return {
        "usuario_id": user.id,
        "nombre": user.nombre,
        "roles": [
            {
                "id": r.id, "nombre": r.nombre, "descripcion": r.descripcion,
                "permisos": [p.codigo for p in r.permisos if p.activo],
            }
            for r in user.roles if r.activo
        ],
        "permisos_directas": sorted(_get_user_permissions(user)),
    }


@router.put("/usuarios/{usuario_id}/roles")
def asignar_roles_usuario(
    usuario_id: int,
    data: schemas.UsuarioRolAssign,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    user = db.query(models.Usuario).filter(models.Usuario.id == usuario_id).first()
    if not user:
        raise HTTPException(404, "Usuario no encontrado")
    roles = db.query(models.Rol).filter(models.Rol.id.in_(data.rol_ids)).all()
    user.roles = roles
    # Sync legacy rol field from first role
    if roles:
        user.rol = roles[0].nombre
    db.commit()
    return {"ok": True, "msg": f"Roles actualizados para {user.nombre}"}