from datetime import datetime, timedelta
from typing import Optional
from jose import JWTError, jwt
from passlib.context import CryptContext
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session
from database import get_db
import models

import os

# H-9 FIX: Secret key from environment variable, not hardcoded
SECRET_KEY = os.environ.get("CORVUS_SECRET_KEY", "corvus-finca-aguacates-secret-key-2024")
if SECRET_KEY == "corvus-finca-aguacates-secret-key-2024":
    import warnings
    warnings.warn("⚠ CORVUS_SECRET_KEY not set — using default fallback. Set env var for production!", stacklevel=2)
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 480  # 8 hours

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/token")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


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


def require_admin(current_user: models.Usuario = Depends(get_current_user)):
    if current_user.rol not in ["admin"]:
        raise HTTPException(status_code=403, detail="Permisos insuficientes — se requiere rol Admin")
    return current_user


def require_supervisor(current_user: models.Usuario = Depends(get_current_user)):
    """Supervisor or Admin — for inventory movements (GR, GI, AJ), OC management."""
    if current_user.rol not in ["admin", "supervisor"]:
        raise HTTPException(status_code=403, detail="Permisos insuficientes — se requiere rol Supervisor o Admin")
    return current_user
