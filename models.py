from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, ForeignKey, Text, Index, Date, Numeric, UniqueConstraint, JSON
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from database import Base


class PerfilAcceso(Base):
    __tablename__ = "perfiles_acceso"
    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String(50), unique=True, nullable=False)
    descripcion = Column(String(200))
    permisos = Column(JSON, default=dict)
    activo = Column(Boolean, default=True)


class Usuario(Base):
    __tablename__ = "usuarios"
    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String(100), nullable=False)
    email = Column(String(100), unique=True, index=True, nullable=False)
    hashed_password = Column(String(200), nullable=False)
    rol = Column(String(50), default="operador")
    perfil_id = Column(Integer, ForeignKey("perfiles_acceso.id"))
    activo = Column(Boolean, default=True)
    creado_en = Column(DateTime, server_default=func.now())
    perfil = relationship("PerfilAcceso")


class Campo(Base):
    __tablename__ = "campos"
    id = Column(Integer, primary_key=True, index=True)
    id_campo = Column(String(10), unique=True, index=True, nullable=False)
    bloque = Column(String(5))
    nombre = Column(String(100))
    area_ha = Column(Float)
    n_plantas = Column(Integer)
    variedad = Column(String(100), default="Hass")
    ano_siembra = Column(Integer)
    sistema_riego = Column(String(50))
    etapa = Column(String(10))
    suelo = Column(String(100))
    activo = Column(Boolean, default=True)
    ordenes = relationship("OrdenTrabajo", back_populates="campo")


class Trabajador(Base):
    __tablename__ = "trabajadores"
    id = Column(Integer, primary_key=True, index=True)
    id_trab = Column(String(10), unique=True, index=True, nullable=False)
    nombre = Column(String(100), nullable=False)
    cargo = Column(String(100))
    costo_hora = Column(Float, default=62.50)
    activo = Column(Boolean, default=True)
    observaciones = Column(Text)
    mano_obra = relationship("OTManoObra", back_populates="trabajador")


class Actividad(Base):
    __tablename__ = "actividades"
    id = Column(Integer, primary_key=True, index=True)
    id_act = Column(String(10), unique=True, index=True, nullable=False)
    actividad = Column(String(200), nullable=False)
    tipo = Column(String(50))
    unidad_rendimiento = Column(String(50))
    kpi_principal = Column(String(200))
    tarifa_jornada = Column(Float)
    tarifa_ajuste = Column(Float)
    activo = Column(Boolean, default=True)


class TipoProducto(Base):
    __tablename__ = "tipos_producto"
    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String(100), unique=True, nullable=False)
    descripcion = Column(String(200))
    activo = Column(Boolean, default=True)


class Proveedor(Base):
    __tablename__ = "proveedores"
    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String(200), unique=True, nullable=False)
    rnc = Column(String(20))
    email = Column(String(200))
    telefono = Column(String(50))
    contacto = Column(String(200))
    direccion = Column(Text)
    odoo_id = Column(Integer)
    condicion_pago_dias = Column(Integer, default=30)
    tipo_ncf_default = Column(String(5), default="B11")
    cuenta_cxp_id = Column(Integer, ForeignKey("cuentas_contables.id"))
    activo = Column(Boolean, default=True)
    cuenta_cxp = relationship("CuentaContable", foreign_keys=[cuenta_cxp_id])
    cuentas_por_pagar = relationship("CuentaPorPagar", back_populates="proveedor")


class Producto(Base):
    __tablename__ = "productos"
    id = Column(Integer, primary_key=True, index=True)
    id_prod = Column(String(10), unique=True, index=True, nullable=False)
    producto = Column(String(200), nullable=False)
    tipo = Column(String(100))
    unidad = Column(String(20))
    costo_unitario = Column(Float)          # precio de referencia / lista
    costo_promedio = Column(Float)          # costo promedio ponderado actual
    stock_actual = Column(Float, default=0)
    stock_minimo = Column(Float, default=0)
    stock_maximo = Column(Float)
    proveedor = Column(String(200))
    proveedor_id = Column(Integer, ForeignKey("proveedores.id"))
    concentracion = Column(String(100))
    es_inventariable = Column(Boolean, default=True)
    cuenta_inventario_id = Column(Integer, ForeignKey("cuentas_contables.id"))
    cuenta_costo_id = Column(Integer, ForeignKey("cuentas_contables.id"))
    cuenta_ingreso_id = Column(Integer, ForeignKey("cuentas_contables.id"))
    activo = Column(Boolean, default=True)
    detalles_ot = relationship("OTDetalle", back_populates="producto")
    movimientos = relationship("MovimientoInventario", back_populates="producto")
    proveedor_rel = relationship("Proveedor", foreign_keys=[proveedor_id])
    cuenta_inventario = relationship("CuentaContable", foreign_keys=[cuenta_inventario_id])
    cuenta_costo = relationship("CuentaContable", foreign_keys=[cuenta_costo_id])
    cuenta_ingreso = relationship("CuentaContable", foreign_keys=[cuenta_ingreso_id])


ESTADOS_OT = ["Abierta", "En Proceso", "En Pausa", "Cerrada"]


class OrdenTrabajo(Base):
    __tablename__ = "ordenes_trabajo"
    id = Column(Integer, primary_key=True, index=True)
    ot_id = Column(Integer, unique=True, index=True, nullable=False)
    fecha_ejecucion = Column(DateTime)
    hora_inicio = Column(String(10))  # HH:MM
    campo_id = Column(String(10), ForeignKey("campos.id_campo"), index=True)
    actividad_id = Column(String(10), ForeignKey("actividades.id_act"), index=True)
    supervisor = Column(String(100))
    estado = Column(String(50), default="Abierta")
    equipo = Column(String(200))
    horas_equipo = Column(Float)
    costo_equipo = Column(Float, default=0)       # costo calculado del equipo
    tarifa_equipo = Column(Float)                  # RD$/hora del equipo usado
    costo_insumos = Column(Float, default=0)
    costo_mano_obra = Column(Float, default=0)
    costo_total = Column(Float, default=0)         # MO + Insumos + Equipo
    costo_ha = Column(Float)
    horas_mo = Column(Float)
    observaciones = Column(Text)
    fecha_cierre = Column(DateTime)
    hora_cierre = Column(String(10))  # HH:MM
    creado_en = Column(DateTime, server_default=func.now())
    unidad_negocio_id = Column(Integer, ForeignKey("unidades_negocio.id"), index=True)
    departamento_id = Column(Integer, ForeignKey("departamentos.id"), index=True)
    campo = relationship("Campo", back_populates="ordenes")
    actividad_rel = relationship("Actividad")
    mano_obra = relationship("OTManoObra", back_populates="orden")
    detalles = relationship("OTDetalle", back_populates="orden")


class OTManoObra(Base):
    __tablename__ = "ot_mano_obra"
    id = Column(Integer, primary_key=True, index=True)
    ot_id = Column(Integer, ForeignKey("ordenes_trabajo.ot_id"), index=True)
    trabajador_id = Column(String(10), ForeignKey("trabajadores.id_trab"), index=True)
    fecha = Column(DateTime)
    hora_inicio = Column(String(10))
    hora_fin = Column(String(10))
    pausa_min = Column(Integer, default=0)
    horas_netas = Column(Float)
    modalidad = Column(String(50), default="Jornada")
    costo_hora = Column(Float)
    costo_mo = Column(Float)
    orden = relationship("OrdenTrabajo", back_populates="mano_obra")
    trabajador = relationship("Trabajador", back_populates="mano_obra")


