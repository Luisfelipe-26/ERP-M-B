from pydantic import BaseModel, EmailStr
from typing import Optional, List
from datetime import datetime, date
from decimal import Decimal


# Auth
class Token(BaseModel):
    access_token: str
    token_type: str
    user: dict

class LoginRequest(BaseModel):
    email: str
    password: str

# Usuario
class UsuarioCreate(BaseModel):
    nombre: str
    email: str
    password: str
    rol: str = "operador"

class UsuarioOut(BaseModel):
    id: int
    nombre: str
    email: str
    rol: str
    activo: bool
    class Config:
        from_attributes = True

# Campo
class CampoCreate(BaseModel):
    id_campo: str
    bloque: Optional[str] = None
    nombre: Optional[str] = None
    area_ha: Optional[float] = None
    n_plantas: Optional[int] = None
    variedad: str = "Hass"
    ano_siembra: Optional[int] = None
    sistema_riego: Optional[str] = None
    etapa: Optional[str] = None
    suelo: Optional[str] = None

class CampoOut(CampoCreate):
    id: int
    activo: bool
    class Config:
        from_attributes = True

# Trabajador
class TrabajadorCreate(BaseModel):
    id_trab: str
    nombre: str
    cargo: Optional[str] = None
    costo_hora: float = 62.50
    observaciones: Optional[str] = None

class TrabajadorOut(TrabajadorCreate):
    id: int
    activo: bool
    class Config:
        from_attributes = True

# Actividad
class ActividadCreate(BaseModel):
    id_act: str
    actividad: str
    tipo: Optional[str] = None
    unidad_rendimiento: Optional[str] = None
    kpi_principal: Optional[str] = None
    tarifa_jornada: Optional[float] = None
    tarifa_ajuste: Optional[float] = None

class ActividadOut(ActividadCreate):
    id: int
    activo: bool
    class Config:
        from_attributes = True

# Producto
class ProductoCreate(BaseModel):
    id_prod: str
    producto: str
    tipo: Optional[str] = None
    unidad: Optional[str] = None
    costo_unitario: Optional[float] = None
    costo_promedio: Optional[float] = None
    stock_actual: float = 0
    stock_minimo: float = 0
    stock_maximo: Optional[float] = None
    proveedor: Optional[str] = None
    proveedor_id: Optional[int] = None
    concentracion: Optional[str] = None
    es_inventariable: bool = True
    cuenta_inventario_id: Optional[int] = None
    cuenta_costo_id: Optional[int] = None
    cuenta_ingreso_id: Optional[int] = None

class ProductoOut(ProductoCreate):
    id: int
    activo: bool
    valor_inventario: Optional[float] = None
    es_inventariable: bool = True
    class Config:
        from_attributes = True

    @classmethod
    def from_orm_with_value(cls, p):
        obj = cls.model_validate(p)
        cp = p.costo_promedio or p.costo_unitario or 0
        obj.valor_inventario = round((p.stock_actual or 0) * cp, 2)
        return obj

# OT Mano Obra
class OTManoObraCreate(BaseModel):
    trabajador_id: str
    fecha: Optional[datetime] = None
    hora_inicio: Optional[str] = None
    hora_fin: Optional[str] = None
    pausa_min: int = 0
    horas_netas: Optional[float] = None
    modalidad: str = "Jornada"
    costo_hora: Optional[float] = None
    costo_mo: Optional[float] = None

class OTManoObraOut(OTManoObraCreate):
    id: int
    ot_id: int
    class Config:
        from_attributes = True

# OT Detalle
class OTDetalleCreate(BaseModel):
    producto_id: str
    fecha: Optional[datetime] = None
    cantidad_usada: Optional[float] = None
    cantidad_tanque: Optional[float] = None
    unidad: Optional[str] = None
    costo_unitario: Optional[float] = None
    costo_real: Optional[float] = None
    observacion: Optional[str] = None

