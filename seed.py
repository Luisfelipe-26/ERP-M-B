"""Seed completo basado en Excel CORVUS 1ra Quincena Junio"""
from database import SessionLocal, engine
import models
from auth import get_password_hash

models.Base.metadata.create_all(bind=engine)
db = SessionLocal()

# Usuarios
users = [
    ("Administrador CORVUS", "admin@corvus.do", "Corvus2024!", "admin"),
    ("Supervisor Finca", "supervisor@corvus.do", "Super2024!", "supervisor"),
    ("Operador Finca", "operador@corvus.do", "Oper2024!", "operador"),
]
for nombre, email, pwd, rol in users:
    if not db.query(models.Usuario).filter_by(email=email).first():
        db.add(models.Usuario(nombre=nombre, email=email, hashed_password=get_password_hash(pwd), rol=rol))

# 17 Campos productivos (datos del Excel CAT_Campos)
campos_data = [
    ("C01", "A", "Campo 01", 2.5,  1200, 2005, "Goteo", "E1", "Franco Arenoso"),
    ("C02", "A", "Campo 02", 2.0,   980, 2021, "Goteo", "E2", "Franco Arenoso"),
    ("C03", "A", "Campo 03", 1.8,   860, 2021, "Goteo", "E2", "Franco Arcilloso"),
    ("C04", "A", "Campo 04", 2.2,  1050, 2021, "Goteo", "E2", "Franco Arenoso"),
    ("C05", "B", "Campo 05", 3.1,  1450, 2021, "Goteo", "E2", "Franco Arcilloso"),
    ("C06", "B", "Campo 06", 2.8,  1320, 2021, "Goteo", "E2", "Franco Arenoso"),
    ("C07", "B", "Campo 07", 2.4,  1150, 2021, "Goteo", "E2", "Franco Arcilloso"),
    ("C08", "C", "Campo 08", 3.5,  1680, 2021, "Goteo", "E2", "Franco Arenoso"),
    ("C09", "C", "Campo 09", 2.9,  1380, 2021, "Goteo", "E2", "Franco Arcilloso"),
    ("C10", "C", "Campo 10", 2.2,  1050, 2021, "Goteo", "E2", "Franco Arenoso"),
    ("C11", "C", "Campo 11", 2.6,  1240, 2021, "Goteo", "E2", "Franco Arcilloso"),
    ("C12", "D", "Campo 12", 1.9,   900, 2023, "Goteo", "E2", "Franco Arenoso"),
    ("C13", "D", "Campo 13", 2.1,  1000, 2023, "Goteo", "E2", "Franco Arcilloso"),
    ("C14", "D", "Campo 14", 2.3,  1100, 2023, "Goteo", "E2", "Franco Arenoso"),
    ("C15", "E", "Campo 15", 1.7,   810, 2024, "Goteo", "E2", "Franco Arenoso"),
    ("C16", "E", "Campo 16", 1.5,   715, 2024, "Goteo", "E2", "Franco Arcilloso"),
    ("C17", "D", "Campo 17", 1.8,   860, 2021, "Goteo", "E2", "Franco Arenoso"),
]
for c in campos_data:
    if not db.query(models.Campo).filter_by(id_campo=c[0]).first():
        db.add(models.Campo(id_campo=c[0], bloque=c[1], nombre=c[2], area_ha=c[3],
                             n_plantas=c[4], variedad="Hass", ano_siembra=c[5],
                             sistema_riego=c[6], etapa=c[7], suelo=c[8]))