class OTDetalle(Base):
    __tablename__ = "ot_detalle"
    id = Column(Integer, primary_key=True, index=True)
    ot_id = Column(Integer, ForeignKey("ordenes_trabajo.ot_id"), index=True)
    producto_id = Column(String(10), ForeignKey("productos.id_prod"), index=True)
    fecha = Column(DateTime)
    cantidad_usada = Column(Float)
    cantidad_tanque = Column(Float)
    unidad = Column(String(20))
    costo_unitario = Column(Float)
    costo_real = Column(Float)
    observacion = Column(Text)
    orden = relationship("OrdenTrabajo", back_populates="detalles")
    producto = relationship("Producto", back_populates="detalles_ot")


class Sequence(Base):
    __tablename__ = "sequences"
    tipo = Column(String(20), primary_key=True)
    ultimo_numero = Column(Integer, default=0, nullable=False)


class OrdenCompra(Base):
    __tablename__ = "ordenes_compra"
    id = Column(Integer, primary_key=True, index=True)
    oc_id = Column(String(20), unique=True, index=True, nullable=False)  # OC-0001
    fecha = Column(DateTime, server_default=func.now())
    proveedor = Column(String(200))
    campo_id = Column(String(10), ForeignKey("campos.id_campo"))
    estado = Column(String(50), default="Pendiente")  # Pendiente, Parcial, Recibida, Cancelada
    total_estimado = Column(Float, default=0)
    total_recibido = Column(Float, default=0)
    num_factura = Column(String(100))
    fecha_recepcion = Column(DateTime)
    observaciones = Column(Text)
    creado_en = Column(DateTime, server_default=func.now())
    unidad_negocio_id = Column(Integer, ForeignKey("unidades_negocio.id"), index=True)
    departamento_id = Column(Integer, ForeignKey("departamentos.id"), index=True)
    almacen_id = Column(Integer, ForeignKey("almacenes.id"), index=True)
    lineas = relationship("OrdenCompraLinea", back_populates="orden")
    campo = relationship("Campo")


class OrdenCompraLinea(Base):
    __tablename__ = "ordenes_compra_lineas"
    id = Column(Integer, primary_key=True, index=True)
    oc_id = Column(String(20), ForeignKey("ordenes_compra.oc_id"), index=True)
    producto_id = Column(String(10), ForeignKey("productos.id_prod"), index=True)
    cantidad = Column(Float)
    cantidad_recibida = Column(Float, default=0)
    precio_unitario = Column(Float)
    subtotal = Column(Float)
    orden = relationship("OrdenCompra", back_populates="lineas")
    producto = relationship("Producto")


class MovimientoInventario(Base):
    __tablename__ = "movimientos_inventario"
    id = Column(Integer, primary_key=True, index=True)
    num_documento = Column(String(20), index=True)   # GR-0001, GI-0001, AJ-0001
    producto_id = Column(String(10), ForeignKey("productos.id_prod"), index=True)
    # tipo_doc: GR=Entrada Mercancía, GI=Salida Mercancía, AJ=Ajuste/Recuento, OT=Consumo OT
    tipo_doc = Column(String(5), default="GR")
    # tipo legacy: "entrada" / "salida" (calculado del tipo_doc)
    tipo = Column(String(20))
    motivo = Column(String(100))            # Compra, Merma, Vencimiento, Ajuste Conteo, Consumo OT…
    cantidad = Column(Float)
    costo_unitario = Column(Float)          # precio de la transacción
    costo_promedio_post = Column(Float)     # costo promedio después de este movimiento
    stock_post = Column(Float)              # stock saldo después de este movimiento (kardex)
    lote = Column(String(100))
    vencimiento = Column(DateTime)
    proveedor = Column(String(200))
    num_factura = Column(String(100))
    referencia = Column(String(100))
    ot_referencia = Column(Integer, index=True)           # OT vinculada (para GI consumo)
    oc_referencia = Column(String(20), index=True)        # OC vinculada (para GR compra)
    observacion = Column(Text)
    fecha = Column(DateTime, server_default=func.now())
    usuario_id = Column(Integer, ForeignKey("usuarios.id"), index=True)
    almacen_id = Column(Integer, ForeignKey("almacenes.id"), index=True)
    asiento_id = Column(Integer, ForeignKey("asientos_contables.id"))
    producto = relationship("Producto", back_populates="movimientos")
    usuario = relationship("Usuario")


class AuditLog(Base):
    """Registro de auditoría para cambios críticos."""
    __tablename__ = "audit_log"
    id = Column(Integer, primary_key=True, index=True)
    fecha = Column(DateTime, server_default=func.now(), index=True)
    usuario_id = Column(Integer, ForeignKey("usuarios.id"), index=True)
    usuario_nombre = Column(String(100))
    accion = Column(String(50), index=True)       # CREAR, MODIFICAR, ELIMINAR, ESTADO, AJUSTE
    entidad = Column(String(50), index=True)       # OT, GR, GI, AJ, OC, ARTICULO, CAMPO...
    entidad_id = Column(String(50))                # OT-5, GR-0031, P-014...
    detalle = Column(Text)                         # descripción legible del cambio
    datos_json = Column(Text)                      # snapshot JSON (antes/después) para auditoría profunda


# ══════════════════════════════════════════════════════════════════════════════
# MÓDULO CLIMA — Monitoreo ambiental y alertas meteorológicas
# ══════════════════════════════════════════════════════════════════════════════

class WeatherReading(Base):
    """Lecturas meteorológicas — de API (Open-Meteo) o estación física."""
    __tablename__ = "weather_readings"
    id = Column(Integer, primary_key=True, index=True)
    recorded_at = Column(DateTime, nullable=False, index=True)
    source = Column(String(30), default="api")  # api, station, manual
    station_code = Column(String(20))            # WS-001 o 'open_meteo'
    campo_id = Column(String(10), ForeignKey("campos.id_campo"), index=True)
    # Core measurements
    temperature_c = Column(Float)
    humidity_pct = Column(Float)
    rainfall_mm = Column(Float)
    wind_speed_kmh = Column(Float)
    wind_direction_deg = Column(Integer)
    wind_gust_kmh = Column(Float)
    solar_radiation_wm2 = Column(Float)
    uv_index = Column(Float)
    dew_point_c = Column(Float)
    pressure_hpa = Column(Float)
    leaf_wetness_min = Column(Integer)
    # Calculated
    et0_mm = Column(Float)                       # Evapotranspiración referencia
    vpd_kpa = Column(Float)                      # Vapor Pressure Deficit
    quality_flag = Column(String(15), default="OK")  # OK, SUSPECT, MISSING


