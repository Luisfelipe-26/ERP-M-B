from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text, inspect
from database import engine
import models
from routers import (
    auth, campos, trabajadores, actividades, productos,
    contabilidad,
    ordenes, dashboard, inventario, reportes, compras,
    audit_log, tipos_producto, proveedores,
    clima, sanidad, riego, analytics,
    clientes, cuentas_bancarias,
)
from sync_odoo import start_sync


def run_migrations():
    """Add missing columns to existing tables without dropping data."""
    migrations = [
        # Producto — new GL account columns
        "ALTER TABLE productos ADD COLUMN IF NOT EXISTS proveedor_id INTEGER REFERENCES proveedores(id)",
        "ALTER TABLE productos ADD COLUMN IF NOT EXISTS cuenta_inventario_id INTEGER REFERENCES cuentas_contables(id)",
        "ALTER TABLE productos ADD COLUMN IF NOT EXISTS cuenta_costo_id INTEGER REFERENCES cuentas_contables(id)",
        "ALTER TABLE productos ADD COLUMN IF NOT EXISTS cuenta_ingreso_id INTEGER REFERENCES cuentas_contables(id)",
        # Proveedor — new accounting columns
        "ALTER TABLE proveedores ADD COLUMN IF NOT EXISTS direccion TEXT",
        "ALTER TABLE proveedores ADD COLUMN IF NOT EXISTS condicion_pago_dias INTEGER DEFAULT 30",
        "ALTER TABLE proveedores ADD COLUMN IF NOT EXISTS tipo_ncf_default VARCHAR(5) DEFAULT 'B11'",
        "ALTER TABLE proveedores ADD COLUMN IF NOT EXISTS cuenta_cxp_id INTEGER REFERENCES cuentas_contables(id)",
    ]
    with engine.begin() as conn:
        for sql in migrations:
            try:
                conn.execute(text(sql))
            except Exception:
                pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    models.Base.metadata.create_all(bind=engine)
    run_migrations()
    start_sync()
    yield

app = FastAPI(
    title="ERP Finca Aguacates CORVUS",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register all routers
app.include_router(auth.router)
app.include_router(campos.router)
app.include_router(trabajadores.router)
app.include_router(actividades.router)
app.include_router(productos.router)
app.include_router(ordenes.router)
app.include_router(dashboard.router)
app.include_router(inventario.router)
app.include_router(reportes.router)
app.include_router(compras.router)
app.include_router(audit_log.router)
app.include_router(tipos_producto.router)
app.include_router(proveedores.router)
app.include_router(clima.router)
app.include_router(sanidad.router)
app.include_router(riego.router)
app.include_router(analytics.router)
app.include_router(contabilidad.router)
app.include_router(clientes.router)
app.include_router(cuentas_bancarias.router)


@app.get("/")
def root():
    return {"message": "ERP Finca CORVUS API v1.0", "docs": "/docs"}
