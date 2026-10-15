import calendar
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
    ahora = datetime.utcnow()
    expire = ahora + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    # iat: cuándo se abrió la sesión, para cerrarla si después cambia la contraseña.
    to_encode.update({"exp": expire, "iat": calendar.timegm(ahora.utctimetuple())})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def marcar_cambio_de_clave(user: models.Usuario):
    """Las sesiones abiertas antes de este momento dejan de valer (ver get_current_user)."""
    user.clave_cambiada_en = datetime.utcnow()


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
    # Una sesión abierta antes del último cambio de contraseña ya no vale: si alguien la tenía
    # (otro equipo, una sesión robada), cambiar la clave la cierra. Las sesiones sin fecha son
    # de antes de este control y solo caducan si la clave cambió después.
    if user.clave_cambiada_en is not None:
        emitido = payload.get("iat")
        if not isinstance(emitido, (int, float)) or emitido < calendar.timegm(user.clave_cambiada_en.utctimetuple()):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Su sesión se cerró porque la contraseña cambió. Inicie sesión de nuevo.",
                headers={"WWW-Authenticate": "Bearer"})
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


# Rutas GET que se usan sin sesión (la plantilla de carga masiva de órdenes).
RUTAS_PUBLICAS = {"/api/ordenes/plantilla/descargar"}


def permiso_modulo(*modulos: str, por_ruta: Optional[dict] = None) -> Callable:
    """Dependencia de router: aplica en el API los permisos por módulo de los roles.

    El frontend solo muestra una pantalla si el rol tiene su módulo, pero el API no lo
    revisaba: cualquier usuario logueado podía leer y escribir de todo llamando a las rutas.
    - Escribir: POST exige create o update, PUT/PATCH update, DELETE delete, en uno de
      `modulos` (o los de `por_ruta`, {prefijo: módulos}, para las pantallas que escriben
      en rutas de otro módulo, como Efectivo y Banco al registrar pagos en contabilidad).
    - Leer: cualquier permiso en uno de esos módulos o en los de las pantallas que leen la
      ruta (permisos_lectura.LECTURAS), igual que el frontend decide si muestra la pantalla.
    """
    from permisos_lectura import LECTURAS
    reglas = sorted((por_ruta or {}).items(), key=lambda kv: -len(kv[0]))

    def _check(request: Request, db: Session = Depends(get_db)):
        ruta = getattr(request.scope.get("route"), "path", request.url.path)
        leer = request.method in ("GET", "HEAD")
        acciones = _ACCIONES_METODO.get(request.method)
        if not leer and not acciones:
            return None
        lectores = LECTURAS.get(ruta, ()) if leer else ()
        if leer and (ruta in RUTAS_PUBLICAS or lectores == "*"):
            return None
        mods = next((m for prefijo, m in reglas if ruta.startswith(prefijo)), modulos)
        mods = tuple(dict.fromkeys(mods + tuple(lectores)))
        encabezado = request.headers.get("authorization", "")
        user = get_current_user(token=encabezado[7:] if encabezado.lower().startswith("bearer ") else "", db=db)
        perms = _get_user_permissions(user)
        if "*" in perms:
            return user
        if leer and any(p.split(".")[0] in mods for p in perms):
            return user
        if not leer and any(f"{m}.{a}" in perms for m in mods for a in acciones):
            return user
        nombres = " o ".join(m.replace("_", " ").capitalize() for m in mods[:3]) + (" u otro módulo" if len(mods) > 3 else "")
        verbo = "ver" if leer else _VERBO[acciones[0]]
        raise HTTPException(status_code=403, detail=(
            f"Su rol no tiene permiso para {verbo} en {nombres}. "
            "Un administrador puede asignarlo en Administración → Roles."))

    _check.modulos = modulos
    return _check


# ── Niveles de rol (admin, supervisor, contador) ───────────────────────────
# Para lo que es jerarquía y no permiso de módulo: anular, cerrar períodos, autorizar un
# sobregiro. Antes se miraba el texto `usuario.rol`, que Administración copia del primer rol
# asignado por orden alfabético: alguien con "contador" y "supervisor" quedaba como
# "contador" y no podía hacer lo de supervisor. Ahora cuentan todos los roles asignados.

def roles_de(user: models.Usuario) -> set:
    """Nombres de los roles asignados; el texto `rol` solo si no tiene ninguno (usuarios viejos)."""
    nombres = {r.nombre.strip().lower() for r in user.roles if r.activo}
    return nombres or {(user.rol or "").strip().lower()}


def tiene_rol(user: models.Usuario, *nombres: str) -> bool:
    """True si el usuario tiene alguno de esos roles, o es administrador."""
    perms = _get_user_permissions(user)
    if "*" in perms or any(p.startswith("admin.") for p in perms):
        return True
    return bool(roles_de(user) & {n.lower() for n in nombres})


def require_admin(current_user: models.Usuario = Depends(get_current_user)):
    if tiene_rol(current_user, "admin"):
        return current_user
    raise HTTPException(status_code=403, detail="Permisos insuficientes — se requiere rol Admin")


def require_supervisor(current_user: models.Usuario = Depends(get_current_user)):
    """Supervisor o admin: anular, ajustar inventario, aprobar compras y otras acciones de mando."""
    if tiene_rol(current_user, "admin", "supervisor"):
        return current_user
    raise HTTPException(status_code=403, detail="Permisos insuficientes — se requiere rol Supervisor o Admin")


def require_operador(current_user: models.Usuario = Depends(get_current_user)):
    """Cualquier usuario con sesión. Lo que puede registrar lo deciden los permisos del módulo
    (main.PERMISOS_MODULO); antes también exigía el texto de rol admin, supervisor u
    operador, y un rol propio (p. ej. "Jefe de campo") no podía registrar cosechas."""
    return current_user