class WeatherDailySummary(Base):
    """Resumen diario pre-agregado — calculado nocturnamente o al consultar."""
    __tablename__ = "weather_daily_summary"
    id = Column(Integer, primary_key=True, index=True)
    date = Column(DateTime, nullable=False, index=True)
    station_code = Column(String(20), default="open_meteo")
    # Temperature
    temp_min = Column(Float)
    temp_max = Column(Float)
    temp_avg = Column(Float)
    # Humidity
    humidity_min = Column(Float)
    humidity_max = Column(Float)
    humidity_avg = Column(Float)
    # Rain & Wind
    rainfall_total_mm = Column(Float)
    wind_avg_kmh = Column(Float)
    wind_max_gust_kmh = Column(Float)
    # Solar
    solar_radiation_mjm2 = Column(Float)
    uv_max = Column(Float)
    # Agronomic metrics
    et0_mm = Column(Float)
    gdd_base10 = Column(Float)                   # Growing Degree Days
    gdd_cumulative = Column(Float)
    chill_hours = Column(Float)                  # Horas 0-7.2°C
    frost_hours = Column(Float)                  # Horas <2°C
    heat_stress_hours = Column(Float)            # Horas >35°C
    vpd_avg = Column(Float)
    leaf_wetness_hours = Column(Float)
    # Disease risk (0-10)
    anthracnose_risk = Column(Float)
    phytophthora_risk = Column(Float)


class WeatherAlertRule(Base):
    """Reglas de alerta configurables para clima."""
    __tablename__ = "weather_alert_rules"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    variable = Column(String(50), nullable=False)    # temperature_c, humidity_pct, etc.
    condition = Column(String(10), nullable=False)   # lt, gt, lte, gte
    threshold_value = Column(Float, nullable=False)
    threshold_value_2 = Column(Float)                # for 'between'
    duration_minutes = Column(Integer, default=0)
    severity = Column(String(20), default="warning") # info, warning, critical
    is_active = Column(Boolean, default=True)
    cooldown_minutes = Column(Integer, default=60)


class WeatherAlert(Base):
    """Eventos de alerta generados."""
    __tablename__ = "weather_alerts"
    id = Column(Integer, primary_key=True, index=True)
    rule_id = Column(Integer, ForeignKey("weather_alert_rules.id"), index=True)
    triggered_at = Column(DateTime, nullable=False, index=True)
    resolved_at = Column(DateTime)
    trigger_value = Column(Float)
    severity = Column(String(20))
    message = Column(Text)
    acknowledged_by = Column(Integer, ForeignKey("usuarios.id"))
    acknowledged_at = Column(DateTime)
    action_taken = Column(Text)


# ══════════════════════════════════════════════════════════════════════════════
# MÓDULO SANIDAD — MIP: Monitoreo plagas/enfermedades, trampas, spray logs
# ══════════════════════════════════════════════════════════════════════════════

class PestCatalog(Base):
    """Catálogo de plagas y enfermedades — datos de referencia."""
    __tablename__ = "pest_catalog"
    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(10), unique=True, nullable=False)  # PL-001, EN-001
    common_name = Column(String(150), nullable=False)
    scientific_name = Column(String(200))
    category = Column(String(30))         # INSECTO, ACARO, HONGO, BACTERIA, NEMATODO
    pest_type = Column(String(30))        # plaga, enfermedad, benefico
    importance = Column(String(20))       # primaria, secundaria, cuarentena
    # Monitoring defaults
    monitoring_method = Column(String(50))   # conteo_directo, trampa, daño_visual
    sample_unit = Column(String(30))         # hoja, fruto, trampa, arbol
    sample_size = Column(Integer)            # 100 hojas típico
    monitoring_frequency_days = Column(Integer, default=7)
    # Thresholds
    action_threshold = Column(Float)
    threshold_unit = Column(String(50))      # trips/hoja, %daño, moscas/trampa/dia
    threshold_description = Column(Text)
    # Plant parts
    affects_leaves = Column(Boolean, default=False)
    affects_fruit = Column(Boolean, default=False)
    affects_roots = Column(Boolean, default=False)
    affects_trunk = Column(Boolean, default=False)
    # Seasonality
    peak_months = Column(String(50))         # "3,4,5,6" (mar-jun)
    activo = Column(Boolean, default=True)


class ScoutingVisit(Base):
    """Visita de monitoreo fitosanitario a un campo."""
    __tablename__ = "scouting_visits"
    id = Column(Integer, primary_key=True, index=True)
    visit_code = Column(String(20), unique=True, nullable=False)  # MON-0001
    campo_id = Column(String(10), ForeignKey("campos.id_campo"), nullable=False, index=True)
    scout_id = Column(Integer, ForeignKey("usuarios.id"))
    visit_date = Column(DateTime, nullable=False, index=True)
    phenological_stage = Column(String(50))   # floracion, cuaje, llenado, cosecha, reposo
    trees_sampled = Column(Integer)
    leaves_sampled = Column(Integer)
    fruits_sampled = Column(Integer)
    general_vigor = Column(String(20))        # excelente, bueno, regular, malo, critico
    # Weather at visit
    temperature_c = Column(Float)
    humidity_pct = Column(Float)
    wind_speed_kmh = Column(Float)
    recent_rain_mm = Column(Float)
    observations = Column(Text)
    status = Column(String(20), default="borrador")  # borrador, completado, revisado
    creado_en = Column(DateTime, server_default=func.now())
    campo = relationship("Campo")


class PestObservation(Base):
    """Observación de plaga/enfermedad dentro de una visita."""
    __tablename__ = "pest_observations"
    id = Column(Integer, primary_key=True, index=True)
    visit_id = Column(Integer, ForeignKey("scouting_visits.id"), nullable=False, index=True)
    pest_id = Column(Integer, ForeignKey("pest_catalog.id"), nullable=False, index=True)
    # Counting
    total_count = Column(Integer)
    sample_units = Column(Integer)
    average_per_unit = Column(Float)           # trips/hoja, ácaros/hoja
    # Damage
    severity_scale = Column(Integer)           # 0-5
    affected_area_pct = Column(Float)          # % superficie dañada
    affected_trees_pct = Column(Float)         # % árboles con síntomas
    # Location
    plant_part = Column(String(30))            # hojas, fruto, raiz, tronco, flores
    canopy_position = Column(String(20))       # superior, medio, inferior
    # Decision
    threshold_exceeded = Column(Boolean)
    recommended_action = Column(String(50))    # ninguna, monitorear, cultural, biologico, quimico
    notes = Column(Text)
    visit = relationship("ScoutingVisit")
    pest = relationship("PestCatalog")


class Trap(Base):
    """Trampas físicas para monitoreo (McPhail, Jackson, pegajosas)."""
    __tablename__ = "traps"
    id = Column(Integer, primary_key=True, index=True)
    trap_code = Column(String(20), unique=True, nullable=False)  # TR-001
    trap_type = Column(String(30))            # mcphail, jackson, pegajosa_amarilla
    campo_id = Column(String(10), ForeignKey("campos.id_campo"), nullable=False, index=True)
    target_pest_id = Column(Integer, ForeignKey("pest_catalog.id"))
    location_desc = Column(String(200))
    latitude = Column(Float)
    longitude = Column(Float)
    installation_date = Column(DateTime)
    lure_type = Column(String(100))
    lure_change_days = Column(Integer, default=14)
    last_lure_change = Column(DateTime)
    status = Column(String(20), default="activa")  # activa, mantenimiento, retirada


