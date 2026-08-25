"""Catálogo de cuentas contables estándar (finca de aguacates, RD).

Fuente única de verdad del plan de cuentas: la usa seed.py (carga inicial)
y el endpoint POST /contabilidad/cuentas/seed-catalogo para poblar la base
de datos de producción sin tener que re-ejecutar el seed completo.
"""

# (codigo, nombre, tipo, naturaleza, grupo, nivel, acepta_mov)
CUENTAS_DATA = [
    # ── ACTIVOS ──
    ("1",        "ACTIVOS",                           "activo",     "deudora",  "Balance",   1, False),
    ("1.1",      "ACTIVOS CORRIENTES",                "activo",     "deudora",  "Balance",   2, False),
    ("1.1.01",   "Efectivo y Equivalentes",           "activo",     "deudora",  "Balance",   3, False),
    ("1.1.01.01", "Caja General",                     "activo",     "deudora",  "Balance",   4, True),
    ("1.1.01.02", "Caja Chica",                       "activo",     "deudora",  "Balance",   4, True),
    ("1.1.01.03", "Banco Cuenta Corriente DOP",       "activo",     "deudora",  "Balance",   4, True),
    ("1.1.01.04", "Banco Cuenta Corriente USD",       "activo",     "deudora",  "Balance",   4, True),
    ("1.1.02",   "Cuentas por Cobrar",                "activo",     "deudora",  "Balance",   3, False),
    ("1.1.02.01", "CxC Clientes",                     "activo",     "deudora",  "Balance",   4, True),
    ("1.1.02.02", "CxC Empleados y Anticipos",        "activo",     "deudora",  "Balance",   4, True),
    ("1.1.02.03", "CxC ITBIS Pagado (Crédito Fiscal)", "activo",    "deudora",  "Balance",   4, True),
    ("1.1.02.04", "CxC Retenciones Recibidas",        "activo",     "deudora",  "Balance",   4, True),
    ("1.1.02.05", "Anticipo ISR",                     "activo",     "deudora",  "Balance",   4, True),
    ("1.1.02.06", "Provisión Cuentas Incobrables",    "activo",     "acreedora", "Balance",   4, True),
    ("1.1.03",   "Inventarios",                       "activo",     "deudora",  "Balance",   3, False),
    ("1.1.03.01", "Insumos Agrícolas",                "activo",     "deudora",  "Balance",   4, True),
    ("1.1.03.02", "Cosecha en Proceso (WIP)",         "activo",     "deudora",  "Balance",   4, True),
    ("1.1.03.03", "Cosecha Terminada",                "activo",     "deudora",  "Balance",   4, True),
    ("1.1.03.04", "Materiales de Empaque",            "activo",     "deudora",  "Balance",   4, True),
    ("1.1.03.05", "Combustibles y Lubricantes",       "activo",     "deudora",  "Balance",   4, True),
    ("1.1.03.06", "Repuestos y Herramientas",         "activo",     "deudora",  "Balance",   4, True),
    ("1.1.03.07", "Provisión Deterioro Inventario",   "activo",     "acreedora", "Balance",   4, True),
    ("1.2",      "ACTIVOS NO CORRIENTES",             "activo",     "deudora",  "Balance",   2, False),
    ("1.2.01",   "Propiedad, Planta y Equipo",        "activo",     "deudora",  "Balance",   3, False),
    ("1.2.01.01", "Terrenos",                         "activo",     "deudora",  "Balance",   4, True),
    ("1.2.01.02", "Plantas Portadoras (NIC 16)",      "activo",     "deudora",  "Balance",   4, True),
    ("1.2.01.03", "Edificaciones e Infraestructura",  "activo",     "deudora",  "Balance",   4, True),
    ("1.2.01.04", "Maquinaria y Equipo Agrícola",     "activo",     "deudora",  "Balance",   4, True),
    ("1.2.01.05", "Vehículos",                        "activo",     "deudora",  "Balance",   4, True),
    ("1.2.01.06", "Sistema de Riego",                 "activo",     "deudora",  "Balance",   4, True),
    ("1.2.01.07", "Equipo de Cómputo",                "activo",     "deudora",  "Balance",   4, True),
    ("1.2.01.08", "Mobiliario y Equipo de Oficina",   "activo",     "deudora",  "Balance",   4, True),
    ("1.2.01.09", "Construcciones en Proceso",        "activo",     "deudora",  "Balance",   4, True),
    ("1.2.03",   "Activos Biológicos (NIC 41)",        "activo",     "deudora",  "Balance",   3, False),
    ("1.2.03.01", "Plantaciones en Desarrollo",        "activo",     "deudora",  "Balance",   4, True),
    ("1.2.03.02", "Vivero y Plántulas",                "activo",     "deudora",  "Balance",   4, True),
    ("1.2.03.03", "Frutos en Crecimiento (Pre-Cosecha)", "activo",   "deudora",  "Balance",   4, True),
    ("1.2.03.04", "Ajuste Valor Razonable Act. Biológicos", "activo", "deudora", "Balance",   4, True),
    ("1.2.04",   "Amortización Acum. Act. Biológicos", "activo",     "acreedora", "Balance",  3, False),
    ("1.2.04.01", "Amort. Acum. Plantaciones en Desarrollo", "activo", "acreedora", "Balance", 4, True),
    ("1.2.02",   "Depreciación Acumulada",            "activo",     "acreedora", "Balance",  3, False),
    ("1.2.02.01", "Dep. Acum. Plantas Portadoras",    "activo",     "acreedora", "Balance",  4, True),
    ("1.2.02.02", "Dep. Acum. Edificaciones",         "activo",     "acreedora", "Balance",  4, True),
    ("1.2.02.03", "Dep. Acum. Maquinaria",            "activo",     "acreedora", "Balance",  4, True),
    ("1.2.02.04", "Dep. Acum. Vehículos",             "activo",     "acreedora", "Balance",  4, True),
    ("1.2.02.05", "Dep. Acum. Sistema de Riego",      "activo",     "acreedora", "Balance",  4, True),
    ("1.2.02.06", "Dep. Acum. Equipo de Cómputo",     "activo",     "acreedora", "Balance",  4, True),
    ("1.2.02.07", "Dep. Acum. Mobiliario y Equipo",   "activo",     "acreedora", "Balance",  4, True),
    ("1.2.05",   "Activos Intangibles",               "activo",     "deudora",  "Balance",   3, False),
    ("1.2.05.01", "Licencias y Software",             "activo",     "deudora",  "Balance",   4, True),
    ("1.2.05.02", "Derechos de Agua / Concesiones",   "activo",     "deudora",  "Balance",   4, True),
    ("1.2.06",   "Activo por Impuesto Diferido",      "activo",     "deudora",  "Balance",   3, False),
    ("1.2.06.01", "Activo por Impuesto Diferido",     "activo",     "deudora",  "Balance",   4, True),
    # ── PASIVOS ──
    ("2",        "PASIVOS",                           "pasivo",     "acreedora", "Balance",  1, False),
    ("2.1",      "PASIVOS CORRIENTES",                "pasivo",     "acreedora", "Balance",  2, False),
    ("2.1.01",   "Cuentas por Pagar",                 "pasivo",     "acreedora", "Balance",  3, False),
    ("2.1.01.01", "CxP Proveedores",                  "pasivo",     "acreedora", "Balance",  4, True),
    ("2.1.01.02", "CxP Nóminas por Pagar",            "pasivo",     "acreedora", "Balance",  4, True),
    ("2.1.01.03", "Acreedores Diversos / Otras CxP",  "pasivo",     "acreedora", "Balance",  4, True),
    ("2.1.02",   "Impuestos por Pagar",               "pasivo",     "acreedora", "Balance",  3, False),
    ("2.1.02.01", "ITBIS por Pagar",                  "pasivo",     "acreedora", "Balance",  4, True),
    ("2.1.02.02", "ISR por Pagar",                    "pasivo",     "acreedora", "Balance",  4, True),
    ("2.1.02.03", "Retenciones ISR por Pagar",        "pasivo",     "acreedora", "Balance",  4, True),
    ("2.1.02.04", "ITBIS Retenido a Terceros por Pagar", "pasivo",  "acreedora", "Balance",  4, True),
    ("2.1.02.05", "Impuesto sobre Activos por Pagar", "pasivo",     "acreedora", "Balance",  4, True),
    ("2.1.03",   "Obligaciones Laborales",            "pasivo",     "acreedora", "Balance",  3, False),
    ("2.1.03.01", "TSS Empleador por Pagar",          "pasivo",     "acreedora", "Balance",  4, True),
    ("2.1.03.02", "TSS Empleado Retenido",            "pasivo",     "acreedora", "Balance",  4, True),
    ("2.1.03.03", "Vacaciones por Pagar",             "pasivo",     "acreedora", "Balance",  4, True),
    ("2.1.03.04", "Prestaciones por Pagar",           "pasivo",     "acreedora", "Balance",  4, True),
    ("2.1.03.05", "INFOTEP por Pagar",                "pasivo",     "acreedora", "Balance",  4, True),
    ("2.1.03.06", "Regalía Pascual por Pagar (Salario 13)", "pasivo", "acreedora", "Balance", 4, True),
    ("2.1.03.07", "Participación de Beneficios por Pagar",   "pasivo", "acreedora", "Balance", 4, True),
    ("2.1.04",   "Provisiones",                       "pasivo",     "acreedora", "Balance",  3, False),
    ("2.1.04.01", "Provisión Prestaciones Laborales", "pasivo",     "acreedora", "Balance",  4, True),
    ("2.1.05",   "Deuda Financiera Corriente",        "pasivo",     "acreedora", "Balance",  3, False),
    ("2.1.05.01", "Porción Corriente Préstamos LP",   "pasivo",     "acreedora", "Balance",  4, True),
    ("2.1.05.02", "Préstamos Bancarios CP / Sobregiros", "pasivo",  "acreedora", "Balance",  4, True),
    # ── PASIVOS NO CORRIENTES ──
    ("2.2",       "PASIVOS NO CORRIENTES",            "pasivo",     "acreedora", "Balance",  2, False),
    ("2.2.01",   "Préstamos por Pagar LP",            "pasivo",     "acreedora", "Balance",  3, False),
    ("2.2.01.01", "Préstamos Bancarios Largo Plazo",  "pasivo",     "acreedora", "Balance",  4, True),
    ("2.2.02",   "Impuesto Diferido",                 "pasivo",     "acreedora", "Balance",  3, False),
    ("2.2.02.01", "Pasivo por Impuesto Diferido",     "pasivo",     "acreedora", "Balance",  4, True),
    # ── PATRIMONIO ──
    ("3",        "PATRIMONIO",                        "patrimonio", "acreedora", "Balance",  1, False),
    ("3.1",      "Capital Social",                    "patrimonio", "acreedora", "Balance",  2, True),
    ("3.2",      "Reservas",                          "patrimonio", "acreedora", "Balance",  2, True),
    ("3.3",      "Resultados Acumulados",             "patrimonio", "acreedora", "Balance",  2, True),
    ("3.4",      "Resultado del Ejercicio",           "patrimonio", "acreedora", "Balance",  2, True),
    ("3.5",      "Reserva Legal (5%)",                "patrimonio", "acreedora", "Balance",  2, True),
    ("3.6",      "Superávit por Revaluación (NIC 16)", "patrimonio", "acreedora", "Balance", 2, True),
    # ── INGRESOS ──
    ("4",        "INGRESOS",                          "ingreso",    "acreedora", "Resultado", 1, False),
    ("4.1",      "Ingresos Operacionales",            "ingreso",    "acreedora", "Resultado", 2, False),
    ("4.1.01",   "Venta de Aguacate Hass",            "ingreso",    "acreedora", "Resultado", 3, True),
    ("4.1.02",   "Venta de Subproductos",             "ingreso",    "acreedora", "Resultado", 3, True),
    ("4.1.03",   "Descuentos sobre Ventas",           "ingreso",    "deudora",  "Resultado", 3, True),
    ("4.1.04",   "Devoluciones sobre Ventas",         "ingreso",    "deudora",  "Resultado", 3, True),
    ("4.1.05",   "Venta de Aguacate Exportación",     "ingreso",    "acreedora", "Resultado", 3, True),
    ("4.2",      "Otros Ingresos",                    "ingreso",    "acreedora", "Resultado", 2, False),
    ("4.2.01",   "Ingreso por Diferencia Cambiaria",  "ingreso",    "acreedora", "Resultado", 3, True),
    ("4.2.02",   "Otros Ingresos No Operacionales",   "ingreso",    "acreedora", "Resultado", 3, True),
    ("4.2.03",   "Ingresos por Intereses",            "ingreso",    "acreedora", "Resultado", 3, True),
    ("4.2.04",   "Ganancia Valor Razonable Act. Biológicos (NIC 41)", "ingreso", "acreedora", "Resultado", 3, True),
    ("4.2.05",   "Ganancia en Venta de Activos Fijos", "ingreso",   "acreedora", "Resultado", 3, True),
    # ── COSTOS ──
    ("5",        "COSTOS",                            "costo",      "deudora",  "Resultado", 1, False),
    ("5.1",      "Costo de Producción Agrícola",      "costo",      "deudora",  "Resultado", 2, False),
    ("5.1.01",   "Mano de Obra Directa",              "costo",      "deudora",  "Resultado", 3, True),
    ("5.1.02",   "Insumos Agrícolas (Consumo)",       "costo",      "deudora",  "Resultado", 3, True),
    ("5.1.03",   "Depreciación Equipo Productivo",    "costo",      "deudora",  "Resultado", 3, True),
    ("5.1.04",   "Riego y Energía",                   "costo",      "deudora",  "Resultado", 3, True),
    ("5.1.05",   "Transporte de Cosecha",             "costo",      "deudora",  "Resultado", 3, True),
    ("5.1.06",   "Costo Transformación Biológica (NIC 41)", "costo", "deudora", "Resultado", 3, True),
    ("5.1.07",   "Pérdida Valor Razonable Act. Biológicos", "costo", "deudora", "Resultado", 3, True),
    ("5.1.08",   "Materiales de Empaque (Consumo)",   "costo",      "deudora",  "Resultado", 3, True),
    ("5.1.09",   "Combustibles y Lubricantes",        "costo",      "deudora",  "Resultado", 3, True),
    ("5.2",      "Costos Indirectos de Producción",   "costo",      "deudora",  "Resultado", 2, False),
    ("5.2.01",   "Supervisión de Campo",              "costo",      "deudora",  "Resultado", 3, True),
    ("5.2.02",   "Mantenimiento de Equipos",          "costo",      "deudora",  "Resultado", 3, True),
    ("5.3",      "Ajustes al Costo",                  "costo",      "acreedora", "Resultado", 2, False),
    ("5.3.01",   "Descuentos sobre Compras",          "costo",      "acreedora", "Resultado", 3, True),
    ("5.3.02",   "Devoluciones sobre Compras",        "costo",      "acreedora", "Resultado", 3, True),
    # ── GASTOS ──
    ("6",        "GASTOS",                            "gasto",      "deudora",  "Resultado", 1, False),
    ("6.1",      "Gastos Administrativos",            "gasto",      "deudora",  "Resultado", 2, False),
    ("6.1.01",   "Sueldos Administrativos",           "gasto",      "deudora",  "Resultado", 3, True),
    ("6.1.02",   "Servicios Profesionales",           "gasto",      "deudora",  "Resultado", 3, True),
    ("6.1.03",   "Servicios Básicos (Luz, Agua, Tel)", "gasto",     "deudora",  "Resultado", 3, True),
    ("6.1.04",   "Seguros",                           "gasto",      "deudora",  "Resultado", 3, True),
    ("6.1.05",   "Depreciación Administrativa",       "gasto",      "deudora",  "Resultado", 3, True),
    ("6.1.06",   "Gastos Legales y Permisos",         "gasto",      "deudora",  "Resultado", 3, True),
    ("6.1.07",   "Contribución INFOTEP (1%)",         "gasto",      "deudora",  "Resultado", 3, True),
    ("6.1.08",   "Gastos de Representación",          "gasto",      "deudora",  "Resultado", 3, True),
    ("6.1.09",   "Cuentas Incobrables (Gasto)",       "gasto",      "deudora",  "Resultado", 3, True),
    ("6.1.10",   "Mantenimiento y Reparaciones",      "gasto",      "deudora",  "Resultado", 3, True),
    ("6.1.11",   "Papelería y Comunicaciones",        "gasto",      "deudora",  "Resultado", 3, True),
    ("6.2",      "Gastos Financieros",                "gasto",      "deudora",  "Resultado", 2, False),
    ("6.2.01",   "Intereses Bancarios",               "gasto",      "deudora",  "Resultado", 3, True),
    ("6.2.02",   "Comisiones Bancarias",              "gasto",      "deudora",  "Resultado", 3, True),
    ("6.2.03",   "Pérdida por Diferencia Cambiaria",  "gasto",      "deudora",  "Resultado", 3, True),
    ("6.3",      "Gastos de Impuestos",               "gasto",      "deudora",  "Resultado", 2, False),
    ("6.3.01",   "Gasto ISR Corriente",               "gasto",      "deudora",  "Resultado", 3, True),
    ("6.3.02",   "Gasto ISR Diferido",                "gasto",      "deudora",  "Resultado", 3, True),
    ("6.4",      "Gastos de Comercialización",        "gasto",      "deudora",  "Resultado", 2, False),
    ("6.4.01",   "Comisiones sobre Ventas",           "gasto",      "deudora",  "Resultado", 3, True),
    ("6.4.02",   "Fletes y Distribución",             "gasto",      "deudora",  "Resultado", 3, True),
    ("6.4.03",   "Gastos de Exportación",             "gasto",      "deudora",  "Resultado", 3, True),
    ("6.4.04",   "Publicidad y Promoción",            "gasto",      "deudora",  "Resultado", 3, True),
]


def sembrar_catalogo(db, models):
    """Crea las cuentas faltantes y enlaza cuenta_padre_id por jerarquía de
    código. Idempotente: no duplica cuentas ya existentes. Devuelve el número
    de cuentas creadas."""
    creadas = 0
    for c in CUENTAS_DATA:
        if not db.query(models.CuentaContable).filter_by(codigo=c[0]).first():
            db.add(models.CuentaContable(
                codigo=c[0], nombre=c[1], tipo=c[2], naturaleza=c[3],
                grupo=c[4], nivel=c[5], acepta_movimientos=c[6]))
            creadas += 1
    db.flush()
    all_cuentas = {c.codigo: c for c in db.query(models.CuentaContable).all()}
    for codigo, cuenta in all_cuentas.items():
        parts = codigo.rsplit(".", 1)
        if len(parts) == 2:
            padre = all_cuentas.get(parts[0])
            if padre and cuenta.cuenta_padre_id != padre.id:
                cuenta.cuenta_padre_id = padre.id
    return creadas