class OTDetalleOut(OTDetalleCreate):
    id: int
    ot_id: int
    class Config:
        from_attributes = True

# Orden Trabajo
class OrdenTrabajoCreate(BaseModel):
    ot_id: Optional[int] = None
    fecha_ejecucion: Optional[datetime] = None
    hora_inicio: Optional[str] = None
    campo_id: str
    actividad_id: str
    supervisor: Optional[str] = None
    estado: str = "Abierta"
    equipo: Optional[str] = None
    horas_equipo: Optional[float] = None
    tarifa_equipo: Optional[float] = None
    observaciones: Optional[str] = None
    mano_obra: Optional[List[OTManoObraCreate]] = []
    detalles: Optional[List[OTDetalleCreate]] = []

class OrdenTrabajoOut(BaseModel):
    id: int
    ot_id: int
    fecha_ejecucion: Optional[datetime]
    hora_inicio: Optional[str]
    campo_id: Optional[str]
    actividad_id: Optional[str]
    supervisor: Optional[str]
    estado: str
    equipo: Optional[str]
    costo_insumos: float
    costo_mano_obra: float
    costo_equipo: Optional[float]
    costo_total: float
    costo_ha: Optional[float]
    horas_mo: Optional[float]
    observaciones: Optional[str]
    hora_cierre: Optional[str]
    creado_en: Optional[datetime]
    class Config:
        from_attributes = True

# Movimiento Inventario — schemas legacy (compatibilidad OT)
class MovimientoCreate(BaseModel):
    producto_id: str
    tipo: str  # entrada, salida
    cantidad: float
    costo_unitario: Optional[float] = None
    referencia: Optional[str] = None
    observacion: Optional[str] = None

class MovimientoOut(BaseModel):
    id: int
    producto_id: str
    tipo_doc: Optional[str] = None
    tipo: Optional[str] = None
    motivo: Optional[str] = None
    cantidad: float
    costo_unitario: Optional[float] = None
    costo_promedio_post: Optional[float] = None
    stock_post: Optional[float] = None
    lote: Optional[str] = None
    vencimiento: Optional[datetime] = None
    proveedor: Optional[str] = None
    num_factura: Optional[str] = None
    referencia: Optional[str] = None
    observacion: Optional[str] = None
    fecha: datetime
    usuario_id: Optional[int] = None
    class Config:
        from_attributes = True

# Inventario SAP B1-style document schemas
class GRCreate(BaseModel):
    """Goods Receipt — Entrada de Mercancía"""
    producto_id: str
    cantidad: float
    precio_compra: float               # precio unitario de compra (actualiza costo promedio)
    proveedor: Optional[str] = None
    num_factura: Optional[str] = None
    lote: Optional[str] = None
    vencimiento: Optional[datetime] = None
    fecha: Optional[datetime] = None
    observacion: Optional[str] = None
    orden_compra_id: Optional[str] = None  # OC-0001

class GICreate(BaseModel):
    """Goods Issue — Salida de Mercancía"""
    producto_id: str
    cantidad: float
    motivo: str  # Merma, Vencimiento, Devolucion, Muestra, Uso No Productivo, Consumo OT, Otro
    referencia: Optional[str] = None
    fecha: Optional[datetime] = None
    observacion: Optional[str] = None
    ot_id: Optional[int] = None  # Orden de trabajo vinculada

class AjusteCreate(BaseModel):
    """Ajuste de Inventario — Recuento Físico"""
    producto_id: str
    cantidad_contada: float            # lo que se contó físicamente
    observacion: Optional[str] = None
    fecha: Optional[datetime] = None

# Dashboard
class DashboardStats(BaseModel):
    total_campos: int
    total_trabajadores: int
    total_productos: int
    ordenes_abiertas: int
    ordenes_mes: int
    costo_mo_mes: float
    costo_insumos_mes: float
    costo_total_mes: float
    productos_bajo_stock: int