class TrapReading(Base):
    """Lectura de trampa durante una visita."""
    __tablename__ = "trap_readings"
    id = Column(Integer, primary_key=True, index=True)
    trap_id = Column(Integer, ForeignKey("traps.id"), nullable=False, index=True)
    visit_id = Column(Integer, ForeignKey("scouting_visits.id"), index=True)
    reading_date = Column(DateTime, nullable=False)
    reader_id = Column(Integer, ForeignKey("usuarios.id"))
    target_count = Column(Integer, default=0)
    non_target_count = Column(Integer, default=0)
    days_since_last = Column(Integer)
    catches_per_trap_day = Column(Float)        # count / days
    lure_replaced = Column(Boolean, default=False)
    threshold_exceeded = Column(Boolean)
    notes = Column(Text)


class SprayLog(Base):
    """Registro de aplicación fitosanitaria — vinculado a OT existente."""
    __tablename__ = "spray_logs"
    id = Column(Integer, primary_key=True, index=True)
    spray_code = Column(String(20), unique=True, nullable=False)  # SPR-0001
    ot_id = Column(Integer, ForeignKey("ordenes_trabajo.ot_id"), index=True)
    campo_id = Column(String(10), ForeignKey("campos.id_campo"), nullable=False, index=True)
    application_date = Column(DateTime, nullable=False)
    applicator_id = Column(Integer, ForeignKey("usuarios.id"))
    # Trigger
    trigger_visit_id = Column(Integer, ForeignKey("scouting_visits.id"))
    target_pests = Column(Text)                  # JSON: ["PL-001","EN-002"]
    justification = Column(Text)
    # Product details (JSON array of products)
    products_json = Column(Text)                 # [{producto_id, dosis_ha, grupo_moa, ...}]
    # Application params
    water_volume_l_ha = Column(Float)
    method = Column(String(50))                  # aspersora, drench, inyeccion_tronco, mochila
    area_treated_ha = Column(Float)
    # Weather at application
    temp_c = Column(Float)
    humidity_pct = Column(Float)
    wind_kmh = Column(Float)
    # Compliance
    rei_hours = Column(Integer)                  # Restricted Entry Interval
    rei_expiry = Column(DateTime)
    phi_days = Column(Integer)                   # Pre-Harvest Interval
    earliest_harvest = Column(DateTime)
    # Efficacy
    efficacy_check_date = Column(DateTime)
    efficacy_rating = Column(String(20))         # excelente, bueno, moderado, pobre
    notes = Column(Text)
    creado_en = Column(DateTime, server_default=func.now())


# ══════════════════════════════════════════════════════════════════════════════
# MÓDULO RIEGO — Humedad de suelo, eventos de irrigación, balance hídrico
# ══════════════════════════════════════════════════════════════════════════════

class SoilReading(Base):
    """Lectura de humedad de suelo — manual (tensiómetro/visual) o sensor."""
    __tablename__ = "soil_readings"
    id = Column(Integer, primary_key=True, index=True)
    campo_id = Column(String(10), ForeignKey("campos.id_campo"), nullable=False, index=True)
    reading_datetime = Column(DateTime, nullable=False, index=True)
    depth_cm = Column(Integer, nullable=False)     # 20, 30, 50, 60
    value_cbar = Column(Float)                     # Tensiómetro
    visual_scale = Column(Integer)                 # 1=saturado ... 5=seco
    reading_type = Column(String(20))              # tensiometro, visual, sensor
    location_zone = Column(String(20))             # A1, B1, etc.
    position = Column(String(30))                  # bajo_gotero, entre_goteros, borde_copa
    recorded_by = Column(Integer, ForeignKey("usuarios.id"))
    sensor_id = Column(String(20))                 # Nullable — para futura integración IoT
    notes = Column(Text)
    creado_en = Column(DateTime, server_default=func.now())


class IrrigationEvent(Base):
    """Evento de riego ejecutado."""
    __tablename__ = "irrigation_events"
    id = Column(Integer, primary_key=True, index=True)
    campo_id = Column(String(10), ForeignKey("campos.id_campo"), nullable=False, index=True)
    start_time = Column(DateTime, nullable=False)
    end_time = Column(DateTime)
    duration_minutes = Column(Integer)
    volume_liters = Column(Float)
    volume_mm = Column(Float)                      # Lámina aplicada
    # Fertirrigación
    fertigation = Column(Boolean, default=False)
    fert_product = Column(String(200))
    fert_dose = Column(String(100))
    # Source
    source = Column(String(20), default="manual")  # manual, controlador, programado
    recorded_by = Column(Integer, ForeignKey("usuarios.id"))
    notes = Column(Text)
    creado_en = Column(DateTime, server_default=func.now())


class DailyWaterBalance(Base):
    """Balance hídrico diario por campo."""
    __tablename__ = "daily_water_balance"
    id = Column(Integer, primary_key=True, index=True)
    campo_id = Column(String(10), ForeignKey("campos.id_campo"), nullable=False, index=True)
    date = Column(DateTime, nullable=False, index=True)
    et0_mm = Column(Float)                         # De clima
    kc = Column(Float)                             # Coeficiente cultivo
    etc_mm = Column(Float)                         # ET₀ × Kc
    rainfall_mm = Column(Float, default=0)
    irrigation_mm = Column(Float, default=0)
    balance_mm = Column(Float)                     # Lluvia + Riego - ETc
    cumulative_deficit_mm = Column(Float)
    status = Column(String(20))                    # adecuado, deficit, exceso


class FieldIrrigationConfig(Base):
    """Configuración de riego por campo — umbrales y sistema."""
    __tablename__ = "field_irrigation_config"
    id = Column(Integer, primary_key=True, index=True)
    campo_id = Column(String(10), ForeignKey("campos.id_campo"), unique=True, nullable=False)
    # Sistema
    irrigation_type = Column(String(30), default="goteo")
    emitters_per_tree = Column(Integer, default=4)
    emitter_flow_lph = Column(Float, default=4.0)
    # Umbrales (centibares)
    field_capacity_cbar = Column(Float, default=10)
    refill_point_cbar = Column(Float, default=25)
    stress_point_cbar = Column(Float, default=40)
    waterlog_point_cbar = Column(Float, default=5)
    # Kc por etapa (JSON)
    kc_by_stage = Column(Text)  # {"floracion":0.65,"cuaje":0.75,"llenado":0.85,"maduracion":0.78}
    notes = Column(Text)


class IrrigationSchedule(Base):
    """Programa de riego recurrente por campo."""
    __tablename__ = "irrigation_schedules"
    id = Column(Integer, primary_key=True, index=True)
    campo_id = Column(String(10), ForeignKey("campos.id_campo"), nullable=False, index=True)
    nombre = Column(String(100), nullable=False)
    dias_semana = Column(String(20))
    hora_inicio = Column(String(5))
    duracion_minutos = Column(Integer)
    volumen_litros = Column(Float)
    fertiriego = Column(Boolean, default=False)
    activo = Column(Boolean, default=True)
    notas = Column(Text)
    creado_en = Column(DateTime, server_default=func.now())


# ══════════════════════════════════════════════════════════════════════════════
# MÓDULO CONTABILIDAD — Núcleo contable, CxP/CxC, activos, nómina, fiscal
# ══════════════════════════════════════════════════════════════════════════════

