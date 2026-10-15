"""Auth — login, token, user-me endpoints."""
import threading
import time
from collections import deque

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel
from sqlalchemy.orm import Session
from database import get_db
import audit
import models, schemas, auth

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LimiteIntentos:
    """Intentos fallidos por clave en una ventana deslizante, en la memoria del proceso.

    Cuenta solo los fallos: una oficina que comparte IP puede entrar sin tropezar con el
    límite, pero adivinar contraseñas queda en unos pocos intentos por ventana.
    """

    def __init__(self, maximo: int, ventana_seg: int):
        self.maximo, self.ventana = maximo, ventana_seg
        self._fallos: dict = {}
        self._lock = threading.Lock()

    def _vigentes(self, clave, ahora):
        q = self._fallos.get(clave)
        while q and ahora - q[0] >= self.ventana:
            q.popleft()
        if q is not None and not q:
            del self._fallos[clave]
            return None
        return q

    def espera(self, clave: str) -> int:
        """Segundos hasta que la clave pueda volver a intentar; 0 si puede ya."""
        ahora = time.monotonic()
        with self._lock:
            q = self._vigentes(clave, ahora)
            if not q or len(q) < self.maximo:
                return 0
            return int(self.ventana - (ahora - q[-self.maximo])) + 1

    def fallo(self, clave: str):
        ahora = time.monotonic()
        with self._lock:
            if len(self._fallos) > 10_000:          # que un barrido de IPs no llene la memoria
                for k in list(self._fallos):
                    self._vigentes(k, ahora)
            self._fallos.setdefault(clave, deque()).append(ahora)

    def limpiar(self, clave: str):
        with self._lock:
            self._fallos.pop(clave, None)


limite_ip = LimiteIntentos(maximo=5, ventana_seg=60)           # por conexión
limite_cuenta = LimiteIntentos(maximo=10, ventana_seg=15 * 60)  # por cuenta, venga de donde venga


def _ip_cliente(request: Request) -> str:
    """IP del navegador. Render pasa por Cloudflare, que la pone en CF-Connecting-IP;
    sin ella, la primera de X-Forwarded-For y, en local, la de la conexión."""
    h = request.headers
    ip = h.get("cf-connecting-ip") or h.get("true-client-ip") or (h.get("x-forwarded-for") or "").split(",")[0].strip()
    return ip or (request.client.host if request.client else "desconocida")


def _frenar_si_excede(ip: str, cuenta: str = None):
    espera = limite_ip.espera(ip)
    msg = "Demasiados intentos fallidos desde esta conexión."
    if not espera and cuenta:
        espera = limite_cuenta.espera(cuenta)
        msg = "Demasiados intentos fallidos para esta cuenta."
    if espera:
        cuando = f"{espera} segundos" if espera < 120 else f"{(espera + 59) // 60} minutos"
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                            detail=f"{msg} Espere {cuando} y vuelva a intentarlo.",
                            headers={"Retry-After": str(espera)})


def _autenticar(request: Request, email: str, password: str, db: Session) -> dict:
    ip, cuenta = _ip_cliente(request), (email or "").strip().lower()
    _frenar_si_excede(ip, cuenta)
    user = db.query(models.Usuario).filter(models.Usuario.email == email).first()
    if not user or not auth.verify_password(password, user.hashed_password):
        limite_ip.fallo(ip)
        limite_cuenta.fallo(cuenta)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Email o contraseña incorrectos")
    if not user.activo:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Usuario inactivo")
    limite_ip.limpiar(ip)
    limite_cuenta.limpiar(cuenta)
    token = auth.create_access_token({"sub": user.email})
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": auth._build_user_payload(user, db),
    }


