from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from database import engine
import models
from routers import (
    auth, campos, trabajadores, actividades, productos,
    ordenes, dashboard, inventario, reportes, compras,
    audit_log, tipos_producto, proveedores,
    clima, sanidad, riego, analytics,
)
from sync_odoo import start_sync

models.Base.metadata.create_all(bind=engine)


@asynccontextmanager
async def lifespan(app):
    # Startup: launch Odoo background sync
    start_sync()
    yield
    # Shutdown: nothing to clean up (daemon threads die with process)


app = FastAPI(
    title="ERP Finca Aguacates CORVUS",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],
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


@app.get("/")
def root():
    return {"message": "ERP Finca CORVUS API v1.0", "docs": "/docs"}