class PartidaEstadoFinanciero(Base):
    __tablename__ = "partidas_estado_financiero"
    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String(150), nullable=False)
    estado = Column(String(30), nullable=False)        # balance_general, estado_resultados
    clasificacion = Column(String(40), nullable=False) # activo_corriente, activo_no_corriente, pasivo_corriente, pasivo_no_corriente, patrimonio, ingresos, costos, gastos
    padre_id = Column(Integer, ForeignKey("partidas_estado_financiero.id"), nullable=True)
    orden = Column(Integer, default=0)
    invertir_signo = Column(Boolean, default=False)
    es_grupo = Column(Boolean, default=False)
    activo = Column(Boolean, default=True)
    padre = relationship("PartidaEstadoFinanciero", remote_side=[id], backref="hijos")
    cuentas = relationship("CuentaContable", back_populates="partida")


class CuentaContable(Base):
    __tablename__ = "cuentas_contables"
    id = Column(Integer, primary_key=True, index=True)
    codigo = Column(String(10), unique=True, index=True, nullable=False)
    nombre = Column(String(200), nullable=False)
    tipo = Column(String(20), nullable=False)          # activo, pasivo, patrimonio, ingreso, costo, gasto
    naturaleza = Column(String(10), nullable=False)    # deudora, acreedora
    grupo = Column(String(100))
    nivel = Column(Integer, default=1)
    cuenta_padre_id = Column(Integer, ForeignKey("cuentas_contables.id"))
    partida_id = Column(Integer, ForeignKey("partidas_estado_financiero.id"), nullable=True)
    acepta_movimientos = Column(Boolean, default=True)
    activo = Column(Boolean, default=True)
    cuenta_padre = relationship("CuentaContable", remote_side=[id])
    partida = relationship("PartidaEstadoFinanciero", back_populates="cuentas")
    lineas = relationship("LineaAsiento", back_populates="cuenta")


class PeriodoContable(Base):
    __tablename__ = "periodos_contables"
    id = Column(Integer, primary_key=True, index=True)
    anio = Column(Integer, nullable=False)
    mes = Column(Integer, nullable=False)
    nombre = Column(String(50))
    fecha_inicio = Column(Date, nullable=False)
    fecha_fin = Column(Date, nullable=False)
    estado = Column(String(20), default="abierto")     # abierto, cerrado
    cerrado_por = Column(String(100))
    cerrado_en = Column(DateTime)
    __table_args__ = (UniqueConstraint("anio", "mes", name="uq_periodo_anio_mes"),)


class DiarioContable(Base):
    __tablename__ = "diarios_contables"
    id = Column(Integer, primary_key=True, index=True)
    codigo = Column(String(10), unique=True, nullable=False)
    nombre = Column(String(100), nullable=False)
    tipo = Column(String(30))
    cuenta_default_id = Column(Integer, ForeignKey("cuentas_contables.id"))
    activo = Column(Boolean, default=True)
    cuenta_default = relationship("CuentaContable")


class AsientoContable(Base):
    __tablename__ = "asientos_contables"
    id = Column(Integer, primary_key=True, index=True)
    numero = Column(String(20), unique=True, index=True, nullable=False)
    fecha = Column(Date, nullable=False, index=True)
    periodo_id = Column(Integer, ForeignKey("periodos_contables.id"), index=True)
    diario_id = Column(Integer, ForeignKey("diarios_contables.id"), index=True)
    tipo = Column(String(20), default="manual")        # automatico, manual, cierre, apertura
    origen = Column(String(10))                        # OT, OC, GR, PAG, COB, VTA, AJ, NOM, DEP, LIQ, MAN
    referencia_id = Column(String(50))
    descripcion = Column(Text)
    total_debe = Column(Numeric(14, 2), default=0)
    total_haber = Column(Numeric(14, 2), default=0)
    estado = Column(String(20), default="borrador")    # borrador, contabilizado, anulado
    creado_por = Column(String(100))
    contabilizado_por = Column(String(100))
    contabilizado_en = Column(DateTime)
    anulado_por = Column(String(100))
    asiento_reverso_id = Column(Integer, ForeignKey("asientos_contables.id"))
    origen_id = Column(Integer, index=True)
    creado_en = Column(DateTime, server_default=func.now())
    periodo = relationship("PeriodoContable")
    diario = relationship("DiarioContable")
    lineas = relationship("LineaAsiento", back_populates="asiento", cascade="all, delete-orphan")


class LineaAsiento(Base):
    __tablename__ = "lineas_asiento"
    id = Column(Integer, primary_key=True, index=True)
    asiento_id = Column(Integer, ForeignKey("asientos_contables.id"), nullable=False, index=True)
    cuenta_id = Column(Integer, ForeignKey("cuentas_contables.id"), nullable=False, index=True)
    debe = Column(Numeric(14, 2), default=0)
    haber = Column(Numeric(14, 2), default=0)
    campo_id = Column(String(10), ForeignKey("campos.id_campo"), index=True)
    tercero_id = Column(String(50))
    descripcion_linea = Column(String(300))
    unidad_negocio_id = Column(Integer, ForeignKey("unidades_negocio.id"), index=True)
    departamento_id = Column(Integer, ForeignKey("departamentos.id"), index=True)
    almacen_id = Column(Integer, ForeignKey("almacenes.id"), index=True)
    asiento = relationship("AsientoContable", back_populates="lineas")
    cuenta = relationship("CuentaContable", back_populates="lineas")


class Cliente(Base):
    __tablename__ = "clientes"
    id = Column(Integer, primary_key=True, index=True)
    id_cliente = Column(String(10), unique=True, index=True, nullable=False)
    nombre = Column(String(200), nullable=False)
    rnc_cedula = Column(String(20))
    tipo_rnc = Column(String(20))                      # RNC, Cedula, Pasaporte
    direccion = Column(Text)
    telefono = Column(String(50))
    email = Column(String(200))
    contacto = Column(String(200))
    condicion_pago_dias = Column(Integer, default=30)
    tipo_ncf_default = Column(String(5), default="B01")
    cuenta_cxc_id = Column(Integer, ForeignKey("cuentas_contables.id"))
    notas = Column(Text)
    activo = Column(Boolean, default=True)
    creado_en = Column(DateTime, server_default=func.now())
    cuenta_cxc = relationship("CuentaContable", foreign_keys=[cuenta_cxc_id])
    cuentas_por_cobrar = relationship("CuentaPorCobrar", back_populates="cliente")


class CuentaBancaria(Base):
    __tablename__ = "cuentas_bancarias"
    id = Column(Integer, primary_key=True, index=True)
    banco = Column(String(100), nullable=False)
    tipo_cuenta = Column(String(20), default="corriente")  # corriente, ahorro
    numero_cuenta = Column(String(50), nullable=False)
    moneda = Column(String(5), default="DOP")              # DOP, USD
    cuenta_contable_id = Column(Integer, ForeignKey("cuentas_contables.id"))
    saldo_segun_libro = Column(Numeric(14, 2), default=0)
    nombre_corto = Column(String(50))
    activo = Column(Boolean, default=True)
    cuenta_contable = relationship("CuentaContable")


class ConfiguracionEmpresa(Base):
    __tablename__ = "configuracion_empresa"
    id = Column(Integer, primary_key=True, index=True)
    razon_social = Column(String(200))
    nombre_comercial = Column(String(200))
    rnc = Column(String(20))
    direccion = Column(Text)
    telefono = Column(String(50))
    email = Column(String(200))
    logo_base64 = Column(Text)
    moneda_funcional = Column(String(5), default="DOP")
    regimen_fiscal = Column(String(100))


