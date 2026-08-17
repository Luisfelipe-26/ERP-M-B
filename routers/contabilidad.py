"""
Módulo Contabilidad — Núcleo contable: plan de cuentas, periodos, asientos, libro mayor, reportes.
Contabilización automática integrada (CxP, Pagos, CxC, Cobros).
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func as sqlfunc, cast, Date as SqlDate, and_, extract
from database import get_db
from auth import get_current_user, require_admin
from datetime import date, datetime, timedelta
from calendar import monthrange
from decimal import Decimal
import models, schemas

router = APIRouter(prefix="/api/contabilidad", tags=["contabilidad"])

# ── Helpers ──

def get_next_seq(db: Session, tipo: str, prefix: str) -> str:
    seq = db.query(models.Sequence).filter(
        models.Sequence.tipo == tipo
    ).with_for_update().first()
    if not seq:
        seq = models.Sequence(tipo=tipo, ultimo_numero=0)
        db.add(seq)
        db.flush()
    seq.ultimo_numero += 1
    db.flush()
    return f"{prefix}-{seq.ultimo_numero:04d}"


def find_periodo(db: Session, fecha: date):
    return db.query(models.PeriodoContable).filter(
        models.PeriodoContable.fecha_inicio <= fecha,
        models.PeriodoContable.fecha_fin >= fecha
    ).first()


def _get_regla_cuentas(db: Session, evento: str, concepto: str):
    regla = db.query(models.ReglaContabilizacion).filter(
        models.ReglaContabilizacion.evento == evento,
        models.ReglaContabilizacion.concepto == concepto,
        models.ReglaContabilizacion.activo == True
    ).first()
    if not regla:
        return None
    return (regla.cuenta_debe_id, regla.cuenta_haber_id)


def _crear_asiento_auto(db: Session, fecha: date, origen: str, referencia_id: str,
                        descripcion: str, lineas_data: list, user_nombre: str):
    """Create and immediately post an automatic journal entry.
    lineas_data: list of dicts {cuenta_id, debe, haber, campo_id?, tercero_id?, descripcion_linea?}
    Returns the AsientoContable or None if periodo missing/closed.
    """
    periodo = find_periodo(db, fecha)
    if not periodo or periodo.estado == "cerrado":
        return None

    lineas_data = [l for l in lineas_data
                   if Decimal(str(l.get("debe") or 0)) > 0 or Decimal(str(l.get("haber") or 0)) > 0]
    if not lineas_data:
        return None

    total_debe = sum(Decimal(str(l.get("debe") or 0)) for l in lineas_data)
    total_haber = sum(Decimal(str(l.get("haber") or 0)) for l in lineas_data)

    numero = get_next_seq(db, "AC", "AC")
    asiento = models.AsientoContable(
        numero=numero, fecha=fecha, periodo_id=periodo.id,
        tipo="automatico", origen=origen, referencia_id=referencia_id,
        descripcion=descripcion, total_debe=total_debe, total_haber=total_haber,
        estado="contabilizado", creado_por="Sistema",
        contabilizado_por="Sistema", contabilizado_en=datetime.utcnow()
    )
    db.add(asiento)
    db.flush()

    for l in lineas_data:
        db.add(models.LineaAsiento(
            asiento_id=asiento.id, cuenta_id=l["cuenta_id"],
            debe=Decimal(str(l.get("debe") or 0)),
            haber=Decimal(str(l.get("haber") or 0)),
            campo_id=l.get("campo_id"), tercero_id=l.get("tercero_id"),
            descripcion_linea=l.get("descripcion_linea")
        ))

    _actualizar_saldos(db, asiento, 1)
    return asiento


# ══════════════════════════════════════════════════════════════════════════════
# PARTIDAS DE ESTADOS FINANCIEROS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/partidas")
def listar_partidas(estado: str = None, db: Session = Depends(get_db),
                    user=Depends(get_current_user)):
    q = db.query(models.PartidaEstadoFinanciero).filter(models.PartidaEstadoFinanciero.activo == True)
    if estado:
        q = q.filter(models.PartidaEstadoFinanciero.estado == estado)
    return [schemas.PartidaEstadoFinancieroOut.model_validate(p)
            for p in q.order_by(models.PartidaEstadoFinanciero.orden, models.PartidaEstadoFinanciero.id).all()]


@router.get("/partidas/arbol")
def arbol_partidas(estado: str, db: Session = Depends(get_db),
                   user=Depends(get_current_user)):
    """Returns hierarchical tree of partidas for a given estado (balance_general / estado_resultados)."""
    all_p = db.query(models.PartidaEstadoFinanciero).filter(
        models.PartidaEstadoFinanciero.activo == True,
        models.PartidaEstadoFinanciero.estado == estado
    ).order_by(models.PartidaEstadoFinanciero.orden).all()

    cuentas_por_partida = {}
    for c in db.query(models.CuentaContable).filter(
        models.CuentaContable.activo == True,
        models.CuentaContable.partida_id.isnot(None)).all():
        cuentas_por_partida.setdefault(c.partida_id, []).append({
            "id": c.id, "codigo": c.codigo, "nombre": c.nombre, "tipo": c.tipo
        })

    by_id = {p.id: p for p in all_p}

    def build(parent_id):
        children = [p for p in all_p if p.padre_id == parent_id]
        result = []
        for p in children:
            node = {
                "id": p.id, "nombre": p.nombre, "clasificacion": p.clasificacion,
                "orden": p.orden, "invertir_signo": p.invertir_signo, "es_grupo": p.es_grupo,
                "cuentas": sorted(cuentas_por_partida.get(p.id, []), key=lambda x: x["codigo"]),
                "hijos": build(p.id),
            }
            result.append(node)
        return result

    roots = build(None)

    todas_cuentas_ids = set()
    for clist in cuentas_por_partida.values():
        for c in clist:
            todas_cuentas_ids.add(c["id"])
    tipo_filter = ["activo", "pasivo", "patrimonio"] if estado == "balance_general" else ["ingreso", "costo", "gasto"]
    no_asignadas = [{"id": c.id, "codigo": c.codigo, "nombre": c.nombre, "tipo": c.tipo}
                    for c in db.query(models.CuentaContable).filter(
                        models.CuentaContable.activo == True,
                        models.CuentaContable.acepta_movimientos == True,
                        models.CuentaContable.tipo.in_(tipo_filter),
                        ~models.CuentaContable.id.in_(todas_cuentas_ids) if todas_cuentas_ids else True
                    ).order_by(models.CuentaContable.codigo).all()]

    return {"arbol": roots, "no_asignadas": no_asignadas}


@router.post("/partidas")
def crear_partida(data: schemas.PartidaEstadoFinancieroCreate, db: Session = Depends(get_db),
                  user=Depends(require_admin)):
    p = models.PartidaEstadoFinanciero(**data.model_dump())
    db.add(p)
    db.commit()
    db.refresh(p)
    return schemas.PartidaEstadoFinancieroOut.model_validate(p)


@router.put("/partidas/{id}")
def actualizar_partida(id: int, data: schemas.PartidaEstadoFinancieroCreate,
                       db: Session = Depends(get_db), user=Depends(require_admin)):
    p = db.query(models.PartidaEstadoFinanciero).get(id)
    if not p:
        raise HTTPException(404, "Partida no encontrada")
    for k, v in data.model_dump().items():
        setattr(p, k, v)
    db.commit()
    db.refresh(p)
    return schemas.PartidaEstadoFinancieroOut.model_validate(p)


@router.delete("/partidas/{id}")
def desactivar_partida(id: int, db: Session = Depends(get_db), user=Depends(require_admin)):
    p = db.query(models.PartidaEstadoFinanciero).get(id)
    if not p:
        raise HTTPException(404, "Partida no encontrada")
    tiene_hijos = db.query(models.PartidaEstadoFinanciero).filter(
        models.PartidaEstadoFinanciero.padre_id == id,
        models.PartidaEstadoFinanciero.activo == True).first()
    tiene_cuentas = db.query(models.CuentaContable).filter(
        models.CuentaContable.partida_id == id, models.CuentaContable.activo == True).first()
    if tiene_hijos or tiene_cuentas:
        p.activo = False
        db.commit()
        return {"ok": True, "msg": "Partida desactivada (tiene hijos o cuentas)"}
    db.delete(p)
    db.commit()
    return {"ok": True, "msg": "Partida eliminada"}


# ══════════════════════════════════════════════════════════════════════════════
# PLAN DE CUENTAS
# ══════════════════════════════════════════════════════════════════════════════

def _cuenta_to_out(c):
    d = schemas.CuentaContableOut.model_validate(c)
    if c.partida:
        d.partida_nombre = c.partida.nombre
        d.partida_clasificacion = c.partida.clasificacion
    return d


@router.get("/cuentas")
def listar_cuentas(activo: bool = True, db: Session = Depends(get_db),
                   user=Depends(get_current_user)):
    q = db.query(models.CuentaContable)
    if activo:
        q = q.filter(models.CuentaContable.activo == True)
    cuentas = q.order_by(models.CuentaContable.codigo).all()
    return [_cuenta_to_out(c) for c in cuentas]


@router.post("/cuentas")
def crear_cuenta(data: schemas.CuentaContableCreate, db: Session = Depends(get_db),
                 user=Depends(require_admin)):
    if db.query(models.CuentaContable).filter(models.CuentaContable.codigo == data.codigo).first():
        raise HTTPException(400, f"Código {data.codigo} ya existe")
    cuenta = models.CuentaContable(**data.model_dump())
    db.add(cuenta)
    db.commit()
    db.refresh(cuenta)
    return _cuenta_to_out(cuenta)


@router.put("/cuentas/{codigo}")
def actualizar_cuenta(codigo: str, data: schemas.CuentaContableCreate,
                      db: Session = Depends(get_db), user=Depends(require_admin)):
    cuenta = db.query(models.CuentaContable).filter(models.CuentaContable.codigo == codigo).first()
    if not cuenta:
        raise HTTPException(404, "Cuenta no encontrada")
    for k, v in data.model_dump().items():
        setattr(cuenta, k, v)
    db.commit()
    db.refresh(cuenta)
    return _cuenta_to_out(cuenta)


@router.delete("/cuentas/{codigo}")
def desactivar_cuenta(codigo: str, db: Session = Depends(get_db), user=Depends(require_admin)):
    cuenta = db.query(models.CuentaContable).filter(models.CuentaContable.codigo == codigo).first()
    if not cuenta:
        raise HTTPException(404, "Cuenta no encontrada")
    tiene_movimientos = db.query(models.LineaAsiento).filter(
        models.LineaAsiento.cuenta_id == cuenta.id).first()
    if tiene_movimientos:
        cuenta.activo = False
        db.commit()
        return {"ok": True, "msg": "Cuenta desactivada (tiene movimientos)"}
    db.delete(cuenta)
    db.commit()
    return {"ok": True, "msg": "Cuenta eliminada"}


# ══════════════════════════════════════════════════════════════════════════════
# PERIODOS CONTABLES
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/periodos")
def listar_periodos(anio: int = None, db: Session = Depends(get_db),
                    user=Depends(get_current_user)):
    q = db.query(models.PeriodoContable)
    if anio:
        q = q.filter(models.PeriodoContable.anio == anio)
    return [schemas.PeriodoContableOut.model_validate(p)
            for p in q.order_by(models.PeriodoContable.anio.desc(), models.PeriodoContable.mes).all()]


@router.post("/periodos/generar")
def generar_periodos(anio: int, db: Session = Depends(get_db), user=Depends(require_admin)):
    creados = 0
    meses = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
             "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]
    for mes in range(1, 13):
        existe = db.query(models.PeriodoContable).filter(
            models.PeriodoContable.anio == anio, models.PeriodoContable.mes == mes).first()
        if existe:
            continue
        _, last_day = monthrange(anio, mes)
        p = models.PeriodoContable(
            anio=anio, mes=mes, nombre=f"{meses[mes-1]} {anio}",
            fecha_inicio=date(anio, mes, 1), fecha_fin=date(anio, mes, last_day)
        )
        db.add(p)
        creados += 1
    db.commit()
    return {"ok": True, "creados": creados}


@router.post("/periodos/{id}/cerrar")
def cerrar_periodo(id: int, db: Session = Depends(get_db), user=Depends(require_admin)):
    p = db.query(models.PeriodoContable).get(id)
    if not p:
        raise HTTPException(404, "Periodo no encontrado")
    if p.estado == "cerrado":
        raise HTTPException(400, "Periodo ya está cerrado")
    borradores = db.query(models.AsientoContable).filter(
        models.AsientoContable.periodo_id == id,
        models.AsientoContable.estado == "borrador"
    ).count()
    if borradores > 0:
        raise HTTPException(400, f"Hay {borradores} asiento(s) en borrador. Contabilícelos o anúlelos primero.")
    periodo_ids = [p.id]
    lq = db.query(
        models.LineaAsiento.cuenta_id,
        sqlfunc.sum(models.LineaAsiento.debe).label("td"),
        sqlfunc.sum(models.LineaAsiento.haber).label("th"),
    ).join(models.AsientoContable).filter(
        models.AsientoContable.estado == "contabilizado",
        models.AsientoContable.periodo_id.in_(periodo_ids)
    ).group_by(models.LineaAsiento.cuenta_id)

    cuentas_map = {c.id: c for c in db.query(models.CuentaContable).all()}
    resultado = Decimal(0)
    for row in lq.all():
        c = cuentas_map.get(row.cuenta_id)
        if c and c.tipo in ("ingreso", "costo", "gasto"):
            neto = Decimal(str(row.td or 0)) - Decimal(str(row.th or 0))
            resultado -= neto

    asiento_cierre = None
    if resultado != 0:
        cta_resultado = db.query(models.CuentaContable).filter(
            models.CuentaContable.codigo == "3.4").first()
        if cta_resultado:
            lineas_cierre = []
            for row in lq.all():
                c = cuentas_map.get(row.cuenta_id)
                if not c or c.tipo not in ("ingreso", "costo", "gasto"):
                    continue
                debe = Decimal(str(row.td or 0))
                haber = Decimal(str(row.th or 0))
                if debe == 0 and haber == 0:
                    continue
                lineas_cierre.append({
                    "cuenta_id": row.cuenta_id,
                    "debe": haber, "haber": debe,
                    "descripcion_linea": f"Cierre {c.codigo} {c.nombre}"
                })
            total_d = sum(Decimal(str(l["debe"])) for l in lineas_cierre)
            total_h = sum(Decimal(str(l["haber"])) for l in lineas_cierre)
            diff = total_d - total_h
            if diff > 0:
                lineas_cierre.append({"cuenta_id": cta_resultado.id,
                    "debe": 0, "haber": diff,
                    "descripcion_linea": "Resultado del ejercicio (utilidad)"})
            elif diff < 0:
                lineas_cierre.append({"cuenta_id": cta_resultado.id,
                    "debe": abs(diff), "haber": 0,
                    "descripcion_linea": "Resultado del ejercicio (pérdida)"})

            if lineas_cierre:
                asiento_cierre = _crear_asiento_auto(
                    db, p.fecha_fin, "CIE", str(p.id),
                    f"Cierre contable — {p.nombre}",
                    lineas_cierre, user.nombre
                )

    p.estado = "cerrado"
    p.cerrado_por = user.nombre
    p.cerrado_en = datetime.utcnow()
    db.commit()
    return {"ok": True, "msg": f"Periodo {p.nombre} cerrado",
            "asiento_cierre": asiento_cierre.numero if asiento_cierre else None}


@router.post("/periodos/{id}/reabrir")
def reabrir_periodo(id: int, db: Session = Depends(get_db), user=Depends(require_admin)):
    p = db.query(models.PeriodoContable).get(id)
    if not p:
        raise HTTPException(404, "Periodo no encontrado")
    p.estado = "abierto"
    p.cerrado_por = None
    p.cerrado_en = None
    db.commit()
    return {"ok": True, "msg": f"Periodo {p.nombre} reabierto"}


# ══════════════════════════════════════════════════════════════════════════════
# ASIENTOS CONTABLES
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/asientos")
def listar_asientos(
    estado: str = None, origen: str = None,
    desde: date = None, hasta: date = None,
    skip: int = 0, limit: int = 50,
    db: Session = Depends(get_db), user=Depends(get_current_user)
):
    q = db.query(models.AsientoContable)
    if estado:
        q = q.filter(models.AsientoContable.estado == estado)
    if origen:
        q = q.filter(models.AsientoContable.origen == origen)
    if desde:
        q = q.filter(models.AsientoContable.fecha >= desde)
    if hasta:
        q = q.filter(models.AsientoContable.fecha <= hasta)
    total = q.count()
    asientos = q.order_by(models.AsientoContable.fecha.desc(), models.AsientoContable.id.desc()
                          ).offset(skip).limit(limit).all()
    result = []
    for a in asientos:
        out = schemas.AsientoContableOut.model_validate(a)
        out.lineas = []
        for l in a.lineas:
            lo = schemas.LineaAsientoOut.model_validate(l)
            lo.cuenta_codigo = l.cuenta.codigo if l.cuenta else None
            lo.cuenta_nombre = l.cuenta.nombre if l.cuenta else None
            out.lineas.append(lo)
        result.append(out)
    return {"total": total, "items": result}


@router.get("/asientos/{numero}")
def ver_asiento(numero: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    a = db.query(models.AsientoContable).filter(models.AsientoContable.numero == numero).first()
    if not a:
        raise HTTPException(404, "Asiento no encontrado")
    out = schemas.AsientoContableOut.model_validate(a)
    out.lineas = []
    for l in a.lineas:
        lo = schemas.LineaAsientoOut.model_validate(l)
        lo.cuenta_codigo = l.cuenta.codigo if l.cuenta else None
        lo.cuenta_nombre = l.cuenta.nombre if l.cuenta else None
        out.lineas.append(lo)
    return out


@router.post("/asientos")
def crear_asiento(data: schemas.AsientoContableCreate, db: Session = Depends(get_db),
                  user=Depends(get_current_user)):
    if user.rol not in ("admin", "supervisor"):
        raise HTTPException(403, "Solo admin o supervisor pueden crear asientos manuales")
    if not data.lineas or len(data.lineas) < 2:
        raise HTTPException(400, "Un asiento requiere al menos 2 líneas")

    for l in data.lineas:
        if (l.debe or 0) > 0 and (l.haber or 0) > 0:
            raise HTTPException(400, "Una línea no puede tener debe y haber simultáneamente")

    total_debe = sum(l.debe for l in data.lineas)
    total_haber = sum(l.haber for l in data.lineas)
    if round(total_debe, 2) != round(total_haber, 2):
        raise HTTPException(400, f"Partida doble no cuadra: Debe={total_debe:.2f} ≠ Haber={total_haber:.2f}")

    for l in data.lineas:
        cuenta = db.query(models.CuentaContable).get(l.cuenta_id)
        if not cuenta:
            raise HTTPException(400, f"Cuenta ID {l.cuenta_id} no existe")
        if not cuenta.acepta_movimientos:
            raise HTTPException(400, f"Cuenta {cuenta.codigo} no acepta movimientos (es grupo)")

    periodo = find_periodo(db, data.fecha)
    if not periodo:
        raise HTTPException(400, f"No existe periodo contable para la fecha {data.fecha}")
    if periodo.estado == "cerrado":
        raise HTTPException(400, f"El periodo {periodo.nombre} está cerrado")

    numero = get_next_seq(db, "AC", "AC")
    asiento = models.AsientoContable(
        numero=numero, fecha=data.fecha, periodo_id=periodo.id,
        tipo=data.tipo, origen=data.origen, referencia_id=data.referencia_id,
        descripcion=data.descripcion, total_debe=total_debe, total_haber=total_haber,
        estado="borrador", creado_por=user.nombre
    )
    db.add(asiento)
    db.flush()

    for l in data.lineas:
        linea = models.LineaAsiento(
            asiento_id=asiento.id, cuenta_id=l.cuenta_id,
            debe=l.debe, haber=l.haber, campo_id=l.campo_id or None,
            tercero_id=l.tercero_id, descripcion_linea=l.descripcion_linea
        )
        db.add(linea)
    db.commit()
    db.refresh(asiento)
    return {"ok": True, "numero": numero, "id": asiento.id}


@router.post("/asientos/{numero}/contabilizar")
def contabilizar_asiento(numero: str, db: Session = Depends(get_db), user=Depends(require_admin)):
    a = db.query(models.AsientoContable).filter(models.AsientoContable.numero == numero).first()
    if not a:
        raise HTTPException(404, "Asiento no encontrado")
    if a.estado != "borrador":
        raise HTTPException(400, f"Solo se puede contabilizar un asiento en borrador (estado actual: {a.estado})")

    periodo = db.query(models.PeriodoContable).get(a.periodo_id)
    if periodo and periodo.estado == "cerrado":
        raise HTTPException(400, f"El periodo {periodo.nombre} está cerrado")

    a.estado = "contabilizado"
    a.contabilizado_por = user.nombre
    a.contabilizado_en = datetime.utcnow()

    _actualizar_saldos(db, a, 1)
    db.commit()
    return {"ok": True, "msg": f"Asiento {numero} contabilizado"}


@router.post("/asientos/{numero}/anular")
def anular_asiento(numero: str, motivo: str = "Anulación manual",
                   db: Session = Depends(get_db), user=Depends(require_admin)):
    a = db.query(models.AsientoContable).filter(models.AsientoContable.numero == numero).first()
    if not a:
        raise HTTPException(404, "Asiento no encontrado")
    if a.estado == "anulado":
        raise HTTPException(400, "El asiento ya está anulado")

    if a.estado == "contabilizado":
        num_rev = get_next_seq(db, "AC", "AC")
        reverso = models.AsientoContable(
            numero=num_rev, fecha=date.today(),
            periodo_id=a.periodo_id, tipo="cierre", origen=a.origen,
            referencia_id=a.referencia_id,
            descripcion=f"Reverso de {numero}: {motivo}",
            total_debe=a.total_debe, total_haber=a.total_haber,
            estado="contabilizado", creado_por=user.nombre,
            contabilizado_por=user.nombre, contabilizado_en=datetime.utcnow()
        )
        db.add(reverso)
        db.flush()
        for l in a.lineas:
            db.add(models.LineaAsiento(
                asiento_id=reverso.id, cuenta_id=l.cuenta_id,
                debe=l.haber, haber=l.debe,
                campo_id=l.campo_id, tercero_id=l.tercero_id,
                descripcion_linea=f"Reverso: {l.descripcion_linea or ''}"
            ))
        _actualizar_saldos(db, reverso, 1)
        a.asiento_reverso_id = reverso.id

    a.estado = "anulado"
    a.anulado_por = user.nombre
    db.commit()
    return {"ok": True, "msg": f"Asiento {numero} anulado"}


def _actualizar_saldos(db: Session, asiento, factor: int):
    """Actualiza saldos_mensuales. factor=1 para contabilizar, -1 para reversar."""
    for l in asiento.lineas:
        sm = db.query(models.SaldoMensual).filter(
            models.SaldoMensual.cuenta_id == l.cuenta_id,
            models.SaldoMensual.periodo_id == asiento.periodo_id
        ).first()
        if not sm:
            sm = models.SaldoMensual(
                cuenta_id=l.cuenta_id, periodo_id=asiento.periodo_id,
                saldo_deudor=0, saldo_acreedor=0, saldo_neto=0
            )
            db.add(sm)
            db.flush()
        debe_val = Decimal(str(l.debe or 0)) * factor
        haber_val = Decimal(str(l.haber or 0)) * factor
        sm.saldo_deudor = Decimal(str(sm.saldo_deudor or 0)) + debe_val
        sm.saldo_acreedor = Decimal(str(sm.saldo_acreedor or 0)) + haber_val
        sm.saldo_neto = Decimal(str(sm.saldo_deudor or 0)) - Decimal(str(sm.saldo_acreedor or 0))


# ══════════════════════════════════════════════════════════════════════════════
# LIBRO MAYOR Y REPORTES
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/libro-mayor")
def libro_mayor(
    cuenta_id: int = None, codigo: str = None,
    desde: date = None, hasta: date = None,
    skip: int = 0, limit: int = 100,
    db: Session = Depends(get_db), user=Depends(get_current_user)
):
    if user.rol == "operador":
        raise HTTPException(403, "Acceso denegado")
    q = db.query(models.LineaAsiento).join(models.AsientoContable).filter(
        models.AsientoContable.estado == "contabilizado"
    )
    target_cuenta_id = cuenta_id
    if not target_cuenta_id and codigo:
        cuenta = db.query(models.CuentaContable).filter(models.CuentaContable.codigo == codigo).first()
        if cuenta:
            target_cuenta_id = cuenta.id
    if target_cuenta_id:
        q = q.filter(models.LineaAsiento.cuenta_id == target_cuenta_id)
    if desde:
        q = q.filter(models.AsientoContable.fecha >= desde)
    if hasta:
        q = q.filter(models.AsientoContable.fecha <= hasta)

    total = q.count()
    order = (models.AsientoContable.fecha, models.AsientoContable.id)

    saldo_previo = Decimal(0)
    if skip > 0:
        prev_lines = q.order_by(*order).limit(skip).all()
        for pl in prev_lines:
            saldo_previo += Decimal(str(pl.debe or 0)) - Decimal(str(pl.haber or 0))

    lineas = q.order_by(*order).offset(skip).limit(limit).all()
    result = []
    saldo_acum = saldo_previo
    for l in lineas:
        saldo_acum += Decimal(str(l.debe or 0)) - Decimal(str(l.haber or 0))
        result.append({
            "fecha": l.asiento.fecha,
            "asiento_numero": l.asiento.numero,
            "descripcion": l.descripcion_linea or l.asiento.descripcion,
            "debe": float(l.debe or 0),
            "haber": float(l.haber or 0),
            "saldo": float(saldo_acum),
            "campo_id": l.campo_id,
            "cuenta_codigo": l.cuenta.codigo,
            "cuenta_nombre": l.cuenta.nombre,
        })
    return {"total": total, "items": result}


@router.get("/balance-comprobacion")
def balance_comprobacion(periodo_id: int = None, anio: int = None, mes: int = None,
                         db: Session = Depends(get_db), user=Depends(get_current_user)):
    if user.rol == "operador":
        raise HTTPException(403, "Acceso denegado")

    if periodo_id:
        periodos = [db.query(models.PeriodoContable).get(periodo_id)]
    elif anio and mes:
        periodos = [db.query(models.PeriodoContable).filter(
            models.PeriodoContable.anio == anio, models.PeriodoContable.mes == mes).first()]
    elif anio:
        periodos = db.query(models.PeriodoContable).filter(
            models.PeriodoContable.anio == anio).all()
    else:
        periodos = db.query(models.PeriodoContable).all()

    periodo_ids = [p.id for p in periodos if p]
    if not periodo_ids:
        return []

    saldos = db.query(models.SaldoMensual).filter(
        models.SaldoMensual.periodo_id.in_(periodo_ids)).all()

    acum = {}
    for s in saldos:
        cid = s.cuenta_id
        if cid not in acum:
            acum[cid] = {"deudor": Decimal(0), "acreedor": Decimal(0)}
        acum[cid]["deudor"] += Decimal(str(s.saldo_deudor or 0))
        acum[cid]["acreedor"] += Decimal(str(s.saldo_acreedor or 0))

    cuentas = db.query(models.CuentaContable).filter(
        models.CuentaContable.activo == True
    ).order_by(models.CuentaContable.codigo).all()

    result = []
    for c in cuentas:
        d = acum.get(c.id, {"deudor": Decimal(0), "acreedor": Decimal(0)})
        saldo = d["deudor"] - d["acreedor"]
        if d["deudor"] == 0 and d["acreedor"] == 0:
            continue
        result.append({
            "codigo": c.codigo, "nombre": c.nombre, "tipo": c.tipo,
            "naturaleza": c.naturaleza,
            "sumas_debe": float(round(d["deudor"], 2)),
            "sumas_haber": float(round(d["acreedor"], 2)),
            "saldo_deudor": float(round(saldo, 2)) if saldo > 0 else 0,
            "saldo_acreedor": float(round(abs(saldo), 2)) if saldo < 0 else 0,
        })
    return result


@router.get("/balance-general")
def balance_general(anio: int = None, mes: int = None,
                    db: Session = Depends(get_db), user=Depends(get_current_user)):
    if user.rol == "operador":
        raise HTTPException(403, "Acceso denegado")
    if not anio:
        anio = date.today().year
    if not mes:
        mes = date.today().month

    periodo_ids = [p.id for p in db.query(models.PeriodoContable).filter(
        and_(models.PeriodoContable.anio <= anio,
             ~and_(models.PeriodoContable.anio == anio, models.PeriodoContable.mes > mes))
    ).all()]

    saldos = db.query(models.SaldoMensual).filter(
        models.SaldoMensual.periodo_id.in_(periodo_ids)).all() if periodo_ids else []

    acum = {}
    for s in saldos:
        cid = s.cuenta_id
        if cid not in acum:
            acum[cid] = Decimal(0)
        acum[cid] += Decimal(str(s.saldo_deudor or 0)) - Decimal(str(s.saldo_acreedor or 0))

    cuentas = {c.id: c for c in db.query(models.CuentaContable).filter(
        models.CuentaContable.activo == True).all()}

    all_partidas = db.query(models.PartidaEstadoFinanciero).filter(
        models.PartidaEstadoFinanciero.activo == True,
        models.PartidaEstadoFinanciero.estado == "balance_general"
    ).order_by(models.PartidaEstadoFinanciero.orden).all()

    cuentas_por_partida = {}
    for c in cuentas.values():
        if c.partida_id:
            cuentas_por_partida.setdefault(c.partida_id, []).append(c)

    total_a, total_p, total_pat = Decimal(0), Decimal(0), Decimal(0)
    resultado_ejercicio = Decimal(0)
    asignadas_ids = set()

    for cid, saldo in acum.items():
        c = cuentas.get(cid)
        if not c:
            continue
        if c.tipo in ("ingreso", "costo", "gasto"):
            resultado_ejercicio -= saldo
        elif c.tipo == "activo":
            total_a += saldo
        elif c.tipo == "pasivo":
            total_p += abs(saldo)
        elif c.tipo == "patrimonio":
            total_pat += abs(saldo)

    def walk_tree(parent_id, depth=0):
        children = [p for p in all_partidas if p.padre_id == parent_id]
        nodes = []
        for p in children:
            ctas_items = []
            for c in cuentas_por_partida.get(p.id, []):
                saldo_raw = acum.get(c.id, Decimal(0))
                if saldo_raw == 0:
                    continue
                display = float(round(-saldo_raw if p.invertir_signo else saldo_raw, 2))
                ctas_items.append({"codigo": c.codigo, "nombre": c.nombre, "saldo": abs(display)})
                asignadas_ids.add(c.id)
            sub_nodes = walk_tree(p.id, depth + 1)
            sub_total = sum(n["subtotal"] for n in sub_nodes) + sum(x["saldo"] for x in ctas_items)
            if sub_total != 0 or ctas_items or sub_nodes:
                nodes.append({
                    "partida": p.nombre, "clasificacion": p.clasificacion,
                    "es_grupo": p.es_grupo, "profundidad": depth,
                    "cuentas": sorted(ctas_items, key=lambda x: x["codigo"]),
                    "hijos": sub_nodes, "subtotal": round(sub_total, 2)
                })
        return nodes

    activos_tree = walk_tree(None)
    activos_tree_filtered = {"activos": [], "pasivos": [], "patrimonio": []}
    for node in activos_tree:
        cls = node["clasificacion"]
        if cls in ("activo_corriente", "activo_no_corriente"):
            activos_tree_filtered["activos"].append(node)
        elif cls in ("pasivo_corriente", "pasivo_no_corriente"):
            activos_tree_filtered["pasivos"].append(node)
        elif cls == "patrimonio":
            activos_tree_filtered["patrimonio"].append(node)

    no_asignadas = {"activos": [], "pasivos": [], "patrimonio": []}
    for cid, saldo in acum.items():
        c = cuentas.get(cid)
        if not c or cid in asignadas_ids or c.tipo in ("ingreso", "costo", "gasto"):
            continue
        entry = {"codigo": c.codigo, "nombre": c.nombre, "saldo": float(round(abs(saldo), 2))}
        if c.tipo == "activo":
            no_asignadas["activos"].append(entry)
        elif c.tipo == "pasivo":
            no_asignadas["pasivos"].append(entry)
        elif c.tipo == "patrimonio":
            no_asignadas["patrimonio"].append(entry)

    for key in no_asignadas:
        if no_asignadas[key]:
            activos_tree_filtered[key].append({
                "partida": "Cuentas Sin Asignar", "clasificacion": "sin_asignar",
                "es_grupo": False, "profundidad": 0,
                "cuentas": sorted(no_asignadas[key], key=lambda x: x["codigo"]),
                "hijos": [], "subtotal": sum(x["saldo"] for x in no_asignadas[key])
            })

    if resultado_ejercicio != 0:
        total_pat += abs(resultado_ejercicio)
        activos_tree_filtered["patrimonio"].append({
            "partida": "Resultado del Ejercicio", "clasificacion": "patrimonio",
            "es_grupo": False, "profundidad": 0,
            "cuentas": [{"codigo": "—", "nombre": "Resultado del Ejercicio (calculado)",
                         "saldo": float(round(abs(resultado_ejercicio), 2))}],
            "hijos": [], "subtotal": float(round(abs(resultado_ejercicio), 2))
        })

    return {
        "periodo": f"{mes:02d}/{anio}",
        "activos": activos_tree_filtered["activos"],
        "total_activos": float(round(total_a, 2)),
        "pasivos": activos_tree_filtered["pasivos"],
        "total_pasivos": float(round(total_p, 2)),
        "patrimonio": activos_tree_filtered["patrimonio"],
        "total_patrimonio": float(round(total_pat, 2)),
        "cuadra": float(round(total_a - total_p - total_pat, 2)) == 0,
        "no_asignadas_count": sum(len(v) for v in no_asignadas.values())
    }


@router.get("/estado-resultados")
def estado_resultados(anio: int = None, mes: int = None, campo_id: str = None,
                      db: Session = Depends(get_db), user=Depends(get_current_user)):
    if user.rol == "operador":
        raise HTTPException(403, "Acceso denegado")
    if not anio:
        anio = date.today().year

    q = db.query(models.PeriodoContable).filter(models.PeriodoContable.anio == anio)
    if mes:
        q = q.filter(models.PeriodoContable.mes == mes)
    periodo_ids = [p.id for p in q.all()]
    if not periodo_ids:
        return {"ingresos": [], "costos": [], "gastos": [], "utilidad_neta": 0}

    lq = db.query(
        models.LineaAsiento.cuenta_id,
        sqlfunc.sum(models.LineaAsiento.debe).label("total_debe"),
        sqlfunc.sum(models.LineaAsiento.haber).label("total_haber"),
    ).join(models.AsientoContable).filter(
        models.AsientoContable.estado == "contabilizado",
        models.AsientoContable.periodo_id.in_(periodo_ids)
    )
    if campo_id:
        lq = lq.filter(models.LineaAsiento.campo_id == campo_id)
    lq = lq.group_by(models.LineaAsiento.cuenta_id)

    acum = {}
    for row in lq.all():
        acum[row.cuenta_id] = float(row.total_debe or 0) - float(row.total_haber or 0)

    cuentas = {c.id: c for c in db.query(models.CuentaContable).all()}

    all_partidas = db.query(models.PartidaEstadoFinanciero).filter(
        models.PartidaEstadoFinanciero.activo == True,
        models.PartidaEstadoFinanciero.estado == "estado_resultados"
    ).order_by(models.PartidaEstadoFinanciero.orden).all()

    cuentas_por_partida = {}
    for c in cuentas.values():
        if c.partida_id:
            cuentas_por_partida.setdefault(c.partida_id, []).append(c)

    total_ing, total_cos, total_gas = 0.0, 0.0, 0.0
    asignadas_ids = set()

    for cid, neto in acum.items():
        c = cuentas.get(cid)
        if not c or c.tipo not in ("ingreso", "costo", "gasto"):
            continue
        if c.tipo == "ingreso":
            total_ing += abs(neto)
        elif c.tipo == "costo":
            total_cos += abs(neto)
        elif c.tipo == "gasto":
            total_gas += abs(neto)

    def walk_tree(parent_id, depth=0):
        children = [p for p in all_partidas if p.padre_id == parent_id]
        nodes = []
        for p in children:
            ctas_items = []
            for c in cuentas_por_partida.get(p.id, []):
                neto = acum.get(c.id, 0)
                if neto == 0:
                    continue
                display = -neto if p.invertir_signo else neto
                ctas_items.append({"codigo": c.codigo, "nombre": c.nombre, "monto": round(abs(display), 2)})
                asignadas_ids.add(c.id)
            sub_nodes = walk_tree(p.id, depth + 1)
            sub_total = sum(n["subtotal"] for n in sub_nodes) + sum(x["monto"] for x in ctas_items)
            if sub_total != 0 or ctas_items or sub_nodes:
                nodes.append({
                    "partida": p.nombre, "clasificacion": p.clasificacion,
                    "es_grupo": p.es_grupo, "profundidad": depth,
                    "cuentas": sorted(ctas_items, key=lambda x: x["codigo"]),
                    "hijos": sub_nodes, "subtotal": round(sub_total, 2)
                })
        return nodes

    tree = walk_tree(None)
    result_sections = {"ingresos": [], "costos": [], "gastos": []}
    for node in tree:
        cls = node["clasificacion"]
        if cls in result_sections:
            result_sections[cls].append(node)

    no_asignadas = {"ingresos": [], "costos": [], "gastos": []}
    for cid, neto in acum.items():
        c = cuentas.get(cid)
        if not c or cid in asignadas_ids or c.tipo not in ("ingreso", "costo", "gasto"):
            continue
        entry = {"codigo": c.codigo, "nombre": c.nombre, "monto": round(abs(neto), 2)}
        if c.tipo == "ingreso":
            no_asignadas["ingresos"].append(entry)
        elif c.tipo == "costo":
            no_asignadas["costos"].append(entry)
        elif c.tipo == "gasto":
            no_asignadas["gastos"].append(entry)

    for key in no_asignadas:
        if no_asignadas[key]:
            result_sections[key].append({
                "partida": "Cuentas Sin Asignar", "clasificacion": "sin_asignar",
                "es_grupo": False, "profundidad": 0,
                "cuentas": sorted(no_asignadas[key], key=lambda x: x["codigo"]),
                "hijos": [], "subtotal": sum(x["monto"] for x in no_asignadas[key])
            })

    return {
        "periodo": f"{mes or 'Anual'}/{anio}",
        "campo_id": campo_id,
        "ingresos": result_sections["ingresos"],
        "total_ingresos": round(total_ing, 2),
        "costos": result_sections["costos"],
        "total_costos": round(total_cos, 2),
        "utilidad_bruta": round(total_ing - total_cos, 2),
        "gastos": result_sections["gastos"],
        "total_gastos": round(total_gas, 2),
        "utilidad_neta": round(total_ing - total_cos - total_gas, 2),
        "no_asignadas_count": sum(len(v) for v in no_asignadas.values())
    }


# ══════════════════════════════════════════════════════════════════════════════
# REGLAS DE CONTABILIZACIÓN
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/reglas")
def listar_reglas(db: Session = Depends(get_db), user=Depends(get_current_user)):
    reglas = db.query(models.ReglaContabilizacion).filter(
        models.ReglaContabilizacion.activo == True
    ).order_by(models.ReglaContabilizacion.evento).all()
    result = []
    for r in reglas:
        data = schemas.ReglaContabilizacionOut.model_validate(r)
        cd = db.query(models.CuentaContable).filter(models.CuentaContable.id == r.cuenta_debe_id).first()
        ch = db.query(models.CuentaContable).filter(models.CuentaContable.id == r.cuenta_haber_id).first()
        if cd:
            data.cuenta_debe_codigo = cd.codigo
            data.cuenta_debe_nombre = cd.nombre
        if ch:
            data.cuenta_haber_codigo = ch.codigo
            data.cuenta_haber_nombre = ch.nombre
        result.append(data)
    return result


# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURACIÓN EMPRESA
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/empresa")
def ver_empresa(db: Session = Depends(get_db), user=Depends(get_current_user)):
    e = db.query(models.ConfiguracionEmpresa).first()
    if not e:
        return None
    return schemas.ConfiguracionEmpresaOut.model_validate(e)


@router.put("/empresa")
def actualizar_empresa(data: schemas.ConfiguracionEmpresaUpdate,
                       db: Session = Depends(get_db), user=Depends(require_admin)):
    e = db.query(models.ConfiguracionEmpresa).first()
    if not e:
        e = models.ConfiguracionEmpresa()
        db.add(e)
    for k, v in data.model_dump(exclude_unset=True).items():
        setattr(e, k, v)
    db.commit()
    db.refresh(e)
    return schemas.ConfiguracionEmpresaOut.model_validate(e)


# ══════════════════════════════════════════════════════════════════════════════
# CUENTAS POR PAGAR (CxP)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/cxp")
def listar_cxp(estado: str = None, skip: int = 0, limit: int = 100,
               db: Session = Depends(get_db), user=Depends(get_current_user)):
    if user.rol == "operador":
        raise HTTPException(403, "Acceso denegado")
    q = db.query(models.CuentaPorPagar)
    if estado:
        q = q.filter(models.CuentaPorPagar.estado == estado)
    total = q.count()
    items = q.order_by(models.CuentaPorPagar.fecha_factura.desc()).offset(skip).limit(limit).all()
    result = []
    for c in items:
        out = schemas.CuentaPorPagarOut.model_validate(c)
        prov = db.query(models.Proveedor).get(c.proveedor_id) if c.proveedor_id else None
        out.proveedor_nombre = prov.nombre if prov else None
        result.append(out)
    return {"total": total, "items": result}


@router.post("/cxp")
def crear_cxp(data: schemas.CuentaPorPagarCreate, db: Session = Depends(get_db),
              user=Depends(get_current_user)):
    if user.rol == "operador":
        raise HTTPException(403, "Acceso denegado")

    prov = db.query(models.Proveedor).get(data.proveedor_id)
    if not prov:
        raise HTTPException(400, f"Proveedor ID {data.proveedor_id} no existe")

    subtotal = Decimal(str(data.subtotal or 0))
    itbis = Decimal(str(data.itbis or 0))
    retencion = Decimal(str(data.retencion_isr or 0))
    total = subtotal + itbis - retencion

    numero = get_next_seq(db, "CXP", "CXP")
    cxp = models.CuentaPorPagar(
        numero=numero,
        proveedor_id=data.proveedor_id,
        oc_id=data.oc_id,
        tipo_ncf=data.tipo_ncf,
        ncf=data.ncf,
        num_factura_proveedor=data.num_factura_proveedor,
        fecha_factura=data.fecha_factura,
        fecha_vencimiento=data.fecha_vencimiento,
        subtotal=subtotal,
        itbis=itbis,
        retencion_isr=retencion,
        total=total,
        saldo_pendiente=total,
        notas=data.notas,
    )
    db.add(cxp)
    db.flush()

    lineas = []
    r_factura = _get_regla_cuentas(db, "compra", "factura_proveedor")
    if r_factura and subtotal > 0:
        lineas.append({"cuenta_id": r_factura[0], "debe": subtotal, "haber": 0,
                        "tercero_id": str(data.proveedor_id),
                        "descripcion_linea": f"Compra {prov.nombre}"})
        lineas.append({"cuenta_id": r_factura[1], "debe": 0, "haber": subtotal,
                        "tercero_id": str(data.proveedor_id),
                        "descripcion_linea": f"CxP {prov.nombre}"})
    r_itbis = _get_regla_cuentas(db, "compra", "itbis_compra")
    if r_itbis and itbis > 0:
        lineas.append({"cuenta_id": r_itbis[0], "debe": itbis, "haber": 0,
                        "descripcion_linea": "ITBIS crédito fiscal"})
        lineas.append({"cuenta_id": r_itbis[1], "debe": 0, "haber": itbis,
                        "tercero_id": str(data.proveedor_id),
                        "descripcion_linea": f"CxP ITBIS {prov.nombre}"})
    if retencion > 0:
        cta_ret = db.query(models.CuentaContable).filter(
            models.CuentaContable.codigo == "2.1.02.03").first()
        if cta_ret and r_factura:
            lineas.append({"cuenta_id": r_factura[1], "debe": retencion, "haber": 0,
                            "tercero_id": str(data.proveedor_id),
                            "descripcion_linea": f"Retención ISR {prov.nombre}"})
            lineas.append({"cuenta_id": cta_ret.id, "debe": 0, "haber": retencion,
                            "descripcion_linea": "Retención ISR por pagar"})

    asiento = None
    if lineas:
        asiento = _crear_asiento_auto(
            db, data.fecha_factura, "CXP", numero,
            f"Factura proveedor {prov.nombre} — {numero}",
            lineas, user.nombre
        )
        if asiento:
            cxp.asiento_id = asiento.id

    db.commit()
    db.refresh(cxp)
    return {"ok": True, "numero": numero, "id": cxp.id,
            "asiento": asiento.numero if asiento else None}


# ══════════════════════════════════════════════════════════════════════════════
# PAGOS
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/pagos")
def registrar_pago(data: schemas.PagoCreate, db: Session = Depends(get_db),
                   user=Depends(get_current_user)):
    if user.rol == "operador":
        raise HTTPException(403, "Acceso denegado")
    cxp = db.query(models.CuentaPorPagar).get(data.cxp_id)
    if not cxp:
        raise HTTPException(404, "CxP no encontrada")
    if cxp.estado == "pagada":
        raise HTTPException(400, "Esta CxP ya está pagada")

    monto = Decimal(str(data.monto))
    saldo = Decimal(str(cxp.saldo_pendiente or 0))
    if monto > saldo:
        raise HTTPException(400, f"Monto ({monto}) excede saldo pendiente ({saldo})")

    numero = get_next_seq(db, "PAG", "PAG")
    pago = models.Pago(
        numero=numero, cxp_id=data.cxp_id, fecha=data.fecha,
        monto=monto, metodo_pago=data.metodo_pago,
        referencia_bancaria=data.referencia_bancaria,
        cuenta_bancaria_id=data.cuenta_bancaria_id,
    )
    db.add(pago)

    nuevo_saldo = saldo - monto
    if nuevo_saldo <= Decimal("0.005"):
        cxp.saldo_pendiente = 0
        cxp.estado = "pagada"
    else:
        cxp.saldo_pendiente = nuevo_saldo
        cxp.estado = "parcial"

    r_pago = _get_regla_cuentas(db, "pago", "pago_proveedor")
    cuenta_banco_contable_id = None
    if data.cuenta_bancaria_id:
        cb = db.query(models.CuentaBancaria).get(data.cuenta_bancaria_id)
        if cb:
            cb.saldo_segun_libro = Decimal(str(cb.saldo_segun_libro or 0)) - monto
            cuenta_banco_contable_id = cb.cuenta_contable_id

    asiento = None
    if r_pago and monto > 0:
        cta_haber = cuenta_banco_contable_id or r_pago[1]
        prov = db.query(models.Proveedor).get(cxp.proveedor_id) if cxp.proveedor_id else None
        prov_nombre = prov.nombre if prov else "Proveedor"
        asiento = _crear_asiento_auto(
            db, data.fecha, "PAG", numero,
            f"Pago a {prov_nombre} — {numero}",
            [
                {"cuenta_id": r_pago[0], "debe": monto, "haber": 0,
                 "tercero_id": str(cxp.proveedor_id),
                 "descripcion_linea": f"Pago CxP {cxp.numero}"},
                {"cuenta_id": cta_haber, "debe": 0, "haber": monto,
                 "descripcion_linea": f"Salida banco — {data.metodo_pago or 'transferencia'}"},
            ],
            user.nombre
        )
        if asiento:
            pago.asiento_id = asiento.id

    db.commit()
    db.refresh(pago)
    return {"ok": True, "numero": numero, "id": pago.id,
            "saldo_restante": float(cxp.saldo_pendiente),
            "asiento": asiento.numero if asiento else None}


@router.get("/pagos")
def listar_pagos(cxp_id: int = None, skip: int = 0, limit: int = 50,
                 db: Session = Depends(get_db), user=Depends(get_current_user)):
    if user.rol == "operador":
        raise HTTPException(403, "Acceso denegado")
    q = db.query(models.Pago)
    if cxp_id:
        q = q.filter(models.Pago.cxp_id == cxp_id)
    items = q.order_by(models.Pago.fecha.desc()).offset(skip).limit(limit).all()
    return [schemas.PagoOut.model_validate(p) for p in items]


# ══════════════════════════════════════════════════════════════════════════════
# CUENTAS POR COBRAR (CxC)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/cxc")
def listar_cxc(estado: str = None, skip: int = 0, limit: int = 100,
               db: Session = Depends(get_db), user=Depends(get_current_user)):
    if user.rol == "operador":
        raise HTTPException(403, "Acceso denegado")
    q = db.query(models.CuentaPorCobrar)
    if estado:
        q = q.filter(models.CuentaPorCobrar.estado == estado)
    total = q.count()
    items = q.order_by(models.CuentaPorCobrar.fecha.desc()).offset(skip).limit(limit).all()
    result = []
    for c in items:
        out = schemas.CuentaPorCobrarOut.model_validate(c)
        cli = db.query(models.Cliente).get(c.cliente_id) if c.cliente_id else None
        out.cliente_nombre = cli.nombre if cli else None
        result.append(out)
    return {"total": total, "items": result}


@router.post("/cxc")
def crear_cxc(data: schemas.CuentaPorCobrarCreate, db: Session = Depends(get_db),
              user=Depends(get_current_user)):
    if user.rol == "operador":
        raise HTTPException(403, "Acceso denegado")

    cli = db.query(models.Cliente).get(data.cliente_id)
    if not cli:
        raise HTTPException(400, f"Cliente ID {data.cliente_id} no existe")

    subtotal = Decimal(str(data.subtotal or 0))
    itbis = Decimal(str(data.itbis or 0))
    total = subtotal + itbis

    numero = get_next_seq(db, "CXC", "CXC")
    tasa = data.tasa_cambio or 1
    total_dop = total * Decimal(str(tasa)) if data.moneda == "USD" else total
    cxc = models.CuentaPorCobrar(
        numero=numero,
        cliente_id=data.cliente_id,
        tipo_ncf=data.tipo_ncf,
        fecha=data.fecha,
        fecha_vencimiento=data.fecha_vencimiento,
        moneda=data.moneda,
        tasa_cambio=tasa,
        subtotal=subtotal,
        itbis=itbis,
        total=total,
        total_dop=total_dop,
        saldo_pendiente=total,
        campo_id=data.campo_id,
        temporada=data.temporada,
        kg_vendidos=data.kg_vendidos,
        precio_por_kg=data.precio_por_kg,
    )
    db.add(cxc)
    db.flush()

    lineas = []
    r_venta = _get_regla_cuentas(db, "venta", "factura_cliente")
    if r_venta and subtotal > 0:
        lineas.append({"cuenta_id": r_venta[0], "debe": subtotal, "haber": 0,
                        "tercero_id": str(data.cliente_id), "campo_id": data.campo_id,
                        "descripcion_linea": f"CxC {cli.nombre}"})
        lineas.append({"cuenta_id": r_venta[1], "debe": 0, "haber": subtotal,
                        "campo_id": data.campo_id,
                        "descripcion_linea": f"Venta {cli.nombre}"})
    r_itbis = _get_regla_cuentas(db, "venta", "itbis_venta")
    if r_itbis and itbis > 0:
        lineas.append({"cuenta_id": r_itbis[0], "debe": itbis, "haber": 0,
                        "tercero_id": str(data.cliente_id),
                        "descripcion_linea": f"CxC ITBIS {cli.nombre}"})
        lineas.append({"cuenta_id": r_itbis[1], "debe": 0, "haber": itbis,
                        "descripcion_linea": "ITBIS por pagar"})

    asiento = None
    if lineas:
        asiento = _crear_asiento_auto(
            db, data.fecha, "VTA", numero,
            f"Venta a {cli.nombre} — {numero}",
            lineas, user.nombre
        )
        if asiento:
            cxc.asiento_id = asiento.id

    db.commit()
    db.refresh(cxc)
    return {"ok": True, "numero": numero, "id": cxc.id,
            "asiento": asiento.numero if asiento else None}


# ══════════════════════════════════════════════════════════════════════════════
# COBROS
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/cobros")
def registrar_cobro(data: schemas.CobroCreate, db: Session = Depends(get_db),
                    user=Depends(get_current_user)):
    if user.rol == "operador":
        raise HTTPException(403, "Acceso denegado")
    cxc = db.query(models.CuentaPorCobrar).get(data.cxc_id)
    if not cxc:
        raise HTTPException(404, "CxC no encontrada")
    if cxc.estado == "cobrada":
        raise HTTPException(400, "Esta CxC ya está cobrada")

    monto = Decimal(str(data.monto))
    saldo = Decimal(str(cxc.saldo_pendiente or 0))
    if monto > saldo:
        raise HTTPException(400, f"Monto ({monto}) excede saldo pendiente ({saldo})")

    numero = get_next_seq(db, "COB", "COB")
    cobro = models.Cobro(
        numero=numero, cxc_id=data.cxc_id, fecha=data.fecha,
        monto=monto, metodo_pago=data.metodo_pago,
        referencia_bancaria=data.referencia_bancaria,
        cuenta_bancaria_id=data.cuenta_bancaria_id,
    )
    db.add(cobro)

    nuevo_saldo = saldo - monto
    if nuevo_saldo <= Decimal("0.005"):
        cxc.saldo_pendiente = 0
        cxc.estado = "cobrada"
    else:
        cxc.saldo_pendiente = nuevo_saldo
        cxc.estado = "parcial"

    r_cobro = _get_regla_cuentas(db, "cobro", "cobro_cliente")
    cuenta_banco_contable_id = None
    if data.cuenta_bancaria_id:
        cb = db.query(models.CuentaBancaria).get(data.cuenta_bancaria_id)
        if cb:
            cb.saldo_segun_libro = Decimal(str(cb.saldo_segun_libro or 0)) + monto
            cuenta_banco_contable_id = cb.cuenta_contable_id

    asiento = None
    if r_cobro and monto > 0:
        cta_debe = cuenta_banco_contable_id or r_cobro[0]
        cli = db.query(models.Cliente).get(cxc.cliente_id) if cxc.cliente_id else None
        cli_nombre = cli.nombre if cli else "Cliente"
        asiento = _crear_asiento_auto(
            db, data.fecha, "COB", numero,
            f"Cobro de {cli_nombre} — {numero}",
            [
                {"cuenta_id": cta_debe, "debe": monto, "haber": 0,
                 "descripcion_linea": f"Entrada banco — {data.metodo_pago or 'transferencia'}"},
                {"cuenta_id": r_cobro[1], "debe": 0, "haber": monto,
                 "tercero_id": str(cxc.cliente_id),
                 "descripcion_linea": f"Cobro CxC {cxc.numero}"},
            ],
            user.nombre
        )
        if asiento:
            cobro.asiento_id = asiento.id

    db.commit()
    db.refresh(cobro)
    return {"ok": True, "numero": numero, "id": cobro.id,
            "saldo_restante": float(cxc.saldo_pendiente),
            "asiento": asiento.numero if asiento else None}


@router.get("/cobros")
def listar_cobros(cxc_id: int = None, skip: int = 0, limit: int = 50,
                  db: Session = Depends(get_db), user=Depends(get_current_user)):
    if user.rol == "operador":
        raise HTTPException(403, "Acceso denegado")
    q = db.query(models.Cobro)
    if cxc_id:
        q = q.filter(models.Cobro.cxc_id == cxc_id)
    items = q.order_by(models.Cobro.fecha.desc()).offset(skip).limit(limit).all()
    return [schemas.CobroOut.model_validate(c) for c in items]


# ══════════════════════════════════════════════════════════════════════════════
# REGLAS CRUD
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/reglas")
def crear_regla(data: schemas.ReglaContabilizacionCreate,
                db: Session = Depends(get_db), user=Depends(require_admin)):
    existing = db.query(models.ReglaContabilizacion).filter(
        models.ReglaContabilizacion.evento == data.evento,
        models.ReglaContabilizacion.concepto == data.concepto,
        models.ReglaContabilizacion.activo == True
    ).first()
    if existing:
        raise HTTPException(400, f"Ya existe regla activa para {data.evento}/{data.concepto}")
    for cid in [data.cuenta_debe_id, data.cuenta_haber_id]:
        if not db.query(models.CuentaContable).get(cid):
            raise HTTPException(400, f"Cuenta {cid} no existe")
    regla = models.ReglaContabilizacion(**data.model_dump(), activo=True)
    db.add(regla)
    db.commit()
    db.refresh(regla)
    return {"ok": True, "id": regla.id}


@router.put("/reglas/{id}")
def actualizar_regla(id: int, data: schemas.ReglaContabilizacionCreate,
                     db: Session = Depends(get_db), user=Depends(require_admin)):
    regla = db.query(models.ReglaContabilizacion).get(id)
    if not regla:
        raise HTTPException(404, "Regla no encontrada")
    for cid in [data.cuenta_debe_id, data.cuenta_haber_id]:
        if not db.query(models.CuentaContable).get(cid):
            raise HTTPException(400, f"Cuenta {cid} no existe")
    for k, v in data.model_dump().items():
        setattr(regla, k, v)
    db.commit()
    return {"ok": True}


@router.delete("/reglas/{id}")
def eliminar_regla(id: int, db: Session = Depends(get_db), user=Depends(require_admin)):
    regla = db.query(models.ReglaContabilizacion).get(id)
    if not regla:
        raise HTTPException(404, "Regla no encontrada")
    regla.activo = False
    db.commit()
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════════
# ACTIVOS FIJOS
# ══════════════════════════════════════════════════════════════════════════════

def _enrich_activo(a: models.ActivoFijo, db: Session) -> dict:
    d = schemas.ActivoFijoOut.model_validate(a).model_dump()
    costo = float(a.costo_adquisicion or 0)
    residual = float(a.valor_residual or 0)
    dep_acum = float(a.depreciacion_acumulada or 0)
    d["valor_en_libros"] = round(costo - dep_acum, 2)
    vida = a.vida_util_meses or 1
    d["dep_mensual_estimada"] = round((costo - residual) / vida, 2)
    if a.campo_id:
        campo = db.query(models.Campo).filter(models.Campo.id_campo == a.campo_id).first()
        d["campo_nombre"] = campo.nombre if campo else None
    return d


@router.get("/activos-fijos")
def listar_activos(categoria: str = None, campo_id: str = None,
                   db: Session = Depends(get_db), user=Depends(get_current_user)):
    if user.rol == "operador":
        raise HTTPException(403, "Acceso denegado")
    q = db.query(models.ActivoFijo).filter(models.ActivoFijo.activo == True)
    if categoria:
        q = q.filter(models.ActivoFijo.categoria == categoria)
    if campo_id:
        q = q.filter(models.ActivoFijo.campo_id == campo_id)
    return [_enrich_activo(a, db) for a in q.order_by(models.ActivoFijo.codigo).all()]


@router.get("/activos-fijos/{id}")
def ver_activo(id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if user.rol == "operador":
        raise HTTPException(403, "Acceso denegado")
    a = db.query(models.ActivoFijo).get(id)
    if not a:
        raise HTTPException(404, "Activo no encontrado")
    d = _enrich_activo(a, db)
    hist = db.query(models.DepreciacionHistorial).filter(
        models.DepreciacionHistorial.activo_id == id
    ).order_by(models.DepreciacionHistorial.id.desc()).all()
    d["historial"] = []
    for h in hist:
        hd = schemas.DepreciacionHistorialOut.model_validate(h).model_dump()
        p = db.query(models.PeriodoContable).get(h.periodo_id)
        hd["periodo_nombre"] = p.nombre if p else None
        d["historial"].append(hd)
    return d


@router.post("/activos-fijos")
def crear_activo(data: schemas.ActivoFijoCreate,
                 db: Session = Depends(get_db), user=Depends(require_admin)):
    if db.query(models.ActivoFijo).filter(models.ActivoFijo.codigo == data.codigo).first():
        raise HTTPException(400, f"Ya existe un activo con código {data.codigo}")
    activo = models.ActivoFijo(**data.model_dump(), depreciacion_acumulada=0, activo=True)
    db.add(activo)
    db.commit()
    db.refresh(activo)
    return _enrich_activo(activo, db)


@router.put("/activos-fijos/{id}")
def actualizar_activo(id: int, data: schemas.ActivoFijoCreate,
                      db: Session = Depends(get_db), user=Depends(require_admin)):
    activo = db.query(models.ActivoFijo).get(id)
    if not activo:
        raise HTTPException(404, "Activo no encontrado")
    dup = db.query(models.ActivoFijo).filter(
        models.ActivoFijo.codigo == data.codigo, models.ActivoFijo.id != id
    ).first()
    if dup:
        raise HTTPException(400, f"Ya existe otro activo con código {data.codigo}")
    for k, v in data.model_dump().items():
        setattr(activo, k, v)
    db.commit()
    db.refresh(activo)
    return _enrich_activo(activo, db)


@router.delete("/activos-fijos/{id}")
def eliminar_activo(id: int, db: Session = Depends(get_db), user=Depends(require_admin)):
    activo = db.query(models.ActivoFijo).get(id)
    if not activo:
        raise HTTPException(404, "Activo no encontrado")
    activo.activo = False
    db.commit()
    return {"ok": True}


@router.post("/activos-fijos/depreciar")
def correr_depreciacion(mes: int = Query(...), ano: int = Query(...),
                        db: Session = Depends(get_db), user=Depends(require_admin)):
    """Run monthly depreciation for all active fixed assets."""
    periodo = db.query(models.PeriodoContable).filter(
        models.PeriodoContable.mes == mes, models.PeriodoContable.anio == ano
    ).first()
    if not periodo:
        raise HTTPException(400, f"No existe periodo contable para {mes}/{ano}")
    if periodo.estado == "cerrado":
        raise HTTPException(400, "El periodo está cerrado")

    activos = db.query(models.ActivoFijo).filter(models.ActivoFijo.activo == True).all()
    depreciados = 0
    total_dep = Decimal("0")

    for a in activos:
        already = db.query(models.DepreciacionHistorial).filter(
            models.DepreciacionHistorial.activo_id == a.id,
            models.DepreciacionHistorial.periodo_id == periodo.id
        ).first()
        if already:
            continue

        costo = Decimal(str(a.costo_adquisicion or 0))
        residual = Decimal(str(a.valor_residual or 0))
        dep_acum = Decimal(str(a.depreciacion_acumulada or 0))
        base_dep = costo - residual
        if base_dep <= 0 or dep_acum >= base_dep:
            continue
        vida = a.vida_util_meses or 1
        dep_mensual = round(base_dep / vida, 2)
        remaining = base_dep - dep_acum
        if dep_mensual > remaining:
            dep_mensual = remaining

        a.depreciacion_acumulada = dep_acum + dep_mensual
        hist = models.DepreciacionHistorial(
            activo_id=a.id, periodo_id=periodo.id,
            monto=dep_mensual, depreciacion_acumulada_post=a.depreciacion_acumulada
        )
        db.add(hist)

        if a.cuenta_gasto_dep_id and a.cuenta_depreciacion_id:
            asiento = _crear_asiento_auto(
                db, periodo.fecha_fin, "DEP", f"AF-{a.codigo}",
                f"Depreciación {a.nombre} — {periodo.nombre or f'{mes}/{ano}'}",
                [
                    {"cuenta_id": a.cuenta_gasto_dep_id, "debe": float(dep_mensual), "haber": 0,
                     "campo_id": a.campo_id,
                     "descripcion_linea": f"Gasto depreciación {a.codigo}"},
                    {"cuenta_id": a.cuenta_depreciacion_id, "debe": 0, "haber": float(dep_mensual),
                     "descripcion_linea": f"Dep. acumulada {a.codigo}"},
                ],
                user.nombre
            )
            if asiento:
                hist.asiento_id = asiento.id

        depreciados += 1
        total_dep += dep_mensual

    db.commit()
    return {"ok": True, "depreciados": depreciados, "total": float(total_dep),
            "periodo": periodo.nombre or f"{mes}/{ano}"}


# ══════════════════════════════════════════════════════════════════════════════
# ANTIGÜEDAD CxP / CxC
# ══════════════════════════════════════════════════════════════════════════════

def _bucket(dias):
    if dias > 120: return "120+"
    if dias > 90: return "91-120"
    if dias > 60: return "61-90"
    if dias > 30: return "31-60"
    return "corriente"

BUCKET_ORDER = ["corriente", "31-60", "61-90", "91-120", "120+"]

@router.get("/antiguedad-cxp")
def antiguedad_cxp(
    proveedor_id: int = None,
    bucket_filter: str = None,
    search: str = None,
    agrupar: bool = False,
    db: Session = Depends(get_db), user=Depends(get_current_user),
):
    if user.rol == "operador":
        raise HTTPException(403, "Acceso denegado")
    hoy = date.today()
    q = db.query(models.CuentaPorPagar).filter(
        models.CuentaPorPagar.estado.notin_(["pagada", "anulada"])
    )
    if proveedor_id:
        q = q.filter(models.CuentaPorPagar.proveedor_id == proveedor_id)
    items = q.all()
    prov_ids = {cxp.proveedor_id for cxp in items}
    provs = {p.id: p for p in db.query(models.Proveedor).filter(models.Proveedor.id.in_(prov_ids)).all()} if prov_ids else {}

    result = []
    for cxp in items:
        prov = provs.get(cxp.proveedor_id)
        nombre_prov = prov.nombre if prov else "—"
        if search and search.lower() not in nombre_prov.lower() and search.lower() not in (cxp.numero or "").lower():
            continue
        dias = (hoy - cxp.fecha_factura).days if cxp.fecha_factura else 0
        b = _bucket(dias)
        if bucket_filter and b != bucket_filter:
            continue
        pagos_list = []
        for p in (cxp.pagos or []):
            pagos_list.append({"numero": p.numero, "fecha": str(p.fecha), "monto": float(p.monto or 0), "metodo": p.metodo_pago or "—", "referencia": p.referencia_bancaria or ""})
        result.append({
            "id": cxp.id, "numero": cxp.numero,
            "proveedor_id": cxp.proveedor_id,
            "proveedor": nombre_prov,
            "fecha_factura": str(cxp.fecha_factura) if cxp.fecha_factura else None,
            "fecha_vencimiento": str(cxp.fecha_vencimiento) if cxp.fecha_vencimiento else None,
            "total": float(cxp.total or 0),
            "saldo": float(cxp.saldo_pendiente or 0),
            "dias": dias, "bucket": b,
            "estado": cxp.estado,
            "pagos": pagos_list,
        })

    buckets = {k: 0.0 for k in BUCKET_ORDER}
    bucket_counts = {k: 0 for k in BUCKET_ORDER}
    for r in result:
        buckets[r["bucket"]] += r["saldo"]
        bucket_counts[r["bucket"]] += 1
    total = sum(buckets.values())
    total_vencido = total - buckets["corriente"]
    avg_dias = round(sum(r["dias"] for r in result) / len(result), 1) if result else 0

    grouped = {}
    if agrupar:
        for r in result:
            key = r["proveedor_id"]
            if key not in grouped:
                grouped[key] = {"nombre": r["proveedor"], "documentos": 0, "saldo_total": 0, "dias_max": 0, "bucket_peor": "corriente", "items": []}
            g = grouped[key]
            g["documentos"] += 1
            g["saldo_total"] += r["saldo"]
            if r["dias"] > g["dias_max"]:
                g["dias_max"] = r["dias"]
                g["bucket_peor"] = r["bucket"]
            g["items"].append(r)
        for g in grouped.values():
            g["saldo_total"] = round(g["saldo_total"], 2)

    return {
        "detalle": sorted(result, key=lambda x: -x["dias"]),
        "resumen": {k: round(v, 2) for k, v in buckets.items()},
        "resumen_count": bucket_counts,
        "total": round(total, 2),
        "total_vencido": round(total_vencido, 2),
        "promedio_dias": avg_dias,
        "num_documentos": len(result),
        "agrupado": list(sorted(grouped.values(), key=lambda x: -x["saldo_total"])) if agrupar else None,
    }


@router.get("/antiguedad-cxc")
def antiguedad_cxc(
    cliente_id: int = None,
    bucket_filter: str = None,
    search: str = None,
    agrupar: bool = False,
    db: Session = Depends(get_db), user=Depends(get_current_user),
):
    if user.rol == "operador":
        raise HTTPException(403, "Acceso denegado")
    hoy = date.today()
    q = db.query(models.CuentaPorCobrar).filter(
        models.CuentaPorCobrar.estado.notin_(["cobrada", "anulada"])
    )
    if cliente_id:
        q = q.filter(models.CuentaPorCobrar.cliente_id == cliente_id)
    items = q.all()
    cli_ids = {cxc.cliente_id for cxc in items if cxc.cliente_id}
    clis = {c.id: c for c in db.query(models.Cliente).filter(models.Cliente.id.in_(cli_ids)).all()} if cli_ids else {}

    result = []
    for cxc in items:
        cli = clis.get(cxc.cliente_id)
        nombre_cli = cli.nombre if cli else "—"
        if search and search.lower() not in nombre_cli.lower() and search.lower() not in (cxc.numero or "").lower():
            continue
        dias = (hoy - cxc.fecha).days if cxc.fecha else 0
        b = _bucket(dias)
        if bucket_filter and b != bucket_filter:
            continue
        cobros_list = []
        for c in (cxc.cobros or []):
            cobros_list.append({"numero": c.numero, "fecha": str(c.fecha), "monto": float(c.monto or 0), "metodo": c.metodo_pago or "—", "referencia": c.referencia_bancaria or ""})
        result.append({
            "id": cxc.id, "numero": cxc.numero,
            "cliente_id": cxc.cliente_id,
            "cliente": nombre_cli,
            "fecha": str(cxc.fecha) if cxc.fecha else None,
            "fecha_vencimiento": str(cxc.fecha_vencimiento) if cxc.fecha_vencimiento else None,
            "total": float(cxc.total or 0),
            "saldo": float(cxc.saldo_pendiente or 0),
            "dias": dias, "bucket": b,
            "estado": cxc.estado,
            "campo_id": cxc.campo_id,
            "cobros": cobros_list,
        })

    buckets = {k: 0.0 for k in BUCKET_ORDER}
    bucket_counts = {k: 0 for k in BUCKET_ORDER}
    for r in result:
        buckets[r["bucket"]] += r["saldo"]
        bucket_counts[r["bucket"]] += 1
    total = sum(buckets.values())
    total_vencido = total - buckets["corriente"]
    avg_dias = round(sum(r["dias"] for r in result) / len(result), 1) if result else 0

    grouped = {}
    if agrupar:
        for r in result:
            key = r["cliente_id"]
            if key not in grouped:
                grouped[key] = {"nombre": r["cliente"], "documentos": 0, "saldo_total": 0, "dias_max": 0, "bucket_peor": "corriente", "items": []}
            g = grouped[key]
            g["documentos"] += 1
            g["saldo_total"] += r["saldo"]
            if r["dias"] > g["dias_max"]:
                g["dias_max"] = r["dias"]
                g["bucket_peor"] = r["bucket"]
            g["items"].append(r)
        for g in grouped.values():
            g["saldo_total"] = round(g["saldo_total"], 2)

    return {
        "detalle": sorted(result, key=lambda x: -x["dias"]),
        "resumen": {k: round(v, 2) for k, v in buckets.items()},
        "resumen_count": bucket_counts,
        "total": round(total, 2),
        "total_vencido": round(total_vencido, 2),
        "promedio_dias": avg_dias,
        "num_documentos": len(result),
        "agrupado": list(sorted(grouped.values(), key=lambda x: -x["saldo_total"])) if agrupar else None,
    }


# ══════════════════════════════════════════════════════════════════════════════
# PRESUPUESTO
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/presupuestos")
def listar_presupuestos(anio: int = None, db: Session = Depends(get_db),
                        user=Depends(get_current_user)):
    if user.rol == "operador":
        raise HTTPException(403, "Acceso denegado")
    q = db.query(models.Presupuesto)
    if anio:
        q = q.filter(models.Presupuesto.anio == anio)
    items = q.all()
    result = []
    for p in items:
        d = schemas.PresupuestoOut.model_validate(p).model_dump()
        cta = db.query(models.CuentaContable).get(p.cuenta_id)
        d["cuenta_codigo"] = cta.codigo if cta else None
        d["cuenta_nombre"] = cta.nombre if cta else None
        if p.campo_id:
            campo = db.query(models.Campo).filter(models.Campo.id_campo == p.campo_id).first()
            d["campo_nombre"] = campo.nombre if campo else None
        meses = [p.monto_ene, p.monto_feb, p.monto_mar, p.monto_abr, p.monto_may, p.monto_jun,
                 p.monto_jul, p.monto_ago, p.monto_sep, p.monto_oct, p.monto_nov, p.monto_dic]
        d["total_anual"] = round(sum(float(m or 0) for m in meses), 2)
        result.append(d)
    return result


@router.post("/presupuestos")
def crear_presupuesto(data: schemas.PresupuestoCreate,
                      db: Session = Depends(get_db), user=Depends(require_admin)):
    if not db.query(models.CuentaContable).get(data.cuenta_id):
        raise HTTPException(400, "Cuenta contable no existe")
    dup = db.query(models.Presupuesto).filter(
        models.Presupuesto.anio == data.anio,
        models.Presupuesto.cuenta_id == data.cuenta_id,
        models.Presupuesto.campo_id == data.campo_id
    ).first()
    if dup:
        raise HTTPException(400, "Ya existe presupuesto para esa cuenta/campo/año")
    p = models.Presupuesto(**data.model_dump())
    db.add(p)
    db.commit()
    db.refresh(p)
    return {"ok": True, "id": p.id}


@router.put("/presupuestos/{id}")
def actualizar_presupuesto(id: int, data: schemas.PresupuestoCreate,
                           db: Session = Depends(get_db), user=Depends(require_admin)):
    p = db.query(models.Presupuesto).get(id)
    if not p:
        raise HTTPException(404, "Presupuesto no encontrado")
    for k, v in data.model_dump().items():
        setattr(p, k, v)
    db.commit()
    return {"ok": True}


@router.delete("/presupuestos/{id}")
def eliminar_presupuesto(id: int, db: Session = Depends(get_db), user=Depends(require_admin)):
    p = db.query(models.Presupuesto).get(id)
    if not p:
        raise HTTPException(404, "Presupuesto no encontrado")
    db.delete(p)
    db.commit()
    return {"ok": True}


@router.get("/presupuesto-vs-real")
def presupuesto_vs_real(anio: int = Query(...), campo_id: str = None,
                        db: Session = Depends(get_db), user=Depends(get_current_user)):
    if user.rol == "operador":
        raise HTTPException(403, "Acceso denegado")
    q = db.query(models.Presupuesto).filter(models.Presupuesto.anio == anio)
    if campo_id:
        q = q.filter(models.Presupuesto.campo_id == campo_id)
    presupuestos = q.all()

    meses_keys = ["monto_ene", "monto_feb", "monto_mar", "monto_abr", "monto_may", "monto_jun",
                  "monto_jul", "monto_ago", "monto_sep", "monto_oct", "monto_nov", "monto_dic"]
    result = []
    for p in presupuestos:
        cta = db.query(models.CuentaContable).get(p.cuenta_id)
        row = {
            "cuenta_id": p.cuenta_id,
            "cuenta_codigo": cta.codigo if cta else None,
            "cuenta_nombre": cta.nombre if cta else None,
            "campo_id": p.campo_id,
            "meses": [],
            "total_presupuesto": 0, "total_real": 0,
        }
        for i, mk in enumerate(meses_keys, 1):
            pres = float(getattr(p, mk) or 0)
            periodo = db.query(models.PeriodoContable).filter(
                models.PeriodoContable.anio == anio, models.PeriodoContable.mes == i
            ).first()
            real = 0.0
            if periodo:
                sm = db.query(models.SaldoMensual).filter(
                    models.SaldoMensual.cuenta_id == p.cuenta_id,
                    models.SaldoMensual.periodo_id == periodo.id
                ).first()
                if sm:
                    nat = cta.naturaleza if cta else "deudora"
                    real = float(sm.saldo_deudor or 0) - float(sm.saldo_acreedor or 0)
                    if nat == "acreedora":
                        real = -real
            desv = round(real - pres, 2) if pres else 0
            row["meses"].append({"mes": i, "presupuesto": pres, "real": round(real, 2), "desviacion": desv})
            row["total_presupuesto"] += pres
            row["total_real"] += real
        row["total_presupuesto"] = round(row["total_presupuesto"], 2)
        row["total_real"] = round(row["total_real"], 2)
        row["total_desviacion"] = round(row["total_real"] - row["total_presupuesto"], 2)
        result.append(row)
    return result


# ══════════════════════════════════════════════════════════════════════════════
# ESTADO DE FLUJO DE EFECTIVO
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/flujo-efectivo")
def flujo_efectivo(anio: int, mes: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if user.rol == "operador":
        raise HTTPException(403, "Acceso denegado")
    periodo = db.query(models.PeriodoContable).filter(
        models.PeriodoContable.anio == anio, models.PeriodoContable.mes == mes
    ).first()
    if not periodo:
        raise HTTPException(404, "Período no encontrado")

    cuentas = db.query(models.CuentaContable).filter(models.CuentaContable.acepta_movimientos == True).all()
    cta_map = {c.id: c for c in cuentas}

    saldos = db.query(models.SaldoMensual).filter(models.SaldoMensual.periodo_id == periodo.id).all()

    operaciones = []
    inversion = []
    financiamiento = []

    for sm in saldos:
        cta = cta_map.get(sm.cuenta_id)
        if not cta:
            continue
        code = cta.codigo or ""
        neto = float(sm.saldo_deudor or 0) - float(sm.saldo_acreedor or 0)
        if abs(neto) < 0.01:
            continue
        item = {"codigo": code, "nombre": cta.nombre, "monto": round(neto, 2)}

        if code.startswith("4") or code.startswith("5") or code.startswith("6"):
            operaciones.append(item)
        elif code.startswith("1.2") or code.startswith("12"):
            inversion.append(item)
        elif code.startswith("2.2") or code.startswith("22") or code.startswith("3"):
            financiamiento.append(item)
        else:
            operaciones.append(item)

    total_op = round(sum(i["monto"] for i in operaciones), 2)
    total_inv = round(sum(i["monto"] for i in inversion), 2)
    total_fin = round(sum(i["monto"] for i in financiamiento), 2)

    bancos = db.query(models.CuentaBancaria).filter(models.CuentaBancaria.activo == True).all()
    saldo_bancos = round(sum(float(b.saldo_segun_libro or 0) for b in bancos), 2)

    return {
        "periodo": f"{mes:02d}/{anio}",
        "operaciones": {"items": operaciones, "total": total_op},
        "inversion": {"items": inversion, "total": total_inv},
        "financiamiento": {"items": financiamiento, "total": total_fin},
        "variacion_neta": round(total_op + total_inv + total_fin, 2),
        "saldo_efectivo_actual": saldo_bancos,
    }


# ══════════════════════════════════════════════════════════════════════════════
# ASIENTOS RECURRENTES
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/asientos-recurrentes")
def listar_recurrentes(db: Session = Depends(get_db), user=Depends(get_current_user)):
    if user.rol == "operador":
        raise HTTPException(403, "Acceso denegado")
    items = db.query(models.AsientoRecurrente).order_by(models.AsientoRecurrente.id.desc()).all()
    result = []
    for ar in items:
        lineas = []
        for l in ar.lineas:
            cta = db.query(models.CuentaContable).get(l.cuenta_id)
            lineas.append({
                "id": l.id, "cuenta_id": l.cuenta_id,
                "cuenta_codigo": cta.codigo if cta else "", "cuenta_nombre": cta.nombre if cta else "",
                "descripcion": l.descripcion, "debe": float(l.debe or 0), "haber": float(l.haber or 0),
            })
        result.append({
            "id": ar.id, "nombre": ar.nombre, "descripcion_asiento": ar.descripcion_asiento,
            "frecuencia": ar.frecuencia, "dia_ejecucion": ar.dia_ejecucion,
            "activo": ar.activo, "ultima_ejecucion": str(ar.ultima_ejecucion) if ar.ultima_ejecucion else None,
            "proxima_ejecucion": str(ar.proxima_ejecucion) if ar.proxima_ejecucion else None,
            "veces_ejecutado": ar.veces_ejecutado, "lineas": lineas,
            "total": round(sum(float(l.debe or 0) for l in ar.lineas), 2),
        })
    return result


@router.post("/asientos-recurrentes")
def crear_recurrente(data: dict, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if user.rol not in ("admin", "contador"):
        raise HTTPException(403, "Solo admin/contador")
    ar = models.AsientoRecurrente(
        nombre=data["nombre"], descripcion_asiento=data.get("descripcion_asiento", ""),
        frecuencia=data.get("frecuencia", "mensual"), dia_ejecucion=data.get("dia_ejecucion", 1),
        activo=True,
    )
    db.add(ar)
    db.flush()
    for l in data.get("lineas", []):
        db.add(models.AsientoRecurrenteLinea(
            recurrente_id=ar.id, cuenta_id=l["cuenta_id"],
            descripcion=l.get("descripcion", ""), debe=l.get("debe", 0), haber=l.get("haber", 0),
        ))
    db.commit()
    return {"id": ar.id, "msg": "Asiento recurrente creado"}


@router.put("/asientos-recurrentes/{id}")
def editar_recurrente(id: int, data: dict, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if user.rol not in ("admin", "contador"):
        raise HTTPException(403, "Solo admin/contador")
    ar = db.query(models.AsientoRecurrente).get(id)
    if not ar:
        raise HTTPException(404, "No encontrado")
    ar.nombre = data.get("nombre", ar.nombre)
    ar.descripcion_asiento = data.get("descripcion_asiento", ar.descripcion_asiento)
    ar.frecuencia = data.get("frecuencia", ar.frecuencia)
    ar.dia_ejecucion = data.get("dia_ejecucion", ar.dia_ejecucion)
    ar.activo = data.get("activo", ar.activo)
    if "lineas" in data:
        db.query(models.AsientoRecurrenteLinea).filter(models.AsientoRecurrenteLinea.recurrente_id == id).delete()
        for l in data["lineas"]:
            db.add(models.AsientoRecurrenteLinea(
                recurrente_id=id, cuenta_id=l["cuenta_id"],
                descripcion=l.get("descripcion", ""), debe=l.get("debe", 0), haber=l.get("haber", 0),
            ))
    db.commit()
    return {"msg": "Actualizado"}


@router.delete("/asientos-recurrentes/{id}")
def eliminar_recurrente(id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if user.rol not in ("admin", "contador"):
        raise HTTPException(403, "Solo admin/contador")
    ar = db.query(models.AsientoRecurrente).get(id)
    if not ar:
        raise HTTPException(404, "No encontrado")
    db.delete(ar)
    db.commit()
    return {"msg": "Eliminado"}


@router.post("/asientos-recurrentes/{id}/ejecutar")
def ejecutar_recurrente(id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if user.rol not in ("admin", "contador"):
        raise HTTPException(403, "Solo admin/contador")
    ar = db.query(models.AsientoRecurrente).get(id)
    if not ar:
        raise HTTPException(404, "No encontrado")
    if not ar.lineas:
        raise HTTPException(400, "Sin líneas definidas")
    hoy = date.today()
    periodo = db.query(models.PeriodoContable).filter(
        models.PeriodoContable.anio == hoy.year, models.PeriodoContable.mes == hoy.month
    ).first()
    if not periodo or periodo.estado == "cerrado":
        raise HTTPException(400, "Período actual no disponible o cerrado")
    numero = get_next_seq(db, "asiento", "AST")
    asiento = models.AsientoContable(
        numero=numero, fecha=hoy, periodo_id=periodo.id,
        descripcion=ar.descripcion_asiento or ar.nombre, estado="borrador",
    )
    db.add(asiento)
    db.flush()
    for l in ar.lineas:
        db.add(models.LineaAsiento(
            asiento_id=asiento.id, cuenta_id=l.cuenta_id,
            descripcion=l.descripcion or "", debe=l.debe or 0, haber=l.haber or 0,
        ))
    ar.ultima_ejecucion = hoy
    ar.veces_ejecutado = (ar.veces_ejecutado or 0) + 1
    db.commit()
    return {"msg": f"Asiento {numero} creado", "asiento_numero": numero}


# ══════════════════════════════════════════════════════════════════════════════
# FORMATOS DGII (606 / 607)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/dgii-606")
def dgii_606(anio: int, mes: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Formato 606 — Compras de bienes y servicios (CxP del período)."""
    if user.rol not in ("admin", "contador"):
        raise HTTPException(403, "Solo admin/contador")
    inicio = date(anio, mes, 1)
    _, ultimo = monthrange(anio, mes)
    fin = date(anio, mes, ultimo)
    empresa = db.query(models.ConfiguracionEmpresa).first()
    rnc_empresa = empresa.rnc if empresa else ""

    items = db.query(models.CuentaPorPagar).filter(
        models.CuentaPorPagar.fecha_factura >= inicio,
        models.CuentaPorPagar.fecha_factura <= fin,
        models.CuentaPorPagar.estado != "anulada",
    ).all()
    provs = {}
    for cxp in items:
        if cxp.proveedor_id not in provs:
            provs[cxp.proveedor_id] = db.query(models.Proveedor).get(cxp.proveedor_id)

    rows = []
    for cxp in items:
        prov = provs.get(cxp.proveedor_id)
        rows.append({
            "rnc_cedula": prov.rnc if prov else "",
            "tipo_id": "1" if prov and prov.rnc and len(prov.rnc) == 9 else "2",
            "tipo_bienes_servicios": "02",
            "ncf": cxp.ncf or "",
            "ncf_modificado": "",
            "fecha_comprobante": str(cxp.fecha_factura) if cxp.fecha_factura else "",
            "fecha_pago": "",
            "monto_facturado": float(cxp.subtotal or 0),
            "itbis_facturado": float(cxp.itbis or 0),
            "itbis_retenido": 0,
            "isr_retenido": float(cxp.retencion_isr or 0),
            "total": float(cxp.total or 0),
            "proveedor": prov.nombre if prov else "—",
        })
    return {
        "rnc_empresa": rnc_empresa,
        "periodo": f"{anio}{mes:02d}",
        "cantidad_registros": len(rows),
        "total_monto": round(sum(r["monto_facturado"] for r in rows), 2),
        "total_itbis": round(sum(r["itbis_facturado"] for r in rows), 2),
        "total_retencion_isr": round(sum(r["isr_retenido"] for r in rows), 2),
        "registros": rows,
    }


