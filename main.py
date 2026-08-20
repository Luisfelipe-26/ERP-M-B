from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text, inspect, func
from database import engine, SessionLocal
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
        # PartidaEstadoFinanciero — SAP FSV hierarchy columns
        "ALTER TABLE partidas_estado_financiero ADD COLUMN IF NOT EXISTS padre_id INTEGER REFERENCES partidas_estado_financiero(id)",
        "ALTER TABLE partidas_estado_financiero ADD COLUMN IF NOT EXISTS orden INTEGER DEFAULT 0",
        "ALTER TABLE partidas_estado_financiero ADD COLUMN IF NOT EXISTS invertir_signo BOOLEAN DEFAULT FALSE",
        "ALTER TABLE partidas_estado_financiero ADD COLUMN IF NOT EXISTS es_grupo BOOLEAN DEFAULT FALSE",
        "ALTER TABLE partidas_estado_financiero ADD COLUMN IF NOT EXISTS activo BOOLEAN DEFAULT TRUE",
        # CuentaContable — link to financial-statement line item
        "ALTER TABLE cuentas_contables ADD COLUMN IF NOT EXISTS partida_id INTEGER REFERENCES partidas_estado_financiero(id)",
        # Dimensiones financieras — FKs en tablas existentes
        "ALTER TABLE lineas_asiento ADD COLUMN IF NOT EXISTS unidad_negocio_id INTEGER REFERENCES unidades_negocio(id)",
        "ALTER TABLE lineas_asiento ADD COLUMN IF NOT EXISTS departamento_id INTEGER REFERENCES departamentos(id)",
        "ALTER TABLE lineas_asiento ADD COLUMN IF NOT EXISTS almacen_id INTEGER REFERENCES almacenes(id)",
        "ALTER TABLE presupuestos ADD COLUMN IF NOT EXISTS unidad_negocio_id INTEGER REFERENCES unidades_negocio(id)",
        "ALTER TABLE presupuestos ADD COLUMN IF NOT EXISTS departamento_id INTEGER REFERENCES departamentos(id)",
        "ALTER TABLE presupuestos ADD COLUMN IF NOT EXISTS almacen_id INTEGER REFERENCES almacenes(id)",
        "ALTER TABLE ordenes_trabajo ADD COLUMN IF NOT EXISTS unidad_negocio_id INTEGER REFERENCES unidades_negocio(id)",
        "ALTER TABLE ordenes_trabajo ADD COLUMN IF NOT EXISTS departamento_id INTEGER REFERENCES departamentos(id)",
        "ALTER TABLE ordenes_compra ADD COLUMN IF NOT EXISTS unidad_negocio_id INTEGER REFERENCES unidades_negocio(id)",
        "ALTER TABLE ordenes_compra ADD COLUMN IF NOT EXISTS departamento_id INTEGER REFERENCES departamentos(id)",
        "ALTER TABLE ordenes_compra ADD COLUMN IF NOT EXISTS almacen_id INTEGER REFERENCES almacenes(id)",
        "ALTER TABLE movimientos_inventario ADD COLUMN IF NOT EXISTS almacen_id INTEGER REFERENCES almacenes(id)",
        "ALTER TABLE nomina_detalle ADD COLUMN IF NOT EXISTS departamento_id INTEGER REFERENCES departamentos(id)",
    ]
    # Each migration runs in its own transaction so one failure does not
    # abort the rest (PostgreSQL poisons the whole tx on any error).
    for sql in migrations:
        try:
            with engine.begin() as conn:
                conn.execute(text(sql))
        except Exception:
            pass


def recalc_all_ot_costs():
    """One-time sync of denormalized costo_mano_obra on every OT."""
    db = SessionLocal()
    try:
        for ot in db.query(models.OrdenTrabajo).all():
            mo = db.query(func.coalesce(func.sum(models.OTManoObra.costo_mo), 0)).filter(
                models.OTManoObra.ot_id == ot.ot_id).scalar() or 0
            new_mo = round(float(mo), 2)
            if round(float(ot.costo_mano_obra or 0), 2) != new_mo:
                ot.costo_mano_obra = new_mo
                ot.costo_total = round(new_mo + float(ot.costo_insumos or 0) + float(ot.costo_equipo or 0), 2)
        db.commit()
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    models.Base.metadata.create_all(bind=engine)
    run_migrations()
    recalc_all_ot_costs()
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