class SecuenciaNcf(Base):
    __tablename__ = "secuencias_ncf"
    id = Column(Integer, primary_key=True, index=True)
    tipo_ncf = Column(String(5), nullable=False)       # B01, B02, B04, B11, B14, B15
    serie = Column(String(20))
    desde = Column(Integer, nullable=False)
    hasta = Column(Integer, nullable=False)
    siguiente = Column(Integer, nullable=False)
    fecha_vencimiento = Column(Date)
    activo = Column(Boolean, default=True)


class ReglaContabilizacion(Base):
    __tablename__ = "reglas_contabilizacion"
    id = Column(Integer, primary_key=True, index=True)
    evento = Column(String(30), nullable=False)        # OT_CIERRE, OC_RECEPCION, PAGO, COBRO, VENTA, etc.
    concepto = Column(String(50), nullable=False)      # mano_obra, insumos, equipo, etc.
    cuenta_debe_id = Column(Integer, ForeignKey("cuentas_contables.id"), nullable=False)
    cuenta_haber_id = Column(Integer, ForeignKey("cuentas_contables.id"), nullable=False)
    descripcion = Column(String(300))
    activo = Column(Boolean, default=True)
    cuenta_debe = relationship("CuentaContable", foreign_keys=[cuenta_debe_id])
    cuenta_haber = relationship("CuentaContable", foreign_keys=[cuenta_haber_id])


class SaldoMensual(Base):
    __tablename__ = "saldos_mensuales"
    id = Column(Integer, primary_key=True, index=True)
    cuenta_id = Column(Integer, ForeignKey("cuentas_contables.id"), nullable=False, index=True)
    periodo_id = Column(Integer, ForeignKey("periodos_contables.id"), nullable=False, index=True)
    saldo_deudor = Column(Numeric(14, 2), default=0)
    saldo_acreedor = Column(Numeric(14, 2), default=0)
    saldo_neto = Column(Numeric(14, 2), default=0)
    __table_args__ = (UniqueConstraint("cuenta_id", "periodo_id", name="uq_saldo_cuenta_periodo"),)


class CuentaPorPagar(Base):
    __tablename__ = "cuentas_por_pagar"
    id = Column(Integer, primary_key=True, index=True)
    numero = Column(String(20), unique=True, index=True, nullable=False)
    proveedor_id = Column(Integer, ForeignKey("proveedores.id"), nullable=False, index=True)
    oc_id = Column(String(20), ForeignKey("ordenes_compra.oc_id"))
    tipo_ncf = Column(String(5))
    ncf = Column(String(30))
    num_factura_proveedor = Column(String(100))
    fecha_factura = Column(Date, nullable=False)
    fecha_vencimiento = Column(Date)
    subtotal = Column(Numeric(14, 2), default=0)
    itbis = Column(Numeric(14, 2), default=0)
    retencion_isr = Column(Numeric(14, 2), default=0)
    total = Column(Numeric(14, 2), default=0)
    saldo_pendiente = Column(Numeric(14, 2), default=0)
    estado = Column(String(20), default="pendiente")   # pendiente, parcial, pagada, anulada
    notas = Column(Text)
    asiento_id = Column(Integer, ForeignKey("asientos_contables.id"))
    creado_en = Column(DateTime, server_default=func.now())
    proveedor = relationship("Proveedor")
    pagos = relationship("Pago", back_populates="cxp")


class Pago(Base):
    __tablename__ = "pagos"
    id = Column(Integer, primary_key=True, index=True)
    numero = Column(String(20), unique=True, index=True, nullable=False)
    cxp_id = Column(Integer, ForeignKey("cuentas_por_pagar.id"), nullable=False, index=True)
    fecha = Column(Date, nullable=False)
    monto = Column(Numeric(14, 2), nullable=False)
    metodo_pago = Column(String(30))                   # efectivo, transferencia, cheque
    referencia_bancaria = Column(String(100))
    cuenta_bancaria_id = Column(Integer, ForeignKey("cuentas_bancarias.id"))
    asiento_id = Column(Integer, ForeignKey("asientos_contables.id"))
    creado_en = Column(DateTime, server_default=func.now())
    cxp = relationship("CuentaPorPagar", back_populates="pagos")
    cuenta_bancaria = relationship("CuentaBancaria")


class CuentaPorCobrar(Base):
    __tablename__ = "cuentas_por_cobrar"
    id = Column(Integer, primary_key=True, index=True)
    numero = Column(String(20), unique=True, index=True, nullable=False)
    cliente_id = Column(Integer, ForeignKey("clientes.id"), nullable=False, index=True)
    tipo_ncf = Column(String(5))
    ncf = Column(String(30))
    fecha = Column(Date, nullable=False)
    fecha_vencimiento = Column(Date)
    moneda = Column(String(5), default="DOP")
    tasa_cambio = Column(Numeric(10, 4), default=1)
    subtotal = Column(Numeric(14, 2), default=0)
    itbis = Column(Numeric(14, 2), default=0)
    total = Column(Numeric(14, 2), default=0)
    total_dop = Column(Numeric(14, 2), default=0)
    saldo_pendiente = Column(Numeric(14, 2), default=0)
    estado = Column(String(20), default="pendiente")   # pendiente, parcial, cobrada, anulada
    campo_id = Column(String(10), ForeignKey("campos.id_campo"))
    temporada = Column(String(20))
    kg_vendidos = Column(Numeric(10, 2))
    precio_por_kg = Column(Numeric(10, 2))
    asiento_id = Column(Integer, ForeignKey("asientos_contables.id"))
    creado_en = Column(DateTime, server_default=func.now())
    cliente = relationship("Cliente", back_populates="cuentas_por_cobrar")
    cobros = relationship("Cobro", back_populates="cxc")
    campo = relationship("Campo")


class Cobro(Base):
    __tablename__ = "cobros"
    id = Column(Integer, primary_key=True, index=True)
    numero = Column(String(20), unique=True, index=True, nullable=False)
    cxc_id = Column(Integer, ForeignKey("cuentas_por_cobrar.id"), nullable=False, index=True)
    fecha = Column(Date, nullable=False)
    monto = Column(Numeric(14, 2), nullable=False)
    metodo_pago = Column(String(30))
    referencia_bancaria = Column(String(100))
    cuenta_bancaria_id = Column(Integer, ForeignKey("cuentas_bancarias.id"))
    asiento_id = Column(Integer, ForeignKey("asientos_contables.id"))
    creado_en = Column(DateTime, server_default=func.now())
    cxc = relationship("CuentaPorCobrar", back_populates="cobros")
    cuenta_bancaria = relationship("CuentaBancaria")


class NotaCredito(Base):
    __tablename__ = "notas_credito"
    id = Column(Integer, primary_key=True, index=True)
    numero = Column(String(20), unique=True, index=True, nullable=False)
    tipo = Column(String(20), nullable=False)          # proveedor, cliente
    referencia_id = Column(Integer)
    ncf = Column(String(30))
    fecha = Column(Date, nullable=False)
    motivo = Column(Text)
    subtotal = Column(Numeric(14, 2), default=0)
    itbis = Column(Numeric(14, 2), default=0)
    total = Column(Numeric(14, 2), default=0)
    asiento_id = Column(Integer, ForeignKey("asientos_contables.id"))
    creado_en = Column(DateTime, server_default=func.now())