# 20 Trabajadores (datos del Excel CAT_Trabajadores)
trabajadores_data = [
    ("T-001", "Keslin Egzilus",          "Jornalero",   66.25, ""),
    ("T-002", "SANEL JEAN",              "Jornalero",   62.50, ""),
    ("T-003", "DIEBESON",                "Jornalero",   62.50, ""),
    ("T-004", "Kenoldson Exys",          "Jornalero",   62.50, ""),
    ("T-005", "JEAN REDSON (SANSON)",    "Jornalero",   62.50, ""),
    ("T-006", "Methelux Maxo",           "Ajustero",     0.00, "Pagado por ajuste/contrato"),
    ("T-007", "Vital Sanchez",           "Supervisor",  62.50, "Salario fijo RD$20,000/mes"),
    ("T-008", "Fraidys Del Jesus",       "Supervisor",  62.50, ""),
    ("T-009", "Esterlin Del Jesus",      "Supervisor",  62.50, ""),
    ("T-010", "Percido Valerio",         "Supervisor",  62.50, ""),
    ("T-011", "Manuel Guarionex Tejeda", "Supervisor",  62.50, ""),
    ("T-012", "Filistin Celestin",       "Jornalero",   62.50, ""),
    ("T-013", "Toco",                    "Jornalero",   62.50, "Equipo de Maxo"),
    ("T-014", "Kinis Petide",            "Jornalero",   62.50, "Equipo de Maxo"),
    ("T-015", "Choca",                   "Jornalero",   62.50, "Equipo de Maxo"),
    ("T-016", "Eliecer Valerio",         "Jornalero",   62.50, ""),
    ("T-017", "Francisco Santos",        "Jornalero",   62.50, ""),
    ("T-018", "Pedro Martinez",          "Jornalero",   62.50, ""),
    ("T-019", "Juan Perez",              "Jornalero",   62.50, ""),
    ("T-020", "Carlos Rios",             "Jornalero",   62.50, ""),
]
for t in trabajadores_data:
    if not db.query(models.Trabajador).filter_by(id_trab=t[0]).first():
        db.add(models.Trabajador(id_trab=t[0], nombre=t[1], cargo=t[2], costo_hora=t[3], observaciones=t[4]))

# Actividades (datos del Excel CAT_Actividades + CAT_Tarifas)
actividades_data = [
    ("A-001", "Aplicacion foliar",                     "Nutricion", "ha",      530.0,   None),
    ("A-002", "Riego",                                  "Riego",    "ha",      530.0,   None),
    ("A-003", "Control de maleza Quimico",              "Labor",    "ha",      530.0,  400.0),
    ("A-004", "Control de maleza Manual",               "Labor",    "ha",      500.0, 1500.0),
    ("A-005", "Control de maleza Mecanizado",           "Labor",    "ha",      600.0,   None),
    ("A-006", "Fertilizacion Edafica",                  "Nutricion","ha",      530.0,   None),
    ("A-007", "Poda de formacion",                      "Labor",    "arboles", 530.0,   None),
    ("A-008", "Poda de mantenimiento",                  "Labor",    "arboles", 530.0,   None),
    ("A-009", "Proteccion solar foliar",                "Sanidad",  "ha",      530.0,   None),
    ("A-010", "Limpieza de tomas",                      "Riego",    "ha",      530.0,   None),
    ("A-011", "Cableado Generador",                     "Labor",    "ha",      530.0, 3000.0),
    ("A-012", "Construccion de Drenaje",                "Labor",    "m3",      530.0, 25320.0),
    ("A-013", "Sereno de generador",                    "Labor",    "turno",   530.0, 2120.0),
    ("A-014", "Construcción Zanja cables eléctricos",    "Labor",    "m3",      530.0, 12800.0),
    ("A-015", "Identificacion y marcacion de plantas",  "Labor",    "arboles", 530.0,   None),
    ("A-016", "Fertilizacion foliar",                   "Nutricion","ha",      530.0,   None),
    ("A-017", "Aplicacion fungicida",                   "Sanidad",  "ha",      530.0,   None),
    ("A-018", "Aplicacion insecticida",                 "Sanidad",  "ha",      530.0,   None),
    ("A-019", "Manejo de arvenses (limpieza manual)",   "Labor",    "ha",      530.0,   None),
    ("A-020", "Instalacion sistema riego",              "Riego",    "ha",      530.0,   None),
    ("A-021", "Revision y reparacion riego",            "Riego",    "ha",      530.0,   None),
    ("A-022", "Recoleccion y conteo de frutos",         "Labor",    "arboles", 530.0,   None),
    ("A-023", "Aplicacion PBZ (regulador hormonal)",    "Nutricion","arboles", 530.0,   None),
    ("A-024", "Monitoreo de plagas y enfermedades",     "Sanidad",  "ha",      530.0,   None),
    ("A-025", "Instalacion tutores y sostenes",         "Labor",    "arboles", 530.0,   None),
]
for a in actividades_data:
    if not db.query(models.Actividad).filter_by(id_act=a[0]).first():
        db.add(models.Actividad(id_act=a[0], actividad=a[1], tipo=a[2],
                                 unidad_rendimiento=a[3], tarifa_jornada=a[4], tarifa_ajuste=a[5]))

