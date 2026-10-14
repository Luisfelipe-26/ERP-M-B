from datetime import datetime, timedelta
from typing import Optional, Callable
from jose import JWTError, jwt
from passlib.context import CryptContext
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session
from database import get_db
import models

import os

_is_production = bool(os.environ.get("DATABASE_URL"))
SECRET_KEY = os.environ.get("CORVUS_SECRET_KEY", "" if _is_production else "corvus-dev-local-only-key")
if not SECRET_KEY:
    raise RuntimeError("CORVUS_SECRET_KEY env var is required in production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 480  # 8 hours

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/token")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


MIN_CONTRASENA = 10
# Palabras que cualquiera probaría primero: el nombre de la empresa, del sistema o del cultivo.
_PALABRAS_OBVIAS = ("password", "contraseña", "contrasena", "corvus", "vision", "agricola",
                    "aguacate", "admin", "123456", "qwerty", "abc123")


def problemas_contrasena(password: str, email: str = "", nombre: str = "") -> list:
    """Lo que le falta a una contraseña para ser aceptable; lista vacía si cumple."""
    p = password or ""
    bajo = p.lower()
    problemas = []
    if len(p) < MIN_CONTRASENA:
        problemas.append(f"tener al menos {MIN_CONTRASENA} caracteres")
    if len(p.encode("utf-8")) > 72:
        problemas.append("tener como máximo 72 caracteres")           # bcrypt ignora el resto
    if not any(c.isalpha() for c in p) or not any(c.isdigit() for c in p):
        problemas.append("combinar letras y números")
    if p and len(set(p)) < 4:
        problemas.append("no repetir los mismos caracteres")
    personales = [(email or "").split("@")[0].lower()] + [w.lower() for w in (nombre or "").split()]
    if any(len(w) >= 3 and w in bajo for w in personales):
        problemas.append("no incluir el nombre ni el correo del usuario")
    if any(w in bajo for w in _PALABRAS_OBVIAS):
        problemas.append("no incluir palabras obvias (corvus, admin, aguacate, 123456…)")
    return problemas


def exigir_contrasena_segura(password: str, email: str = "", nombre: str = ""):
    problemas = problemas_contrasena(password, email, nombre)
    if problemas:
        raise HTTPException(status_code=400, detail="La contraseña debe " + "; ".join(problemas) + ".")


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def _get_user_permissions(user: models.Usuario) -> set:
    """Collect all permission codes for a user through their roles."""
    perms = set()
    for rol in user.roles:
        if rol.activo:
            for perm in rol.permisos:
                if perm.activo:
                    perms.add(perm.codigo)
    # If user has no roles, fall back to role-string for admin
    if not perms and user.rol == "admin":
        perms.add("*")
    return perms


def _build_user_payload(user: models.Usuario, db: Session) -> dict:
    """Build the user payload dict returned in login responses and JWT."""
    permisos = sorted(_get_user_permissions(user))
    roles_list = [{"id": r.id, "nombre": r.nombre} for r in user.roles if r.activo]
    return {
        "id": user.id,
        "nombre": user.nombre,
        "email": user.email,
        "rol": user.rol,
        "roles": roles_list,
        "permisos": permisos,
    }


def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Credenciales inválidas",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        if email is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    user = db.query(models.Usuario).filter(models.Usuario.email == email).first()
    if user is None or not user.activo:
        raise credentials_exception
    return user


def require_permission(*codigo_list: str) -> Callable:
    """Dependency factory: require one of the given permission codes.

    Usage:
        @router.get("/ordenes")
        def list(..., _=Depends(auth.require_permission("ordenes.read"))):
    """
    def _check(current_user: models.Usuario = Depends(get_current_user)):
        user_perms = _get_user_permissions(current_user)
        if "*" in user_perms:
            return current_user
        if not any(c in user_perms for c in codigo_list):
            raise HTTPException(
                status_code=403,
                detail=f"Permisos insuficientes — se requiere: {', '.join(codigo_list)}",
            )
        return current_user
    return _check


def require_permission_any(module: str) -> Callable:
    """Dependency factory: require ANY permission on a module (module.*).

    Usage:
        @router.get("/ordenes")
        def list(..., _=Depends(auth.require_permission_any("ordenes"))):
    """
    def _check(current_user: models.Usuario = Depends(get_current_user)):
        user_perms = _get_user_permissions(current_user)
        if "*" in user_perms:
            return current_user
        if not any(p.startswith(module + ".") for p in user_perms):
            raise HTTPException(
                status_code=403,
                detail=f"Permisos insuficientes — se requiere acceso al módulo '{module}'",
            )
        return current_user
    return _check


_ACCIONES_METODO = {"POST": ("create", "update"), "PUT": ("update",), "PATCH": ("update",), "DELETE": ("delete",)}
_VERBO = {"create": "crear", "update": "modificar", "delete": "eliminar"}


def permiso_escritura(*modulos: str, por_ruta: Optional[dict] = None) -> Callable:
    """Dependencia de router: crear, modificar o eliminar exige permiso en uno de `modulos`.

    El frontend solo muestra una pantalla si el rol tiene su módulo, pero el API no lo
    revisaba: cualquier usuario logueado podía escribir llamando a la ruta directamente.
    Las lecturas pasan sin cambios. `por_ruta` ({prefijo: módulos}) cubre las pantallas que
    escriben en rutas de otro módulo, como Efectivo y Banco al registrar pagos en
    contabilidad.
    """
    reglas = sorted((por_ruta or {}).items(), key=lambda kv: -len(kv[0]))

    def _check(request: Request, db: Session = Depends(get_db)):
        acciones = _ACCIONES_METODO.get(request.method)
        if not acciones:
            return None
        ruta = getattr(request.scope.get("route"), "path", request.url.path)
        mods = next((m for prefijo, m in reglas if ruta.startswith(prefijo)), modulos)
        encabezado = request.headers.get("authorization", "")
        user = get_current_user(token=encabezado[7:] if encabezado.lower().startswith("bearer ") else "", db=db)
        perms = _get_user_permissions(user)
        if "*" in perms or any(f"{m}.{a}" in perms for m in mods for a in acciones):
            return user
        nombres = " o ".join(m.replace("_", " ").capitalize() for m in mods)
        raise HTTPException(status_code=403, detail=(
            f"Su rol no tiene permiso para {_VERBO[acciones[0]]} en {nombres}. "
            "Un administrador puede asignarlo en Administración → Roles."))

    _check.modulos = modulos
    return _check


# ── Legacy helpers (kept for backward compat, now check RBAC first) ────────

def require_admin(current_user: models.Usuario = Depends(get_current_user)):
    """Require admin role. Checks both legacy rol field and RBAC roles."""
    user_perms = _get_user_permissions(current_user)
    if "*" in user_perms:
        return current_user
    if any(p.startswith("admin.") for p in user_perms):
        return current_user
    if current_user.rol in ["admin"]:
        return current_user
    raise HTTPException(status_code=403, detail="Permisos insuficientes — se requiere rol Admin")


def require_supervisor(current_user: models.Usuario = Depends(get_current_user)):
    """Supervisor or Admin — for inventory movements (GI, AJ), OC management."""
    user_perms = _get_user_permissions(current_user)
    if "*" in user_perms:
        return current_user
    if any(p.startswith("admin.") for p in user_perms):
        return current_user
    if current_user.rol in ["admin", "supervisor"]:
        return current_user
    raise HTTPException(status_code=403, detail="Permisos insuficientes — se requiere rol Supervisor o Admin")


def require_operador(current_user: models.Usuario = Depends(get_current_user)):
    """Operador, Supervisor or Admin — for inventory entries (GR)."""
    user_perms = _get_user_permissions(current_user)
    if "*" in user_perms:
        return current_user
    if any(p.startswith("admin.") for p in user_perms):
        return current_user
    if current_user.rol in ["admin", "supervisor", "operador"]:
        return current_user
    raise HTTPException(status_code=403, detail="Permisos insuficientes — se requiere rol Operador, Supervisor o Admin")