class NotaDebito(Base):
    __tablename__ = "notas_debito"
    id = Column(Integer, primary_key=True, index=True)
    numero = Column(String(20), unique=True, index=True, nullable=False)
    tipo = Column(String(20), nullable=False)
    referencia_id = Column(Integer)
    ncf = Column(String(30))
    fecha = Column(Date, nullable=False)
    motivo = Column(Text)
    subtotal = Column(Numeric(14, 2), default=0)
    itbis = Column(Numeric(14, 2), default=0)
    total = Column(Numeric(14, 2), default=0)
    asiento_id = Column(Integer, ForeignKey("asientos_contables.id"))
    creado_en = Column(DateTime, server_default=func.now())


class ActivoFijo(Base):
    __tablename__ = "activos_fijos"
    id = Column(Integer, primary_key=True, index=True)
    codigo = Column(String(20), unique=True, index=True, nullable=False)
    nombre = Column(String(200), nullable=False)
    categoria = Column(String(30))                     # equipo, vehiculo, planta_portadora, infraestructura
    fecha_adquisicion = Column(Date)
    costo_adquisicion = Column(Numeric(14, 2), default=0)
    vida_util_meses = Column(Integer)
    valor_residual = Column(Numeric(14, 2), default=0)
    depreciacion_acumulada = Column(Numeric(14, 2), default=0)
    metodo_depreciacion = Column(String(20), default="lineal")
    campo_id = Column(String(10), ForeignKey("campos.id_campo"))
    cuenta_activo_id = Column(Integer, ForeignKey("cuentas_contables.id"))
    cuenta_depreciacion_id = Column(Integer, ForeignKey("cuentas_contables.id"))
    cuenta_gasto_dep_id = Column(Integer, ForeignKey("cuentas_contables.id"))
    activo = Column(Boolean, default=True)
    creado_en = Column(DateTime, server_default=func.now())
    campo = relationship("Campo")
    cuenta_activo = relationship("CuentaContable", foreign_keys=[cuenta_activo_id])
    cuenta_depreciacion = relationship("CuentaContable", foreign_keys=[cuenta_depreciacion_id])
    cuenta_gasto_dep = relationship("CuentaContable", foreign_keys=[cuenta_gasto_dep_id])


class DepreciacionHistorial(Base):
    __tablename__ = "depreciacion_historial"
    id = Column(Integer, primary_key=True, index=True)
    activo_id = Column(Integer, ForeignKey("activos_fijos.id"), nullable=False, index=True)
    periodo_id = Column(Integer, ForeignKey("periodos_contables.id"), nullable=False)
    monto = Column(Numeric(14, 2), nullable=False)
    depreciacion_acumulada_post = Column(Numeric(14, 2))
    asiento_id = Column(Integer, ForeignKey("asientos_contables.id"))


class Presupuesto(Base):
    __tablename__ = "presupuestos"
    id = Column(Integer, primary_key=True, index=True)
    anio = Column(Integer, nullable=False)
    cuenta_id = Column(Integer, ForeignKey("cuentas_contables.id"), nullable=False)
    campo_id = Column(String(10), ForeignKey("campos.id_campo"))
    monto_ene = Column(Numeric(14, 2), default=0)
    monto_feb = Column(Numeric(14, 2), default=0)
    monto_mar = Column(Numeric(14, 2), default=0)
    monto_abr = Column(Numeric(14, 2), default=0)
    monto_may = Column(Numeric(14, 2), default=0)
    monto_jun = Column(Numeric(14, 2), default=0)
    monto_jul = Column(Numeric(14, 2), default=0)
    monto_ago = Column(Numeric(14, 2), default=0)
    monto_sep = Column(Numeric(14, 2), default=0)
    monto_oct = Column(Numeric(14, 2), default=0)
    monto_nov = Column(Numeric(14, 2), default=0)
    monto_dic = Column(Numeric(14, 2), default=0)
    descripcion = Column(String(300))
    unidad_negocio_id = Column(Integer, ForeignKey("unidades_negocio.id"), index=True)
    departamento_id = Column(Integer, ForeignKey("departamentos.id"), index=True)
    almacen_id = Column(Integer, ForeignKey("almacenes.id"), index=True)
    version = Column(String(30), default="original")
    estado = Column(String(20), default="borrador")


class TransferenciaPresupuesto(Base):
    __tablename__ = "transferencias_presupuesto"
    id = Column(Integer, primary_key=True, index=True)
    fecha = Column(Date, nullable=False)
    origen_presupuesto_id = Column(Integer, ForeignKey("presupuestos.id"), nullable=False)
    destino_presupuesto_id = Column(Integer, ForeignKey("presupuestos.id"), nullable=False)
    mes = Column(Integer, nullable=False)
    monto = Column(Numeric(14, 2), nullable=False)
    motivo = Column(String(300))
    usuario_id = Column(Integer, ForeignKey("usuarios.id"))
    created_at = Column(DateTime, server_default=func.now())


class ConfigPresupuesto(Base):
    __tablename__ = "config_presupuesto"
    id = Column(Integer, primary_key=True, index=True)
    umbral_alerta = Column(Integer, default=85)
    umbral_bloqueo = Column(Integer, default=100)
    control_habilitado = Column(Boolean, default=True)
    distribucion_default = Column(String(20), default="mensual")
    requiere_aprobacion = Column(Boolean, default=True)


class RegistroPresupuestario(Base):
    __tablename__ = "registros_presupuestarios"
    id = Column(Integer, primary_key=True, index=True)
    numero = Column(String(20), unique=True, nullable=False)
    fecha = Column(Date, nullable=False)
    tipo = Column(String(20), nullable=False)
    anio = Column(Integer, nullable=False)
    descripcion = Column(String(300))
    estado = Column(String(20), default="borrador")
    usuario_id = Column(Integer, ForeignKey("usuarios.id"))
    created_at = Column(DateTime, server_default=func.now())
    lineas = relationship("LineaRegistroPresupuestario", cascade="all, delete-orphan",
                          backref="registro")


class LineaRegistroPresupuestario(Base):
    __tablename__ = "lineas_registro_presupuestario"
    id = Column(Integer, primary_key=True, index=True)
    registro_id = Column(Integer, ForeignKey("registros_presupuestarios.id", ondelete="CASCADE"),
                         nullable=False)
    cuenta_id = Column(Integer, ForeignKey("cuentas_contables.id"), nullable=False)
    campo_id = Column(String(10), ForeignKey("campos.id_campo"))
    unidad_negocio_id = Column(Integer, ForeignKey("unidades_negocio.id"))
    departamento_id = Column(Integer, ForeignKey("departamentos.id"))
    monto_ene = Column(Numeric(14, 2), default=0)
    monto_feb = Column(Numeric(14, 2), default=0)
    monto_mar = Column(Numeric(14, 2), default=0)
    monto_abr = Column(Numeric(14, 2), default=0)
    monto_may = Column(Numeric(14, 2), default=0)
    monto_jun = Column(Numeric(14, 2), default=0)
    monto_jul = Column(Numeric(14, 2), default=0)
    monto_ago = Column(Numeric(14, 2), default=0)
    monto_sep = Column(Numeric(14, 2), default=0)
    monto_oct = Column(Numeric(14, 2), default=0)
    monto_nov = Column(Numeric(14, 2), default=0)
    monto_dic = Column(Numeric(14, 2), default=0)
    descripcion = Column(String(200))