# Productos (datos del Excel CAT_Productos)
productos_data = [
    # id,         nombre,                   tipo,                  unidad, costo_unit, stock, stock_min, proveedor
    ("P-001", "ANASAC IMPERIO",            "FUNGICIDAS",           "L",    850.00,   5.0,  2.0, "Duwest"),
    ("P-002", "MANNIPLEX ZN",              "FERTILIZANTE",         "L",    420.00,  10.0,  5.0, "Fertica"),
    ("P-003", "MANNIPLEX B-MOLY",          "FERTILIZANTE",         "L",    380.00,  10.0,  5.0, "Fertica"),
    ("P-004", "ABANEX PLUS",               "BIOESTIMULANTE",       "L",    650.00,   5.0,  2.0, "Bioagro"),
    ("P-005", "KIYON 25 SC",               "INSECTICIDAS",         "L",   1200.00,   2.0,  1.0, "Comesa"),
    ("P-006", "PH PLUS",                   "REGULADOR HORMONAL",   "Kg",   150.00,  20.0,  5.0, "Ferquido"),
    ("P-007", "ACIDO SALICILICO",          "BIOESTIMULANTE",       "Kg",     0.35,  50.0, 10.0, "Agricenter"),
    ("P-008", "AGROSOL",                   "FERTILIZANTE",         "Kg",  9815.00,   1.0,  0.5, "Juan Jimenez"),
    ("P-009", "UREA (46%N)",               "FERTILIZANTE",         "Kg",    45.00, 200.0, 50.0, "Fertica"),
    ("P-010", "NITRATO DE CALCIO",         "FERTILIZANTE",         "Kg",    68.00, 150.0, 30.0, "Fertica"),
    ("P-011", "FOSFATO MONOAMONICO (MAP)", "FERTILIZANTE",         "Kg",    95.00, 100.0, 20.0, "Almonte Comercial"),
    ("P-012", "CLORURO DE POTASIO (KCl)",  "FERTILIZANTE",         "Kg",    72.00, 120.0, 25.0, "Fertica"),
    ("P-013", "SULFATO DE MAGNESIO",       "FERTILIZANTE",         "Kg",    55.00,  80.0, 20.0, "Ferquido"),
    ("P-014", "GLIFOSATO 48%",             "HERBICIDA",            "L",    185.00,  30.0, 10.0, "Triniagro"),
    ("P-015", "PARAQUAT",                  "HERBICIDA",            "L",    220.00,  15.0,  5.0, "Duwest"),
    ("P-016", "NITRATO DE POTASIO",        "FERTILIZANTE",         "Kg",   110.00,  80.0, 20.0, "Fertica"),
    ("P-017", "SULFATO DE ZINC",           "FERTILIZANTE",         "Kg",    75.00,  50.0, 10.0, "Ferquido"),
    ("P-018", "ACIDO BORICO",              "FERTILIZANTE",         "Kg",    90.00,  30.0,  5.0, "Agricenter"),
    ("P-019", "MANCOZEB 80%",              "FUNGICIDAS",           "Kg",   280.00,  20.0,  5.0, "Duwest"),
    ("P-020", "METALAXIL",                 "FUNGICIDAS",           "Kg",   520.00,  10.0,  3.0, "Comesa"),
    ("P-021", "COBRE OXICLORURO",          "FUNGICIDAS",           "Kg",   150.00,  25.0,  5.0, "Fersan"),
    ("P-022", "ABAMECTINA 1.8% EC",        "INSECTICIDAS",         "L",    750.00,   5.0,  2.0, "Duwest"),
    ("P-023", "IMIDACLOPRID 35% SC",       "INSECTICIDAS",         "L",    890.00,   3.0,  1.0, "Comesa"),
    ("P-024", "LAMBDACIHALOTRINA",         "INSECTICIDAS",         "L",    620.00,   4.0,  1.0, "Agricenter"),
    ("P-025", "PACLOBUTRAZOL (PBZ) 25%",   "PBZ",                  "L",   1800.00,   8.0,  2.0, "Juan Jimenez"),
    ("P-026", "CYTOKININ (CITOQUININA)",   "BIOESTIMULANTE",       "L",    950.00,   3.0,  1.0, "Bioagro"),
    ("P-027", "AMINOACIDOS LIBRES",        "BIOESTIMULANTE",       "L",    480.00,   5.0,  2.0, "Bioagro"),
    ("P-028", "HUMATE DE POTASIO",         "BIOESTIMULANTE",       "Kg",   320.00,  15.0,  5.0, "Agricenter"),
    ("P-029", "CALCIO BORO LIQUIDO",       "FERTILIZANTE",         "L",    390.00,   8.0,  2.0, "Fertica"),
    ("P-030", "FOSFONATO DE POTASIO",      "FUNGICIDAS",           "L",    420.00,   6.0,  2.0, "Comesa"),
]
admin_user = db.query(models.Usuario).filter_by(email="admin@corvus.do").first()
gr_counter = 0
for p in productos_data:
    if not db.query(models.Producto).filter_by(id_prod=p[0]).first():
        prod = models.Producto(id_prod=p[0], producto=p[1], tipo=p[2], unidad=p[3],
                               costo_unitario=p[4], costo_promedio=p[4],
                               stock_actual=p[5], stock_minimo=p[6], proveedor=p[7])
        db.add(prod)
        db.flush()
        if p[5] > 0:
            gr_counter += 1
            db.add(models.MovimientoInventario(
                num_documento=f"GR-{str(gr_counter).zfill(4)}",
                producto_id=p[0],
                tipo_doc="GR",
                tipo="entrada",
                motivo="Saldo Inicial",
                cantidad=p[5],
                costo_unitario=p[4],
                costo_promedio_post=p[4],
                stock_post=p[5],
                proveedor=p[7],
                observacion="Saldo inicial de inventario",
                usuario_id=admin_user.id if admin_user else 1,
            ))