# Orden de Compra
class OCLineaCreate(BaseModel):
    producto_id: str
    cantidad: float
    precio_unitario: float

class OCLineaOut(BaseModel):
    id: int
    oc_id: str
    producto_id: str
    cantidad: float
    cantidad_recibida: float
    precio_unitario: float
    subtotal: float
    class Config:
        from_attributes = True

class OrdenCompraCreate(BaseModel):
    fecha: Optional[datetime] = None
    proveedor: Optional[str] = None
    campo_id: Optional[str] = None
    observaciones: Optional[str] = None
    lineas: List[OCLineaCreate] = []

class OrdenCompraOut(BaseModel):
    id: int
    oc_id: str
    fecha: Optional[datetime]
    proveedor: Optional[str]
    campo_id: Optional[str] = None
    estado: str
    total_estimado: float
    total_recibido: Optional[float] = 0
    num_factura: Optional[str] = None
    fecha_recepcion: Optional[datetime] = None
    observaciones: Optional[str]
    creado_en: Optional[datetime]
    class Config:
        from_attributes = True


# ══════════════════════════════════════════════════════════════════════════════
# CONTABILIDAD
# ══════════════════════════════════════════════════════════════════════════════

# Cuenta Contable
class CuentaContableCreate(BaseModel):
    codigo: str
    nombre: str
    tipo: str
    naturaleza: str
    grupo: Optional[str] = None
    nivel: int = 1
    cuenta_padre_id: Optional[int] = None
    acepta_movimientos: bool = True

class CuentaContableOut(CuentaContableCreate):
    id: int
    activo: bool
    class Config:
        from_attributes = True

# Periodo Contable
class PeriodoContableOut(BaseModel):
    id: int
    anio: int
    mes: int
    nombre: Optional[str] = None
    fecha_inicio: date
    fecha_fin: date
    estado: str
    cerrado_por: Optional[str] = None
    cerrado_en: Optional[datetime] = None
    class Config:
        from_attributes = True

# Línea Asiento
class LineaAsientoCreate(BaseModel):
    cuenta_id: int
    debe: float = 0
    haber: float = 0
    campo_id: Optional[str] = None
    tercero_id: Optional[str] = None
    descripcion_linea: Optional[str] = None

class LineaAsientoOut(LineaAsientoCreate):
    id: int
    cuenta_codigo: Optional[str] = None
    cuenta_nombre: Optional[str] = None
    class Config:
        from_attributes = True

# Asiento Contable
class AsientoContableCreate(BaseModel):
    fecha: date
    tipo: str = "manual"
    origen: Optional[str] = "MAN"
    referencia_id: Optional[str] = None
    descripcion: Optional[str] = None
    lineas: List[LineaAsientoCreate] = []

class AsientoContableOut(BaseModel):
    id: int
    numero: str
    fecha: date
    periodo_id: Optional[int] = None
    tipo: str
    origen: Optional[str] = None
    referencia_id: Optional[str] = None
    descripcion: Optional[str] = None
    total_debe: float
    total_haber: float
    estado: str
    creado_por: Optional[str] = None
    contabilizado_por: Optional[str] = None
    contabilizado_en: Optional[datetime] = None
    creado_en: Optional[datetime] = None
    lineas: List[LineaAsientoOut] = []
    class Config:
        from_attributes = True

# Cliente
class ClienteCreate(BaseModel):
    nombre: str
    rnc_cedula: Optional[str] = None
    tipo_rnc: Optional[str] = None
    direccion: Optional[str] = None
    telefono: Optional[str] = None
    email: Optional[str] = None
    contacto: Optional[str] = None
    condicion_pago_dias: int = 30
    tipo_ncf_default: str = "B01"
    cuenta_cxc_id: Optional[int] = None
    notas: Optional[str] = None

class ClienteOut(ClienteCreate):
    id: int
    id_cliente: str
    activo: bool
    class Config:
        from_attributes = True

