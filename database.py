import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

# PostgreSQL connection (migrated from SQLite for robustness)
# Format: postgresql://user:password@host:port/database
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:Corvus2024!@127.0.0.1:5432/corvus_finca"
)

# Driver explícito: SQLAlchemy 2.1 cambió el de por defecto para "postgresql://" de
# psycopg2 a psycopg (v3), que no está instalado, y el arranque moría sin conectar.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = "postgresql+psycopg2://" + DATABASE_URL[len("postgresql://"):]

# Fallback to SQLite if PostgreSQL is not available
if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
else:
    engine = create_engine(
        DATABASE_URL,
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,      # auto-reconnect on stale connections
        pool_recycle=300,         # recycle connections every 5 min
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