# Inicializar secuencias en base a los datos sembrados
sequences_init = [
    ('CAMPO', 17),
    ('TRAB',  20),
    ('ACT',   25),
    ('PROD',  30),
    ('GR',    gr_counter),
    ('GI',    0),
    ('AJ',    0),
]
for tipo, ultimo in sequences_init:
    if not db.query(models.Sequence).filter_by(tipo=tipo).first():
        db.add(models.Sequence(tipo=tipo, ultimo_numero=ultimo))

# ══════════════════════════════════════════════════════════════════════════════
# CONTABILIDAD — Plan de Cuentas
# ══════════════════════════════════════════════════════════════════════════════

cuentas_data = [
    # (codigo, nombre, tipo, naturaleza, grupo, nivel, acepta_mov)
    # ── ACTIVOS ──
    ("1",       "ACTIVOS",                           "activo",     "deudora", "Balance",     1, False),
    ("1.1",     "ACTIVOS CORRIENTES",                "activo",     "deudora", "Balance",     2, False),
    ("1.1.01",  "Efectivo y Equivalentes",           "activo",     "deudora", "Balance",     3, False),
    ("1.1.01.01","Caja General",                     "activo",     "deudora", "Balance",     4, True),
    ("1.1.01.02","Caja Chica",                       "activo",     "deudora", "Balance",     4, True),
    ("1.1.01.03","Banco Cuenta Corriente DOP",       "activo",     "deudora", "Balance",     4, True),
    ("1.1.01.04","Banco Cuenta Corriente USD",       "activo",     "deudora", "Balance",     4, True),
    ("1.1.02",  "Cuentas por Cobrar",                "activo",     "deudora", "Balance",     3, False),
    ("1.1.02.01","CxC Clientes",                     "activo",     "deudora", "Balance",     4, True),
    ("1.1.02.02","CxC Empleados y Anticipos",        "activo",     "deudora", "Balance",     4, True),
    ("1.1.02.03","CxC ITBIS Pagado (Crédito Fiscal)","activo",     "deudora", "Balance",     4, True),
    ("1.1.02.04","CxC Retenciones Recibidas",        "activo",     "deudora", "Balance",     4, True),
    ("1.1.03",  "Inventarios",                       "activo",     "deudora", "Balance",     3, False),
    ("1.1.03.01","Insumos Agrícolas",                "activo",     "deudora", "Balance",     4, True),
    ("1.1.03.02","Cosecha en Proceso (WIP)",         "activo",     "deudora", "Balance",     4, True),
    ("1.1.03.03","Cosecha Terminada",                "activo",     "deudora", "Balance",     4, True),
    ("1.1.02.05","Anticipo ISR",                      "activo",     "deudora", "Balance",     4, True),
    ("1.2",     "ACTIVOS NO CORRIENTES",             "activo",     "deudora", "Balance",     2, False),
    ("1.2.01",  "Propiedad, Planta y Equipo",        "activo",     "deudora", "Balance",     3, False),
    ("1.2.01.01","Terrenos",                         "activo",     "deudora", "Balance",     4, True),
    ("1.2.01.02","Plantas Portadoras (NIC 16)",      "activo",     "deudora", "Balance",     4, True),
    ("1.2.01.03","Edificaciones e Infraestructura",  "activo",     "deudora", "Balance",     4, True),
    ("1.2.01.04","Maquinaria y Equipo Agrícola",     "activo",     "deudora", "Balance",     4, True),
    ("1.2.01.05","Vehículos",                        "activo",     "deudora", "Balance",     4, True),
    ("1.2.01.06","Sistema de Riego",                 "activo",     "deudora", "Balance",     4, True),
    ("1.2.02",  "Depreciación Acumulada",            "activo",     "acreedora","Balance",     3, False),
    ("1.2.02.01","Dep. Acum. Plantas Portadoras",    "activo",     "acreedora","Balance",     4, True),
    ("1.2.02.02","Dep. Acum. Edificaciones",         "activo",     "acreedora","Balance",     4, True),
    ("1.2.02.03","Dep. Acum. Maquinaria",            "activo",     "acreedora","Balance",     4, True),
    ("1.2.02.04","Dep. Acum. Vehículos",             "activo",     "acreedora","Balance",     4, True),
    ("1.2.02.05","Dep. Acum. Sistema de Riego",      "activo",     "acreedora","Balance",     4, True),
    # ── PASIVOS ──
    ("2",       "PASIVOS",                           "pasivo",     "acreedora","Balance",     1, False),
    ("2.1",     "PASIVOS CORRIENTES",                "pasivo",     "acreedora","Balance",     2, False),
    ("2.1.01",  "Cuentas por Pagar",                 "pasivo",     "acreedora","Balance",     3, False),
    ("2.1.01.01","CxP Proveedores",                  "pasivo",     "acreedora","Balance",     4, True),
    ("2.1.01.02","CxP Nóminas por Pagar",            "pasivo",     "acreedora","Balance",     4, True),
    ("2.1.02",  "Impuestos por Pagar",               "pasivo",     "acreedora","Balance",     3, False),
    ("2.1.02.01","ITBIS por Pagar",                  "pasivo",     "acreedora","Balance",     4, True),
    ("2.1.02.02","ISR por Pagar",                    "pasivo",     "acreedora","Balance",     4, True),
    ("2.1.02.03","Retenciones ISR por Pagar",        "pasivo",     "acreedora","Balance",     4, True),
    ("2.1.03",  "Obligaciones Laborales",            "pasivo",     "acreedora","Balance",     3, False),
    ("2.1.03.01","TSS Empleador por Pagar",          "pasivo",     "acreedora","Balance",     4, True),
    ("2.1.03.02","TSS Empleado Retenido",            "pasivo",     "acreedora","Balance",     4, True),
    ("2.1.03.03","Vacaciones por Pagar",             "pasivo",     "acreedora","Balance",     4, True),
    ("2.1.03.04","Prestaciones por Pagar",           "pasivo",     "acreedora","Balance",     4, True),
    ("2.1.03.05","INFOTEP por Pagar",               "pasivo",     "acreedora","Balance",     4, True),
    ("2.1.04",  "Provisiones",                       "pasivo",     "acreedora","Balance",     3, False),
    ("2.1.04.01","Provisión Prestaciones Laborales", "pasivo",     "acreedora","Balance",     4, True),
    # ── PATRIMONIO ──
    ("3",       "PATRIMONIO",                        "patrimonio", "acreedora","Balance",     1, False),
    ("3.1",     "Capital Social",                    "patrimonio", "acreedora","Balance",     2, True),
    ("3.2",     "Reservas",                          "patrimonio", "acreedora","Balance",     2, True),
    ("3.3",     "Resultados Acumulados",             "patrimonio", "acreedora","Balance",     2, True),
    ("3.4",     "Resultado del Ejercicio",           "patrimonio", "acreedora","Balance",     2, True),
    # ── INGRESOS ──
    ("4",       "INGRESOS",                          "ingreso",    "acreedora","Resultado",   1, False),
    ("4.1",     "Ingresos Operacionales",            "ingreso",    "acreedora","Resultado",   2, False),
    ("4.1.01",  "Venta de Aguacate Hass",            "ingreso",    "acreedora","Resultado",   3, True),
    ("4.1.02",  "Venta de Subproductos",             "ingreso",    "acreedora","Resultado",   3, True),
    ("4.1.03",  "Descuentos sobre Ventas",           "ingreso",    "deudora", "Resultado",   3, True),
    ("4.1.04",  "Devoluciones sobre Ventas",         "ingreso",    "deudora", "Resultado",   3, True),
    ("4.2",     "Otros Ingresos",                    "ingreso",    "acreedora","Resultado",   2, False),
    ("4.2.01",  "Ingreso por Diferencia Cambiaria",  "ingreso",    "acreedora","Resultado",   3, True),
    ("4.2.02",  "Otros Ingresos No Operacionales",   "ingreso",    "acreedora","Resultado",   3, True),
    ("4.2.03",  "Ingresos por Intereses",            "ingreso",    "acreedora","Resultado",   3, True),
    # ── COSTOS ──
    ("5",       "COSTOS",                            "costo",      "deudora", "Resultado",   1, False),
    ("5.1",     "Costo de Producción Agrícola",      "costo",      "deudora", "Resultado",   2, False),
    ("5.1.01",  "Mano de Obra Directa",              "costo",      "deudora", "Resultado",   3, True),
    ("5.1.02",  "Insumos Agrícolas (Consumo)",       "costo",      "deudora", "Resultado",   3, True),
    ("5.1.03",  "Depreciación Equipo Productivo",    "costo",      "deudora", "Resultado",   3, True),
    ("5.1.04",  "Riego y Energía",                   "costo",      "deudora", "Resultado",   3, True),
    ("5.1.05",  "Transporte de Cosecha",             "costo",      "deudora", "Resultado",   3, True),
    ("5.2",     "Costos Indirectos de Producción",   "costo",      "deudora", "Resultado",   2, False),
    ("5.2.01",  "Supervisión de Campo",              "costo",      "deudora", "Resultado",   3, True),
    ("5.2.02",  "Mantenimiento de Equipos",          "costo",      "deudora", "Resultado",   3, True),
    ("5.3",     "Ajustes al Costo",                  "costo",      "acreedora","Resultado",   2, False),
    ("5.3.01",  "Descuentos sobre Compras",          "costo",      "acreedora","Resultado",   3, True),
    ("5.3.02",  "Devoluciones sobre Compras",        "costo",      "acreedora","Resultado",   3, True),
    # ── GASTOS ──
    ("6",       "GASTOS",                            "gasto",      "deudora", "Resultado",   1, False),
    ("6.1",     "Gastos Administrativos",            "gasto",      "deudora", "Resultado",   2, False),
    ("6.1.01",  "Sueldos Administrativos",           "gasto",      "deudora", "Resultado",   3, True),
    ("6.1.02",  "Servicios Profesionales",           "gasto",      "deudora", "Resultado",   3, True),
    ("6.1.03",  "Servicios Básicos (Luz, Agua, Tel)","gasto",      "deudora", "Resultado",   3, True),
    ("6.1.04",  "Seguros",                           "gasto",      "deudora", "Resultado",   3, True),
    ("6.1.05",  "Depreciación Administrativa",       "gasto",      "deudora", "Resultado",   3, True),
    ("6.1.06",  "Gastos Legales y Permisos",         "gasto",      "deudora", "Resultado",   3, True),
    ("6.1.07",  "Contribución INFOTEP (1%)",         "gasto",      "deudora", "Resultado",   3, True),
    ("6.1.08",  "Gastos de Representación",          "gasto",      "deudora", "Resultado",   3, True),
    ("6.2",     "Gastos Financieros",                "gasto",      "deudora", "Resultado",   2, False),
    ("6.2.01",  "Intereses Bancarios",               "gasto",      "deudora", "Resultado",   3, True),
    ("6.2.02",  "Comisiones Bancarias",              "gasto",      "deudora", "Resultado",   3, True),
    ("6.2.03",  "Pérdida por Diferencia Cambiaria",  "gasto",      "deudora", "Resultado",   3, True),
    # ── IMPUESTOS SOBRE LA RENTA ──
    ("6.3",     "Gastos de Impuestos",               "gasto",      "deudora", "Resultado",   2, False),
    ("6.3.01",  "Gasto ISR Corriente",               "gasto",      "deudora", "Resultado",   3, True),
    ("6.3.02",  "Gasto ISR Diferido",                "gasto",      "deudora", "Resultado",   3, True),
]
for c in cuentas_data:
    if not db.query(models.CuentaContable).filter_by(codigo=c[0]).first():
        db.add(models.CuentaContable(
            codigo=c[0], nombre=c[1], tipo=c[2], naturaleza=c[3],
            grupo=c[4], nivel=c[5], acepta_movimientos=c[6]
        ))