@router.post("/token", response_model=schemas.Token)
def login(request: Request, form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    return _autenticar(request, form_data.username, form_data.password, db)


@router.post("/login", response_model=schemas.Token)
def login_json(request: Request, data: schemas.LoginRequest, db: Session = Depends(get_db)):
    return _autenticar(request, data.email, data.password, db)


class CambioContrasena(BaseModel):
    actual: str
    nueva: str


@router.post("/cambiar-contrasena")
def cambiar_contrasena(data: CambioContrasena, request: Request, db: Session = Depends(get_db),
                       user: models.Usuario = Depends(auth.get_current_user)):
    """Cada usuario cambia su propia contraseña: pide la actual y aplica la política.

    La clave actual equivocada responde 400, no 401: el frontend cierra la sesión ante un 401.
    Los intentos fallidos cuentan como en el login, para que una sesión abierta y olvidada
    no sirva para adivinar la contraseña.
    """
    ip, cuenta = _ip_cliente(request), user.email.lower()
    _frenar_si_excede(ip, cuenta)
    if not auth.verify_password(data.actual, user.hashed_password):
        limite_ip.fallo(ip)
        limite_cuenta.fallo(cuenta)
        raise HTTPException(status_code=400, detail="La contraseña actual no es correcta")
    if data.nueva == data.actual:
        raise HTTPException(status_code=400, detail="La contraseña nueva debe ser distinta de la actual")
    auth.exigir_contrasena_segura(data.nueva, user.email, user.nombre)
    user.hashed_password = auth.get_password_hash(data.nueva)
    auth.marcar_cambio_de_clave(user)
    audit.log(db, user, "CAMBIAR_CONTRASENA", "USUARIO", user.email,
              f"{user.nombre} cambió su contraseña; se cerraron sus otras sesiones")
    db.commit()
    # Las demás sesiones se cierran; esta sigue con un token nuevo.
    return {"ok": True, "msg": "Contraseña actualizada", "access_token": auth.create_access_token({"sub": user.email})}


@router.get("/me")
def get_me(current_user: models.Usuario = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    return auth._build_user_payload(current_user, db)


@router.post("/usuarios", response_model=schemas.UsuarioOut)
def create_user(data: schemas.UsuarioCreate, db: Session = Depends(get_db),
                current_user: models.Usuario = Depends(auth.require_admin)):
    if db.query(models.Usuario).filter(models.Usuario.email == data.email).first():
        raise HTTPException(status_code=400, detail="Email ya registrado")
    auth.exigir_contrasena_segura(data.password, data.email, data.nombre)
    user = models.Usuario(
        nombre=data.nombre,
        email=data.email,
        hashed_password=auth.get_password_hash(data.password),
        rol=data.rol,
        perfil_id=data.perfil_id,
    )
    db.add(user)
    db.flush()
    # Assign roles
    if data.rol_ids:
        roles = db.query(models.Rol).filter(models.Rol.id.in_(data.rol_ids)).all()
        user.roles = roles
    db.commit()
    db.refresh(user)
    return user


@router.post("/recover")
def recover_admin(request: Request, data: schemas.LoginRequest, db: Session = Depends(get_db)):
    """Emergency: resets admin credentials when no users can log in.
    Only works when the usuarios table is empty (DB was wiped)."""
    ip = _ip_cliente(request)
    _frenar_si_excede(ip)
    if db.query(models.Usuario).count() > 0:
        limite_ip.fallo(ip)
        raise HTTPException(status_code=403, detail="Operacion no permitida — ya existen usuarios")
    auth.exigir_contrasena_segura(data.password, data.email)
    perfil = db.query(models.PerfilAcceso).filter(
        models.PerfilAcceso.nombre == "Administrador").first()
    user = models.Usuario(
        nombre="Administrador",
        email=data.email,
        hashed_password=auth.get_password_hash(data.password),
        rol="admin",
        perfil_id=perfil.id if perfil else None,
    )
    db.add(user)
    db.flush()
    # Assign admin role if exists
    admin_rol = db.query(models.Rol).filter(models.Rol.nombre == "admin").first()
    if admin_rol:
        user.roles.append(admin_rol)
    db.commit()
    db.refresh(user)
    token = auth.create_access_token({"sub": user.email})
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": auth._build_user_payload(user, db),
    }


@router.get("/usuarios", response_model=list[schemas.UsuarioOut])
def list_users(db: Session = Depends(get_db), current_user: models.Usuario = Depends(auth.require_admin)):
    return db.query(models.Usuario).all()
