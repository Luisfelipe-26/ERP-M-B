from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text, inspect, func
from database import engine, SessionLocal
import models
import auth as auth_module
from routers import (
    auth, campos, trabajadores, actividades, productos,
    contabilidad, sequences, admin,
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
        # Trazabilidad — origen_id en asientos, asiento_id en movimientos y conciliación
        "ALTER TABLE asientos_contables ADD COLUMN IF NOT EXISTS origen_id INTEGER",
        "CREATE INDEX IF NOT EXISTS ix_asientos_contables_origen_id ON asientos_contables(origen_id)",
        "ALTER TABLE movimientos_inventario ADD COLUMN IF NOT EXISTS asiento_id INTEGER REFERENCES asientos_contables(id)",
        "ALTER TABLE conciliacion_partidas ADD COLUMN IF NOT EXISTS asiento_id INTEGER REFERENCES asientos_contables(id)",
        # DiarioContable — diario_id FK en asientos
        "ALTER TABLE asientos_contables ADD COLUMN IF NOT EXISTS diario_id INTEGER REFERENCES diarios_contables(id)",
        "CREATE INDEX IF NOT EXISTS ix_asientos_contables_diario_id ON asientos_contables(diario_id)",
        # PerfilAcceso — perfil_id FK en usuarios
        "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS perfil_id INTEGER REFERENCES perfiles_acceso(id)",
        # Presupuesto — versiones y estado
        "ALTER TABLE presupuestos ADD COLUMN IF NOT EXISTS version VARCHAR(30) DEFAULT 'original'",
        "ALTER TABLE presupuestos ADD COLUMN IF NOT EXISTS estado VARCHAR(20) DEFAULT 'borrador'",
        # Registros presupuestarios (asientos de presupuesto)
        """CREATE TABLE IF NOT EXISTS registros_presupuestarios (
            id SERIAL PRIMARY KEY,
            numero VARCHAR(20) UNIQUE NOT NULL,
            fecha DATE NOT NULL,
            tipo VARCHAR(20) NOT NULL,
            anio INTEGER NOT NULL,
            descripcion VARCHAR(300),
            estado VARCHAR(20) DEFAULT 'borrador',
            usuario_id INTEGER REFERENCES usuarios(id),
            created_at TIMESTAMP DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS lineas_registro_presupuestario (
            id SERIAL PRIMARY KEY,
            registro_id INTEGER NOT NULL REFERENCES registros_presupuestarios(id) ON DELETE CASCADE,
            cuenta_id INTEGER NOT NULL REFERENCES cuentas_contables(id),
            campo_id VARCHAR(10) REFERENCES campos(id_campo),
            unidad_negocio_id INTEGER REFERENCES unidades_negocio(id),
            departamento_id INTEGER REFERENCES departamentos(id),
            monto_ene NUMERIC(14,2) DEFAULT 0,
            monto_feb NUMERIC(14,2) DEFAULT 0,
            monto_mar NUMERIC(14,2) DEFAULT 0,
            monto_abr NUMERIC(14,2) DEFAULT 0,
            monto_may NUMERIC(14,2) DEFAULT 0,
            monto_jun NUMERIC(14,2) DEFAULT 0,
            monto_jul NUMERIC(14,2) DEFAULT 0,
            monto_ago NUMERIC(14,2) DEFAULT 0,
            monto_sep NUMERIC(14,2) DEFAULT 0,
            monto_oct NUMERIC(14,2) DEFAULT 0,
            monto_nov NUMERIC(14,2) DEFAULT 0,
            monto_dic NUMERIC(14,2) DEFAULT 0,
            descripcion VARCHAR(200)
        )""",
        """CREATE TABLE IF NOT EXISTS config_presupuesto (
            id SERIAL PRIMARY KEY,
            umbral_alerta INTEGER DEFAULT 85,
            umbral_bloqueo INTEGER DEFAULT 100,
            control_habilitado BOOLEAN DEFAULT TRUE,
            distribucion_default VARCHAR(20) DEFAULT 'mensual',
            requiere_aprobacion BOOLEAN DEFAULT TRUE
        )""",
        "CREATE INDEX IF NOT EXISTS ix_registros_presup_anio ON registros_presupuestarios(anio)",
        "CREATE INDEX IF NOT EXISTS ix_lineas_reg_presup_registro ON lineas_registro_presupuestario(registro_id)",
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


def seed_perfiles():
    """Ensure default access profiles exist and assign them to existing users."""
    defaults = [
        ("Administrador", "Acceso total al sistema", {
            "contabilidad": "full", "inventario": "full", "ordenes": "full",
            "compras": "full", "nomina": "full", "sanidad": "full", "riego": "full",
            "campos": "full", "trabajadores": "full", "productos": "full",
            "clientes": "full", "proveedores": "full", "activos_fijos": "full",
            "presupuesto": "full", "configuracion": "full", "admin": "full",
        }),
        ("Contador", "Acceso a módulos financieros", {
            "contabilidad": "full", "inventario": "read", "compras": "full",
            "nomina": "full", "clientes": "full", "proveedores": "full",
            "activos_fijos": "full", "presupuesto": "full",
        }),
        ("Supervisor", "Supervisión de operaciones de campo", {
            "ordenes": "full", "inventario": "full", "campos": "full",
            "trabajadores": "full", "productos": "read", "sanidad": "full",
            "riego": "full", "nomina": "read",
        }),
        ("Operador", "Acceso básico operativo", {
            "ordenes": "write", "inventario": "read", "campos": "read",
            "trabajadores": "read", "productos": "read",
        }),
    ]
    db = SessionLocal()
    try:
        for nombre, desc, permisos in defaults:
            exists = db.query(models.PerfilAcceso).filter(
                models.PerfilAcceso.nombre == nombre).first()
            if not exists:
                db.add(models.PerfilAcceso(nombre=nombre, descripcion=desc, permisos=permisos))
        db.commit()
        admin_perfil = db.query(models.PerfilAcceso).filter(
            models.PerfilAcceso.nombre == "Administrador").first()
        if admin_perfil:
            db.query(models.Usuario).filter(
                models.Usuario.rol == "admin",
                models.Usuario.perfil_id == None
            ).update({"perfil_id": admin_perfil.id}, synchronize_session=False)
            sup_perfil = db.query(models.PerfilAcceso).filter(models.PerfilAcceso.nombre == "Supervisor").first()
            if sup_perfil:
                db.query(models.Usuario).filter(
                    models.Usuario.rol == "supervisor",
                    models.Usuario.perfil_id == None
                ).update({"perfil_id": sup_perfil.id}, synchronize_session=False)
            op_perfil = db.query(models.PerfilAcceso).filter(models.PerfilAcceso.nombre == "Operador").first()
            if op_perfil:
                db.query(models.Usuario).filter(
                    models.Usuario.rol == "operador",
                    models.Usuario.perfil_id == None
                ).update({"perfil_id": op_perfil.id}, synchronize_session=False)
            db.commit()
    finally:
        db.close()


def seed_diarios():
    """Ensure default accounting journals exist."""
    defaults = [
        ("COMP", "Diario de Compras", "compras"),
        ("VTA",  "Diario de Ventas", "ventas"),
        ("BAN",  "Diario de Banco/Tesorería", "banco"),
        ("NOM",  "Diario de Nómina", "nomina"),
        ("AJU",  "Diario de Ajustes", "ajuste"),
        ("OPR",  "Diario de Operaciones", "operaciones"),
    ]
    db = SessionLocal()
    try:
        for codigo, nombre, tipo in defaults:
            exists = db.query(models.DiarioContable).filter(
                models.DiarioContable.codigo == codigo).first()
            if not exists:
                db.add(models.DiarioContable(codigo=codigo, nombre=nombre, tipo=tipo))
        db.commit()
    finally:
        db.close()


def seed_admin():
    """Ensure a default admin user exists so the system is always accessible."""
    admin_email = os.environ.get("ADMIN_EMAIL", "admin@corvus.do")
    admin_pass = os.environ.get("ADMIN_PASSWORD")
    if not admin_pass:
        return
    db = SessionLocal()
    try:
        exists = db.query(models.Usuario).filter(models.Usuario.email == admin_email).first()
        if not exists:
            admin_perfil = db.query(models.PerfilAcceso).filter(
                models.PerfilAcceso.nombre == "Administrador").first()
            db.add(models.Usuario(
                nombre="Administrador",
                email=admin_email,
                hashed_password=auth_module.get_password_hash(admin_pass),
                rol="admin",
                perfil_id=admin_perfil.id if admin_perfil else None,
                activo=True,
            ))
            db.commit()
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    models.Base.metadata.create_all(bind=engine)
    run_migrations()
    seed_perfiles()
    seed_diarios()
    seed_admin()
    recalc_all_ot_costs()
    start_sync()
    yield

app = FastAPI(
    title="ERP Finca Aguacates CORVUS",
    version="1.0.0",
    lifespan=lifespan,
)

import os
_cors_origins = os.environ.get("CORS_ORIGINS", "http://localhost:5173,http://localhost:5174").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
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
app.include_router(sequences.router)
app.include_router(admin.router)


@app.get("/")
def root():
    return {"message": "ERP Finca CORVUS API v1.0", "docs": "/docs"}
