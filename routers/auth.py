"""Auth — login, token, user-me endpoints."""
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from database import get_db
import models, schemas, auth

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/token", response_model=schemas.Token)
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(models.Usuario).filter(models.Usuario.email == form_data.username).first()
    if not user or not auth.verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Email o contraseña incorrectos")
    if not user.activo:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Usuario inactivo")
    token = auth.create_access_token({"sub": user.email})
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": auth._build_user_payload(user, db),
    }


@router.post("/login", response_model=schemas.Token)
def login_json(data: schemas.LoginRequest, db: Session = Depends(get_db)):
    user = db.query(models.Usuario).filter(models.Usuario.email == data.email).first()
    if not user or not auth.verify_password(data.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Email o contraseña incorrectos")
    if not user.activo:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Usuario inactivo")
    token = auth.create_access_token({"sub": user.email})
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": auth._build_user_payload(user, db),
    }


@router.get("/me")
def get_me(current_user: models.Usuario = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    return auth._build_user_payload(current_user, db)


@router.post("/usuarios", response_model=schemas.UsuarioOut)
def create_user(data: schemas.UsuarioCreate, db: Session = Depends(get_db),
                current_user: models.Usuario = Depends(auth.require_admin)):
    if db.query(models.Usuario).filter(models.Usuario.email == data.email).first():
        raise HTTPException(status_code=400, detail="Email ya registrado")
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
def recover_admin(data: schemas.LoginRequest, db: Session = Depends(get_db)):
    """Emergency: resets admin credentials when no users can log in.
    Only works when the usuarios table is empty (DB was wiped)."""
    if db.query(models.Usuario).count() > 0:
        raise HTTPException(status_code=403, detail="Operacion no permitida — ya existen usuarios")
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
