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
    ("compra", "recepcion_por_facturar", "1.1.03.01", "2.1.01.04",
     "Recepción sin factura: Db Inventario, Cr Compras Recibidas por Facturar"),
    ("compra", "retencion_isr",     "2.1.01.01", "2.1.02.03", "Retención ISR a proveedor: Cr Retenciones ISR por Pagar"),
    ("compra", "retencion_itbis",   "2.1.01.01", "2.1.02.04", "Retención ITBIS a proveedor: Cr ITBIS Retenido por Pagar"),
    # ── Ventas ──
    ("venta", "factura_cliente", "1.1.02.01", "4.1.01",    "Venta: Db CxC Clientes, Cr Ingreso Venta"),
    ("venta", "itbis_venta",     "1.1.02.01", "2.1.02.01", "ITBIS en ventas: Db CxC, Cr ITBIS por Pagar"),
    ("venta", "costo_venta",     "5.1.02",    "1.1.03.03", "Costo de venta: Db Costo, Cr Inventario Terminado"),
    ("venta", "despacho_por_liquidar", "1.1.03.08", "1.1.03.03",
     "Despacho: Db Fruta Despachada por Liquidar, Cr Inventario de fruta"),
    # ── Tesorería ──
    ("pago",  "pago_proveedor", "2.1.01.01", "1.1.01.03", "Pago proveedor: Db CxP, Cr Banco"),
    ("cobro", "cobro_cliente",  "1.1.01.03", "1.1.02.01", "Cobro cliente: Db Banco, Cr CxC"),
    ("cobro", "diferencia_cambiaria", "6.2.03", "4.2.01",
     "Diferencia cambiaria: Db Pérdida (tasa bajó), Cr Ganancia (tasa subió)"),
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


def _asegurar_cuenta_con_regla(db, models, *, evento, concepto, padre, hermana, desde, nombre,
                               tipo, naturaleza, palabras, descripcion, lado, contraparte):
    """Crea, si faltan, una cuenta de trabajo y la regla que la usa. Aditivo e idempotente.

    Si ya hay una cuenta hija de `padre` cuyo nombre contiene todas las `palabras`, se reutiliza;
    si no, se crea con el primer código libre desde `padre.desde` (el catálogo de producción
    divergió y un código puede estar ocupado por otra cuenta). Se presenta en los estados
    financieros junto a su `hermana`. `lado` dice si la cuenta va al debe o al haber de la
    regla; `contraparte` es (evento, concepto, "debe"|"haber") de la regla de la que se toma
    la otra cuenta. Devuelve el código usado si creó la regla, o None si ya existía.
    """
    C, R = models.CuentaContable, models.ReglaContabilizacion
    if db.query(R).filter_by(evento=evento, concepto=concepto).first():
        return None
    cuenta = next((c for c in db.query(C).filter(C.codigo.like(f"{padre}.%")).all()
                   if all(p in (c.nombre or "").lower() for p in palabras)), None)
    if cuenta is None:
        usados = {c for (c,) in db.query(C.codigo).all()}
        codigo = next(f"{padre}.{n:02d}" for n in range(desde, 100) if f"{padre}.{n:02d}" not in usados)
        cta_padre = db.query(C).filter_by(codigo=padre).first()
        cta_hermana = db.query(C).filter_by(codigo=hermana).first()
        cuenta = C(codigo=codigo, nombre=nombre, tipo=tipo, naturaleza=naturaleza, grupo="Balance",
                   nivel=4, acepta_movimientos=True, cuenta_padre_id=cta_padre.id if cta_padre else None,
                   partida_id=cta_hermana.partida_id if cta_hermana else None, activo=True)
        db.add(cuenta)
        db.flush()
    ref = db.query(R).filter_by(evento=contraparte[0], concepto=contraparte[1]).first()
    otra = (getattr(ref, "cuenta_debe_id" if contraparte[2] == "debe" else "cuenta_haber_id") if ref else None) or cuenta.id
    db.add(R(evento=evento, concepto=concepto, activo=True, descripcion=descripcion,
             cuenta_debe_id=cuenta.id if lado == "debe" else otra,
             cuenta_haber_id=cuenta.id if lado == "haber" else otra))
    return cuenta.codigo


def asegurar_cuenta_puente(db, models):
    """Cuenta 'Compras Recibidas por Facturar': la recepción la acredita y la factura del
    proveedor la liquida, como en Dynamics 365."""
    return _asegurar_cuenta_con_regla(
        db, models, evento="compra", concepto="recepcion_por_facturar",
        padre="2.1.01", hermana="2.1.01.01", desde=4, nombre="Compras Recibidas por Facturar",
        tipo="pasivo", naturaleza="acreedora", palabras=("factur", "recib"),
        descripcion="Recepción sin factura: Db Inventario, Cr Compras Recibidas por Facturar",
        lado="haber", contraparte=("compra", "factura_proveedor", "debe"))


def asegurar_cuenta_despacho(db, models):
    """Cuenta 'Fruta Despachada por Liquidar': el despacho la debita al sacar la fruta del
    inventario y la liquidación del cliente la acredita al reconocer el costo de venta."""
    return _asegurar_cuenta_con_regla(
        db, models, evento="venta", concepto="despacho_por_liquidar",
        padre="1.1.03", hermana="1.1.03.03", desde=8, nombre="Fruta Despachada por Liquidar",
        tipo="activo", naturaleza="deudora", palabras=("despach", "liquid"),
        descripcion="Despacho: Db Fruta Despachada por Liquidar, Cr Inventario de fruta",
        lado="debe", contraparte=("venta", "costo_venta", "haber"))


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