# Cuenta Bancaria
class CuentaBancariaCreate(BaseModel):
    banco: str
    tipo_cuenta: str = "corriente"
    numero_cuenta: str
    moneda: str = "DOP"
    cuenta_contable_id: Optional[int] = None
    nombre_corto: Optional[str] = None

class CuentaBancariaOut(CuentaBancariaCreate):
    id: int
    saldo_segun_libro: float = 0
    activo: bool
    class Config:
        from_attributes = True

# Configuracion Empresa
class ConfiguracionEmpresaUpdate(BaseModel):
    razon_social: Optional[str] = None
    nombre_comercial: Optional[str] = None
    rnc: Optional[str] = None
    direccion: Optional[str] = None
    telefono: Optional[str] = None
    email: Optional[str] = None
    logo_base64: Optional[str] = None
    moneda_funcional: str = "DOP"
    regimen_fiscal: Optional[str] = None

class ConfiguracionEmpresaOut(ConfiguracionEmpresaUpdate):
    id: int
    class Config:
        from_attributes = True

# CxP
class CuentaPorPagarCreate(BaseModel):
    proveedor_id: int
    oc_id: Optional[str] = None
    tipo_ncf: Optional[str] = None
    ncf: Optional[str] = None
    num_factura_proveedor: Optional[str] = None
    fecha_factura: date
    fecha_vencimiento: Optional[date] = None
    subtotal: float = 0
    itbis: float = 0
    retencion_isr: float = 0
    total: float = 0
    notas: Optional[str] = None

class CuentaPorPagarOut(BaseModel):
    id: int
    numero: str
    proveedor_id: int
    oc_id: Optional[str] = None
    tipo_ncf: Optional[str] = None
    ncf: Optional[str] = None
    num_factura_proveedor: Optional[str] = None
    fecha_factura: date
    fecha_vencimiento: Optional[date] = None
    subtotal: float
    itbis: float
    retencion_isr: float
    total: float
    saldo_pendiente: float
    estado: str
    notas: Optional[str] = None
    creado_en: Optional[datetime] = None
    proveedor_nombre: Optional[str] = None
    class Config:
        from_attributes = True

# Pago
class PagoCreate(BaseModel):
    cxp_id: int
    fecha: date
    monto: float
    metodo_pago: Optional[str] = "transferencia"
    referencia_bancaria: Optional[str] = None
    cuenta_bancaria_id: Optional[int] = None

class PagoOut(BaseModel):
    id: int
    numero: str
    cxp_id: int
    fecha: date
    monto: float
    metodo_pago: Optional[str] = None
    referencia_bancaria: Optional[str] = None
    asiento_id: Optional[int] = None
    creado_en: Optional[datetime] = None
    class Config:
        from_attributes = True

# CxC
class CuentaPorCobrarCreate(BaseModel):
    cliente_id: int
    tipo_ncf: Optional[str] = None
    fecha: date
    fecha_vencimiento: Optional[date] = None
    moneda: str = "DOP"
    tasa_cambio: float = 1.0
    subtotal: float = 0
    itbis: float = 0
    total: float = 0
    campo_id: Optional[str] = None
    temporada: Optional[str] = None
    kg_vendidos: Optional[float] = None
    precio_por_kg: Optional[float] = None

class CuentaPorCobrarOut(BaseModel):
    id: int
    numero: str
    cliente_id: int
    tipo_ncf: Optional[str] = None
    ncf: Optional[str] = None
    fecha: date
    fecha_vencimiento: Optional[date] = None
    moneda: str
    tasa_cambio: float
    subtotal: float
    itbis: float
    total: float
    total_dop: float
    saldo_pendiente: float
    estado: str
    campo_id: Optional[str] = None
    temporada: Optional[str] = None
    kg_vendidos: Optional[float] = None
    precio_por_kg: Optional[float] = None
    creado_en: Optional[datetime] = None
    cliente_nombre: Optional[str] = None
    class Config:
        from_attributes = True

