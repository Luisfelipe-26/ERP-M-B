"""Reglas de contabilización estándar (finca de aguacates, RD).

Fuente única de verdad de las reglas evento→cuentas: la usa seed.py (carga
inicial) y el endpoint POST /contabilidad/reglas/seed-default para poblar
producción sin re-ejecutar el seed completo.

El motor _get_regla_cuentas(db, evento, concepto) las resuelve en runtime.
Los módulos las consumen así:
  - contabilidad.py: compra, venta, pago, cobro, NOM
  - ordenes.py:      nomina/salario_jornada, consumo_ot/salida_insumo
  - inventario.py:   inventario/entrada, salida, ajuste
"""

# (evento, concepto, codigo_debe, codigo_haber, descripcion)
REGLAS_DATA = [
    # ── Compras ──
    ("compra", "factura_proveedor", "1.1.03.01", "2.1.01.01", "Compra insumos: Db Inventario, Cr CxP Proveedores"),
    ("compra", "itbis_compra",      "1.1.02.03", "2.1.01.01", "ITBIS en compras: Db Crédito Fiscal, Cr CxP"),
    # ── Ventas ──
    ("venta", "factura_cliente", "1.1.02.01", "4.1.01",    "Venta: Db CxC Clientes, Cr Ingreso Venta"),
    ("venta", "itbis_venta",     "1.1.02.01", "2.1.02.01", "ITBIS en ventas: Db CxC, Cr ITBIS por Pagar"),
    ("venta", "costo_venta",     "5.1.02",    "1.1.03.03", "Costo de venta: Db Costo, Cr Inventario Terminado"),
    # ── Tesorería ──
    ("pago",  "pago_proveedor", "2.1.01.01", "1.1.01.03", "Pago proveedor: Db CxP, Cr Banco"),
    ("cobro", "cobro_cliente",  "1.1.01.03", "1.1.02.01", "Cobro cliente: Db Banco, Cr CxC"),
    # ── Nómina por Orden de Trabajo (evento 'nomina', usado en ordenes.py) ──
    ("nomina", "salario_jornada", "5.1.01",    "2.1.01.02", "Nómina MO directa: Db Costo MO, Cr Nóminas por Pagar"),
    ("nomina", "pago_nomina",     "2.1.01.02", "1.1.01.03", "Pago nómina: Db Nóminas por Pagar, Cr Banco"),
    ("nomina", "tss_empleador",   "5.1.01",    "2.1.03.01", "TSS empleador: Db Costo MO, Cr TSS Empleador x Pagar"),
    # ── Nómina por Período (evento 'NOM', usado en contabilidad.py) ──
    ("NOM", "nomina",      "5.1.01",    "2.1.01.02", "Nómina período: Db Costo MO, Cr Nóminas por Pagar"),
    ("NOM", "deducciones", "2.1.01.02", "2.1.03.02", "Deducciones nómina (SFS+AFP): Cr TSS Empleado Retenido"),
    # ── Consumo en Orden de Trabajo ──
    ("consumo_ot", "salida_insumo", "5.1.02", "1.1.03.01", "Consumo OT: Db Costo Insumos, Cr Inventario Insumos"),
    # ── Inventario ──
    # NOTA: 'entrada' acredita CxP igual que compra/factura_proveedor; si ambos
    # flujos se disparan por la misma compra habría doble registro. Elegir uno.
    ("inventario", "entrada", "1.1.03.01", "2.1.01.01", "Entrada inventario (GR): Db Inventario, Cr CxP"),
    ("inventario", "salida",  "5.1.02",    "1.1.03.01", "Salida inventario (GI): Db Costo, Cr Inventario"),
    ("inventario", "ajuste",  "5.2.03",    "1.1.03.01", "Ajuste/merma inventario: Db Merma, Cr Inventario"),
    # ── Depreciación ──
    ("depreciacion", "dep_mensual", "5.1.03", "1.2.02.01", "Depreciación: Db Costo Dep, Cr Dep Acumulada"),
]


def sembrar_reglas(db, models):
    """Crea las reglas faltantes resolviendo códigos de cuenta a IDs.
    Idempotente: no duplica reglas (evento, concepto) ya existentes. Devuelve
    (creadas, faltantes), donde faltantes lista las reglas omitidas porque su
    cuenta débito o crédito no existe en el catálogo. No hace commit."""
    cuentas = {c.codigo: c.id for c in db.query(models.CuentaContable).all()}
    creadas = 0
    faltantes = []
    for evento, concepto, cod_debe, cod_haber, desc in REGLAS_DATA:
        if db.query(models.ReglaContabilizacion).filter_by(
                evento=evento, concepto=concepto).first():
            continue
        id_debe = cuentas.get(cod_debe)
        id_haber = cuentas.get(cod_haber)
        if not id_debe or not id_haber:
            falta = cod_debe if not id_debe else cod_haber
            faltantes.append(f"{evento}/{concepto} (cuenta {falta} no existe)")
            continue
        db.add(models.ReglaContabilizacion(
            evento=evento, concepto=concepto,
            cuenta_debe_id=id_debe, cuenta_haber_id=id_haber,
            descripcion=desc, activo=True))
        creadas += 1
    return creadas, faltantes