@router.get("/dgii-607")
def dgii_607(anio: int, mes: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Formato 607 — Ventas de bienes y servicios (CxC del período)."""
    if user.rol not in ("admin", "contador"):
        raise HTTPException(403, "Solo admin/contador")
    inicio = date(anio, mes, 1)
    _, ultimo = monthrange(anio, mes)
    fin = date(anio, mes, ultimo)
    empresa = db.query(models.ConfiguracionEmpresa).first()
    rnc_empresa = empresa.rnc if empresa else ""

    items = db.query(models.CuentaPorCobrar).filter(
        models.CuentaPorCobrar.fecha >= inicio,
        models.CuentaPorCobrar.fecha <= fin,
        models.CuentaPorCobrar.estado != "anulada",
    ).all()
    clis = {}
    for cxc in items:
        if cxc.cliente_id and cxc.cliente_id not in clis:
            clis[cxc.cliente_id] = db.query(models.Cliente).get(cxc.cliente_id)

    rows = []
    for cxc in items:
        cli = clis.get(cxc.cliente_id)
        rows.append({
            "rnc_cedula": cli.rnc if cli and hasattr(cli, 'rnc') else "",
            "tipo_id": "1" if cli and hasattr(cli, 'rnc') and cli.rnc and len(cli.rnc) == 9 else "2",
            "ncf": cxc.ncf or "",
            "ncf_modificado": "",
            "tipo_ingreso": "01",
            "fecha_comprobante": str(cxc.fecha) if cxc.fecha else "",
            "fecha_retencion": "",
            "monto_facturado": float(cxc.subtotal or 0),
            "itbis_facturado": float(cxc.itbis or 0),
            "itbis_retenido_terceros": 0,
            "isr_retenido_terceros": 0,
            "total": float(cxc.total or 0),
            "cliente": cli.nombre if cli else "—",
        })
    return {
        "rnc_empresa": rnc_empresa,
        "periodo": f"{anio}{mes:02d}",
        "cantidad_registros": len(rows),
        "total_monto": round(sum(r["monto_facturado"] for r in rows), 2),
        "total_itbis": round(sum(r["itbis_facturado"] for r in rows), 2),
        "registros": rows,
    }


# ══════════════════════════════════════════════════════════════════════════════
# CONCILIACIÓN BANCARIA
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/conciliaciones")
def listar_conciliaciones(cuenta_bancaria_id: int = None, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if user.rol == "operador":
        raise HTTPException(403, "Acceso denegado")
    q = db.query(models.ConciliacionBancaria)
    if cuenta_bancaria_id:
        q = q.filter(models.ConciliacionBancaria.cuenta_bancaria_id == cuenta_bancaria_id)
    items = q.order_by(models.ConciliacionBancaria.id.desc()).all()
    result = []
    for c in items:
        banco = db.query(models.CuentaBancaria).get(c.cuenta_bancaria_id)
        per = db.query(models.PeriodoContable).get(c.periodo_id)
        result.append({
            "id": c.id, "cuenta_bancaria_id": c.cuenta_bancaria_id,
            "banco": f"{banco.banco} — {banco.numero_cuenta}" if banco else "—",
            "periodo": f"{per.mes:02d}/{per.anio}" if per else "—",
            "saldo_extracto": float(c.saldo_extracto or 0),
            "saldo_libro": float(c.saldo_libro or 0),
            "diferencia": float(c.diferencia or 0),
            "estado": c.estado,
            "fecha_conciliacion": str(c.fecha_conciliacion) if c.fecha_conciliacion else None,
            "partidas": len(c.partidas),
        })
    return result


@router.post("/conciliaciones")
def crear_conciliacion(data: dict, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if user.rol not in ("admin", "contador"):
        raise HTTPException(403, "Solo admin/contador")
    c = models.ConciliacionBancaria(
        cuenta_bancaria_id=data["cuenta_bancaria_id"],
        periodo_id=data["periodo_id"],
        saldo_extracto=data.get("saldo_extracto", 0),
        saldo_libro=data.get("saldo_libro", 0),
        diferencia=round(float(data.get("saldo_extracto", 0)) - float(data.get("saldo_libro", 0)), 2),
    )
    db.add(c)
    db.flush()
    for p in data.get("partidas", []):
        db.add(models.ConciliacionPartida(
            conciliacion_id=c.id, tipo=p["tipo"], descripcion=p.get("descripcion", ""),
            monto=p["monto"], fecha=p.get("fecha"), referencia=p.get("referencia", ""),
        ))
    db.commit()
    return {"id": c.id, "msg": "Conciliación creada"}


@router.get("/conciliaciones/{id}")
def detalle_conciliacion(id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if user.rol == "operador":
        raise HTTPException(403, "Acceso denegado")
    c = db.query(models.ConciliacionBancaria).get(id)
    if not c:
        raise HTTPException(404, "No encontrada")
    banco = db.query(models.CuentaBancaria).get(c.cuenta_bancaria_id)
    per = db.query(models.PeriodoContable).get(c.periodo_id)
    partidas = []
    for p in c.partidas:
        partidas.append({
            "id": p.id, "tipo": p.tipo, "descripcion": p.descripcion,
            "monto": float(p.monto or 0), "fecha": str(p.fecha) if p.fecha else None,
            "referencia": p.referencia, "conciliada": p.conciliada,
        })
    total_partidas = round(sum(p["monto"] for p in partidas), 2)
    return {
        "id": c.id,
        "banco": f"{banco.banco} — {banco.numero_cuenta}" if banco else "—",
        "periodo": f"{per.mes:02d}/{per.anio}" if per else "—",
        "saldo_extracto": float(c.saldo_extracto or 0),
        "saldo_libro": float(c.saldo_libro or 0),
        "diferencia": float(c.diferencia or 0),
        "estado": c.estado,
        "partidas": partidas,
        "total_partidas": total_partidas,
        "saldo_conciliado": round(float(c.saldo_libro or 0) + total_partidas, 2),
    }


@router.put("/conciliaciones/{id}")
def actualizar_conciliacion(id: int, data: dict, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if user.rol not in ("admin", "contador"):
        raise HTTPException(403, "Solo admin/contador")
    c = db.query(models.ConciliacionBancaria).get(id)
    if not c:
        raise HTTPException(404, "No encontrada")
    if "saldo_extracto" in data:
        c.saldo_extracto = data["saldo_extracto"]
    if "saldo_libro" in data:
        c.saldo_libro = data["saldo_libro"]
    c.diferencia = round(float(c.saldo_extracto or 0) - float(c.saldo_libro or 0), 2)
    if "estado" in data:
        c.estado = data["estado"]
        if data["estado"] == "conciliada":
            c.fecha_conciliacion = date.today()
            c.conciliado_por = user.nombre
    if "partidas" in data:
        db.query(models.ConciliacionPartida).filter(models.ConciliacionPartida.conciliacion_id == id).delete()
        for p in data["partidas"]:
            db.add(models.ConciliacionPartida(
                conciliacion_id=id, tipo=p["tipo"], descripcion=p.get("descripcion", ""),
                monto=p["monto"], fecha=p.get("fecha"), referencia=p.get("referencia", ""),
                conciliada=p.get("conciliada", False),
            ))
    db.commit()
    return {"msg": "Conciliación actualizada"}


# ══════════════════════════════════════════════════════════════════════════════
# DASHBOARD FINANCIERO + NOTIFICACIONES
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/dashboard-financiero")
def dashboard_financiero(db: Session = Depends(get_db), user=Depends(get_current_user)):
    if user.rol == "operador":
        raise HTTPException(403, "Acceso denegado")
    hoy = date.today()

    cxp_pend = db.query(models.CuentaPorPagar).filter(models.CuentaPorPagar.estado.notin_(["pagada", "anulada"])).all()
    cxc_pend = db.query(models.CuentaPorCobrar).filter(models.CuentaPorCobrar.estado.notin_(["cobrada", "anulada"])).all()
    total_cxp = round(sum(float(c.saldo_pendiente or 0) for c in cxp_pend), 2)
    total_cxc = round(sum(float(c.saldo_pendiente or 0) for c in cxc_pend), 2)
    cxp_vencidas = round(sum(float(c.saldo_pendiente or 0) for c in cxp_pend if c.fecha_vencimiento and c.fecha_vencimiento < hoy), 2)
    cxc_vencidas = round(sum(float(c.saldo_pendiente or 0) for c in cxc_pend if c.fecha_vencimiento and c.fecha_vencimiento < hoy), 2)

    bancos = db.query(models.CuentaBancaria).filter(models.CuentaBancaria.activo == True).all()
    saldo_bancos = round(sum(float(b.saldo_segun_libro or 0) for b in bancos), 2)

    anio = hoy.year
    ingresos_mes = []
    gastos_mes = []
    for m in range(1, 13):
        per = db.query(models.PeriodoContable).filter(
            models.PeriodoContable.anio == anio, models.PeriodoContable.mes == m
        ).first()
        ing = 0.0
        gas = 0.0
        if per:
            saldos = db.query(models.SaldoMensual).filter(models.SaldoMensual.periodo_id == per.id).all()
            for sm in saldos:
                cta = db.query(models.CuentaContable).get(sm.cuenta_id)
                if cta and cta.codigo:
                    if cta.codigo.startswith("4"):
                        ing += float(sm.saldo_acreedor or 0) - float(sm.saldo_deudor or 0)
                    elif cta.codigo.startswith("5") or cta.codigo.startswith("6"):
                        gas += float(sm.saldo_deudor or 0) - float(sm.saldo_acreedor or 0)
        ingresos_mes.append(round(ing, 2))
        gastos_mes.append(round(gas, 2))

    meses_label = ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]
    tendencia = [{"mes": meses_label[i], "ingresos": ingresos_mes[i], "gastos": gastos_mes[i], "utilidad": round(ingresos_mes[i] - gastos_mes[i], 2)} for i in range(12)]

    return {
        "saldo_bancos": saldo_bancos,
        "total_cxc": total_cxc, "cxc_vencidas": cxc_vencidas, "num_cxc": len(cxc_pend),
        "total_cxp": total_cxp, "cxp_vencidas": cxp_vencidas, "num_cxp": len(cxp_pend),
        "ingresos_anio": round(sum(ingresos_mes), 2),
        "gastos_anio": round(sum(gastos_mes), 2),
        "utilidad_anio": round(sum(ingresos_mes) - sum(gastos_mes), 2),
        "tendencia": tendencia,
    }


@router.get("/notificaciones")
def notificaciones(db: Session = Depends(get_db), user=Depends(get_current_user)):
    hoy = date.today()
    alertas = []

    cxp_venc = db.query(models.CuentaPorPagar).filter(
        models.CuentaPorPagar.estado.notin_(["pagada", "anulada"]),
        models.CuentaPorPagar.fecha_vencimiento != None,
        models.CuentaPorPagar.fecha_vencimiento <= hoy,
    ).all()
    if cxp_venc:
        total = round(sum(float(c.saldo_pendiente or 0) for c in cxp_venc), 2)
        alertas.append({"tipo": "cxp_vencida", "nivel": "danger", "titulo": f"{len(cxp_venc)} facturas por pagar vencidas", "detalle": f"Total: RD$ {total:,.2f}", "accion": "/contabilidad"})

    cxp_prox = db.query(models.CuentaPorPagar).filter(
        models.CuentaPorPagar.estado.notin_(["pagada", "anulada"]),
        models.CuentaPorPagar.fecha_vencimiento != None,
        models.CuentaPorPagar.fecha_vencimiento > hoy,
        models.CuentaPorPagar.fecha_vencimiento <= hoy + timedelta(days=7),
    ).all()
    if cxp_prox:
        total = round(sum(float(c.saldo_pendiente or 0) for c in cxp_prox), 2)
        alertas.append({"tipo": "cxp_proxima", "nivel": "warning", "titulo": f"{len(cxp_prox)} facturas vencen en 7 días", "detalle": f"Total: RD$ {total:,.2f}", "accion": "/contabilidad"})

    cxc_venc = db.query(models.CuentaPorCobrar).filter(
        models.CuentaPorCobrar.estado.notin_(["cobrada", "anulada"]),
        models.CuentaPorCobrar.fecha_vencimiento != None,
        models.CuentaPorCobrar.fecha_vencimiento <= hoy,
    ).all()
    if cxc_venc:
        total = round(sum(float(c.saldo_pendiente or 0) for c in cxc_venc), 2)
        alertas.append({"tipo": "cxc_vencida", "nivel": "warning", "titulo": f"{len(cxc_venc)} facturas por cobrar vencidas", "detalle": f"Total: RD$ {total:,.2f}", "accion": "/contabilidad"})

    inv_bajo = db.query(models.Inventario).filter(
        models.Inventario.cantidad <= models.Inventario.stock_minimo,
        models.Inventario.stock_minimo > 0,
    ).all()
    if inv_bajo:
        alertas.append({"tipo": "inventario_bajo", "nivel": "warning", "titulo": f"{len(inv_bajo)} productos con stock bajo", "detalle": ", ".join(i.producto_id for i in inv_bajo[:5]), "accion": "/inventario"})

    per_abiertos = db.query(models.PeriodoContable).filter(
        models.PeriodoContable.estado == "abierto",
        models.PeriodoContable.anio < hoy.year if hoy.month > 1 else models.PeriodoContable.anio < hoy.year,
    ).all()
    old_periods = [p for p in per_abiertos if p.anio < hoy.year or (p.anio == hoy.year and p.mes < hoy.month - 1)]
    if old_periods:
        alertas.append({"tipo": "periodo_abierto", "nivel": "info", "titulo": f"{len(old_periods)} período(s) contable(s) sin cerrar", "detalle": ", ".join(f"{p.mes:02d}/{p.anio}" for p in old_periods[:3]), "accion": "/contabilidad"})

    return {"alertas": alertas, "total": len(alertas)}