db.flush()

# Asignar cuenta_padre_id por jerarquía de código
all_cuentas = {c.codigo: c for c in db.query(models.CuentaContable).all()}
for codigo, cuenta in all_cuentas.items():
    parts = codigo.rsplit(".", 1)
    if len(parts) == 2:
        padre = all_cuentas.get(parts[0])
        if padre and cuenta.cuenta_padre_id != padre.id:
            cuenta.cuenta_padre_id = padre.id

# Reglas de contabilización
reglas_data = [
    ("compra",       "factura_proveedor",  "1.1.03.01", "2.1.01.01", "Compra insumos: Db Inventario, Cr CxP Proveedores"),
    ("compra",       "itbis_compra",       "1.1.02.03", "2.1.01.01", "ITBIS en compras: Db Crédito Fiscal, Cr CxP"),
    ("venta",        "factura_cliente",     "1.1.02.01", "4.1.01",    "Venta aguacate: Db CxC Clientes, Cr Ingreso Venta"),
    ("venta",        "itbis_venta",        "1.1.02.01", "2.1.02.01", "ITBIS en ventas: Db CxC, Cr ITBIS por Pagar"),
    ("venta",        "costo_venta",        "5.1.02",    "1.1.03.03", "Costo venta: Db Costo Insumos, Cr Inventario Terminado"),
    ("pago",         "pago_proveedor",     "2.1.01.01", "1.1.01.03", "Pago proveedor: Db CxP, Cr Banco"),
    ("cobro",        "cobro_cliente",       "1.1.01.03", "1.1.02.01", "Cobro cliente: Db Banco, Cr CxC"),
    ("nomina",       "salario_jornada",     "5.1.01",    "2.1.01.02", "Nómina MO directa: Db Costo MO, Cr Nóminas por Pagar"),
    ("nomina",       "pago_nomina",         "2.1.01.02", "1.1.01.03", "Pago nómina: Db Nóminas por Pagar, Cr Banco"),
    ("nomina",       "tss_empleador",       "5.1.01",    "2.1.03.01", "TSS empleador: Db Costo MO, Cr TSS Empleador x Pagar"),
    ("consumo_ot",   "salida_insumo",       "5.1.02",    "1.1.03.01", "Consumo OT: Db Costo Insumos, Cr Inventario Insumos"),
    ("depreciacion", "dep_mensual",         "5.1.03",    "1.2.02.01", "Depreciación: Db Costo Dep, Cr Dep Acumulada"),
]
for r in reglas_data:
    if not db.query(models.ReglaContabilizacion).filter_by(evento=r[0], concepto=r[1]).first():
        cd = all_cuentas.get(r[2])
        ch = all_cuentas.get(r[3])
        if cd and ch:
            db.add(models.ReglaContabilizacion(
                evento=r[0], concepto=r[1],
                cuenta_debe_id=cd.id, cuenta_haber_id=ch.id,
                descripcion=r[4]
            ))

# Configuración Empresa
if not db.query(models.ConfiguracionEmpresa).first():
    db.add(models.ConfiguracionEmpresa(
        razon_social="CORVUS Agrícola, SRL",
        nombre_comercial="Finca CORVUS",
        rnc="000-000000-0",
        moneda_funcional="DOP",
        regimen_fiscal="RSI",
    ))

# Secuencias contables
for tipo in ('AC', 'CXP', 'PAG', 'CXC', 'COB', 'NC', 'ND', 'CLI'):
    if not db.query(models.Sequence).filter_by(tipo=tipo).first():
        db.add(models.Sequence(tipo=tipo, ultimo_numero=0))

db.commit()
db.close()
print("Base de datos inicializada correctamente.")
print(f"  - 3 usuarios")
print(f"  - 17 campos")
print(f"  - 20 trabajadores")
print(f"  - 25 actividades")
print(f"  - 30 productos")
print()
print("Credenciales:")
print("  Admin:      admin@corvus.do      / Corvus2024!")
print("  Supervisor: supervisor@corvus.do  / Super2024!")
print("  Operador:   operador@corvus.do   / Oper2024!")