class CosechaLiquidacion(Base):
    __tablename__ = "cosecha_liquidaciones"
    id = Column(Integer, primary_key=True, index=True)
    campo_id = Column(String(10), ForeignKey("campos.id_campo"), nullable=False)
    temporada = Column(String(20), nullable=False)
    fecha_liquidacion = Column(Date, nullable=False)
    costo_mo_acumulado = Column(Numeric(14, 2), default=0)
    costo_insumos_acumulado = Column(Numeric(14, 2), default=0)
    costo_equipo_acumulado = Column(Numeric(14, 2), default=0)
    costo_otros_acumulado = Column(Numeric(14, 2), default=0)
    costo_total = Column(Numeric(14, 2), default=0)
    kg_cosechados = Column(Numeric(10, 2), default=0)
    costo_por_kg = Column(Numeric(10, 4), default=0)
    asiento_id = Column(Integer, ForeignKey("asientos_contables.id"))
    estado = Column(String(20), default="liquidada")   # liquidada, ajustada
    creado_en = Column(DateTime, server_default=func.now())


class NominaPeriodo(Base):
    __tablename__ = "nomina_periodos"
    id = Column(Integer, primary_key=True, index=True)
    tipo = Column(String(20), default="quincenal")     # quincenal, mensual
    fecha_inicio = Column(Date, nullable=False)
    fecha_fin = Column(Date, nullable=False)
    total_bruto = Column(Numeric(14, 2), default=0)
    total_deducciones = Column(Numeric(14, 2), default=0)
    total_neto = Column(Numeric(14, 2), default=0)
    total_tss_empleador = Column(Numeric(14, 2), default=0)
    estado = Column(String(20), default="borrador")    # borrador, procesada, pagada
    asiento_id = Column(Integer, ForeignKey("asientos_contables.id"))
    procesado_por = Column(String(100))
    creado_en = Column(DateTime, server_default=func.now())
    detalles = relationship("NominaDetalle", back_populates="periodo")


class NominaDetalle(Base):
    __tablename__ = "nomina_detalle"
    id = Column(Integer, primary_key=True, index=True)
    nomina_periodo_id = Column(Integer, ForeignKey("nomina_periodos.id"), nullable=False, index=True)
    trabajador_id = Column(String(10), ForeignKey("trabajadores.id_trab"), nullable=False)
    dias_trabajados = Column(Integer, default=0)
    salario_bruto = Column(Numeric(14, 2), default=0)
    horas_extra = Column(Numeric(8, 2), default=0)
    monto_horas_extra = Column(Numeric(14, 2), default=0)
    bonificacion = Column(Numeric(14, 2), default=0)
    total_devengado = Column(Numeric(14, 2), default=0)
    deduccion_sfs = Column(Numeric(14, 2), default=0)
    deduccion_afp = Column(Numeric(14, 2), default=0)
    deduccion_isr = Column(Numeric(14, 2), default=0)
    otras_deducciones = Column(Numeric(14, 2), default=0)
    total_deducciones = Column(Numeric(14, 2), default=0)
    neto_a_pagar = Column(Numeric(14, 2), default=0)
    campo_id_principal = Column(String(10), ForeignKey("campos.id_campo"))
    departamento_id = Column(Integer, ForeignKey("departamentos.id"), index=True)
    periodo = relationship("NominaPeriodo", back_populates="detalles")
    trabajador = relationship("Trabajador")


class AsientoRecurrente(Base):
    __tablename__ = "asientos_recurrentes"
    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String(100), nullable=False)
    descripcion_asiento = Column(String(300))
    frecuencia = Column(String(20), default="mensual")    # mensual, quincenal, semanal
    dia_ejecucion = Column(Integer, default=1)
    activo = Column(Boolean, default=True)
    ultima_ejecucion = Column(Date)
    proxima_ejecucion = Column(Date)
    veces_ejecutado = Column(Integer, default=0)
    creado_en = Column(DateTime, server_default=func.now())
    lineas = relationship("AsientoRecurrenteLinea", back_populates="recurrente", cascade="all, delete-orphan")


class AsientoRecurrenteLinea(Base):
    __tablename__ = "asientos_recurrentes_lineas"
    id = Column(Integer, primary_key=True, index=True)
    recurrente_id = Column(Integer, ForeignKey("asientos_recurrentes.id"), nullable=False, index=True)
    cuenta_id = Column(Integer, ForeignKey("cuentas_contables.id"), nullable=False)
    descripcion = Column(String(300))
    debe = Column(Numeric(14, 2), default=0)
    haber = Column(Numeric(14, 2), default=0)
    cuenta = relationship("CuentaContable")
    recurrente = relationship("AsientoRecurrente", back_populates="lineas")


class ConciliacionBancaria(Base):
    __tablename__ = "conciliacion_bancaria"
    id = Column(Integer, primary_key=True, index=True)
    cuenta_bancaria_id = Column(Integer, ForeignKey("cuentas_bancarias.id"), nullable=False)
    periodo_id = Column(Integer, ForeignKey("periodos_contables.id"), nullable=False)
    saldo_extracto = Column(Numeric(14, 2), default=0)
    saldo_libro = Column(Numeric(14, 2), default=0)
    diferencia = Column(Numeric(14, 2), default=0)
    estado = Column(String(20), default="en_proceso")  # en_proceso, conciliada
    fecha_conciliacion = Column(Date)
    conciliado_por = Column(String(100))
    creado_en = Column(DateTime, server_default=func.now())
    partidas = relationship("ConciliacionPartida", back_populates="conciliacion")


class ConciliacionPartida(Base):
    __tablename__ = "conciliacion_partidas"
    id = Column(Integer, primary_key=True, index=True)
    conciliacion_id = Column(Integer, ForeignKey("conciliacion_bancaria.id"), nullable=False, index=True)
    tipo = Column(String(30))                          # cheque_transito, deposito_transito, nd_banco, nc_banco, error
    descripcion = Column(String(300))
    monto = Column(Numeric(14, 2), nullable=False)
    fecha = Column(Date)
    referencia = Column(String(100))
    conciliada = Column(Boolean, default=False)
    asiento_id = Column(Integer, ForeignKey("asientos_contables.id"))
    conciliacion = relationship("ConciliacionBancaria", back_populates="partidas")


# ── Dimensiones Financieras ──────────────────────────────────

class UnidadNegocio(Base):
    __tablename__ = "unidades_negocio"
    id = Column(Integer, primary_key=True, index=True)
    codigo = Column(String(20), unique=True, nullable=False, index=True)
    nombre = Column(String(200), nullable=False)
    descripcion = Column(String(500))
    activo = Column(Boolean, default=True)


class Departamento(Base):
    __tablename__ = "departamentos"
    id = Column(Integer, primary_key=True, index=True)
    codigo = Column(String(20), unique=True, nullable=False, index=True)
    nombre = Column(String(200), nullable=False)
    descripcion = Column(String(500))
    activo = Column(Boolean, default=True)


class Almacen(Base):
    __tablename__ = "almacenes"
    id = Column(Integer, primary_key=True, index=True)
    codigo = Column(String(20), unique=True, nullable=False, index=True)
    nombre = Column(String(200), nullable=False)
    ubicacion = Column(String(300))
    activo = Column(Boolean, default=True)