# Cobro
class CobroCreate(BaseModel):
    cxc_id: int
    fecha: date
    monto: float
    metodo_pago: Optional[str] = "transferencia"
    referencia_bancaria: Optional[str] = None
    cuenta_bancaria_id: Optional[int] = None

class CobroOut(BaseModel):
    id: int
    numero: str
    cxc_id: int
    fecha: date
    monto: float
    metodo_pago: Optional[str] = None
    referencia_bancaria: Optional[str] = None
    asiento_id: Optional[int] = None
    creado_en: Optional[datetime] = None
    class Config:
        from_attributes = True

# Regla Contabilización
class ReglaContabilizacionOut(BaseModel):
    id: int
    evento: str
    concepto: str
    cuenta_debe_id: int
    cuenta_haber_id: int
    cuenta_debe_codigo: Optional[str] = None
    cuenta_debe_nombre: Optional[str] = None
    cuenta_haber_codigo: Optional[str] = None
    cuenta_haber_nombre: Optional[str] = None
    descripcion: Optional[str] = None
    activo: bool
    class Config:
        from_attributes = True

# Regla Contabilización Create/Update
class ReglaContabilizacionCreate(BaseModel):
    evento: str
    concepto: str
    cuenta_debe_id: int
    cuenta_haber_id: int
    descripcion: Optional[str] = None

# Activo Fijo
class ActivoFijoCreate(BaseModel):
    codigo: str
    nombre: str
    categoria: Optional[str] = None
    fecha_adquisicion: date
    costo_adquisicion: float = 0
    vida_util_meses: int = 60
    valor_residual: float = 0
    metodo_depreciacion: str = "lineal"
    campo_id: Optional[str] = None
    cuenta_activo_id: Optional[int] = None
    cuenta_depreciacion_id: Optional[int] = None
    cuenta_gasto_dep_id: Optional[int] = None

class ActivoFijoOut(BaseModel):
    id: int
    codigo: str
    nombre: str
    categoria: Optional[str] = None
    fecha_adquisicion: Optional[date] = None
    costo_adquisicion: float
    vida_util_meses: Optional[int] = None
    valor_residual: float
    depreciacion_acumulada: float
    metodo_depreciacion: str
    campo_id: Optional[str] = None
    cuenta_activo_id: Optional[int] = None
    cuenta_depreciacion_id: Optional[int] = None
    cuenta_gasto_dep_id: Optional[int] = None
    activo: bool
    creado_en: Optional[datetime] = None
    valor_en_libros: Optional[float] = None
    dep_mensual_estimada: Optional[float] = None
    campo_nombre: Optional[str] = None
    class Config:
        from_attributes = True

class DepreciacionHistorialOut(BaseModel):
    id: int
    activo_id: int
    periodo_id: int
    monto: float
    depreciacion_acumulada_post: Optional[float] = None
    asiento_id: Optional[int] = None
    periodo_nombre: Optional[str] = None
    class Config:
        from_attributes = True

# Presupuesto
class PresupuestoCreate(BaseModel):
    anio: int
    cuenta_id: int
    campo_id: Optional[str] = None
    monto_ene: float = 0
    monto_feb: float = 0
    monto_mar: float = 0
    monto_abr: float = 0
    monto_may: float = 0
    monto_jun: float = 0
    monto_jul: float = 0
    monto_ago: float = 0
    monto_sep: float = 0
    monto_oct: float = 0
    monto_nov: float = 0
    monto_dic: float = 0
    descripcion: Optional[str] = None

class PresupuestoOut(PresupuestoCreate):
    id: int
    cuenta_codigo: Optional[str] = None
    cuenta_nombre: Optional[str] = None
    campo_nombre: Optional[str] = None
    total_anual: Optional[float] = None
    class Config:
        from_attributes = True
