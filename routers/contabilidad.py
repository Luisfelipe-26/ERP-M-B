"""
Módulo Contabilidad — Núcleo contable: plan de cuentas, periodos, asientos, libro mayor, reportes.
Contabilización automática integrada (CxP, Pagos, CxC, Cobros).
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func as sqlfunc, cast, Date as SqlDate, and_, extract
from database import get_db
from auth import get_current_user, require_admin
from datetime import date, datetime
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
# PLAN DE CUENTAS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/cuentas")
def listar_cuentas(activo: bool = True, db: Session = Depends(get_db),
                   user=Depends(get_current_user)):
    q = db.query(models.CuentaContable)
    if activo:
        q = q.filter(models.CuentaContable.activo == True)
    cuentas = q.order_by(models.CuentaContable.codigo).all()
    return [schemas.CuentaContableOut.model_validate(c) for c in cuentas]


@router.post("/cuentas")
def crear_cuenta(data: schemas.CuentaContableCreate, db: Session = Depends(get_db),
                 user=Depends(require_admin)):
    if db.query(models.CuentaContable).filter(models.CuentaContable.codigo == data.codigo).first():
        raise HTTPException(400, f"Código {data.codigo} ya existe")
    cuenta = models.CuentaContable(**data.model_dump())
    db.add(cuenta)
    db.commit()
    db.refresh(cuenta)
    return schemas.CuentaContableOut.model_validate(cuenta)


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
    return schemas.CuentaContableOut.model_validate(cuenta)


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
    p.estado = "cerrado"
    p.cerrado_por = user.nombre
    p.cerrado_en = datetime.utcnow()
    db.commit()
    return {"ok": True, "msg": f"Periodo {p.nombre} cerrado"}


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

    activos, pasivos, patrimonio = [], [], []
    total_a, total_p, total_pat = Decimal(0), Decimal(0), Decimal(0)
    resultado_ejercicio = Decimal(0)

    for cid, saldo in acum.items():
        c = cuentas.get(cid)
        if not c:
            continue
        if c.tipo in ("ingreso", "costo", "gasto"):
            resultado_ejercicio -= saldo
            continue
        entry = {"codigo": c.codigo, "nombre": c.nombre, "saldo": float(round(abs(saldo), 2))}
        if c.tipo == "activo":
            activos.append(entry)
            total_a += saldo
        elif c.tipo == "pasivo":
            pasivos.append(entry)
            total_p += abs(saldo)
        elif c.tipo == "patrimonio":
            patrimonio.append(entry)
            total_pat += abs(saldo)

    if resultado_ejercicio != 0:
        patrimonio.append({
            "codigo": "3.4", "nombre": "Resultado del Ejercicio (calculado)",
            "saldo": float(round(abs(resultado_ejercicio), 2))
        })
        total_pat += abs(resultado_ejercicio)

    return {
        "periodo": f"{mes:02d}/{anio}",
        "activos": sorted(activos, key=lambda x: x["codigo"]),
        "total_activos": float(round(total_a, 2)),
        "pasivos": sorted(pasivos, key=lambda x: x["codigo"]),
        "total_pasivos": float(round(total_p, 2)),
        "patrimonio": sorted(patrimonio, key=lambda x: x["codigo"]),
        "total_patrimonio": float(round(total_pat, 2)),
        "cuadra": float(round(total_a - total_p - total_pat, 2)) == 0
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
    ingresos, costos, gastos = [], [], []
    total_ing, total_cos, total_gas = 0, 0, 0

    for cid, neto in acum.items():
        c = cuentas.get(cid)
        if not c:
            continue
        entry = {"codigo": c.codigo, "nombre": c.nombre, "monto": round(abs(neto), 2)}
        if c.tipo == "ingreso":
            ingresos.append(entry)
            total_ing += abs(neto)
        elif c.tipo == "costo":
            costos.append(entry)
            total_cos += abs(neto)
        elif c.tipo == "gasto":
            gastos.append(entry)
            total_gas += abs(neto)

    return {
        "periodo": f"{mes or 'Anual'}/{anio}",
        "campo_id": campo_id,
        "ingresos": sorted(ingresos, key=lambda x: x["codigo"]),
        "total_ingresos": round(total_ing, 2),
        "costos": sorted(costos, key=lambda x: x["codigo"]),
        "total_costos": round(total_cos, 2),
        "utilidad_bruta": round(total_ing - total_cos, 2),
        "gastos": sorted(gastos, key=lambda x: x["codigo"]),
        "total_gastos": round(total_gas, 2),
        "utilidad_neta": round(total_ing - total_cos - total_gas, 2),
    }


# ══════════════════════════════════════════════════════════════════════════════
# REGLAS DE CONTABILIZACIÓN
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/reglas")
def listar_reglas(db: Session = Depends(get_db), user=Depends(get_current_user)):
    reglas = db.query(models.ReglaContabilizacion).filter(
        models.ReglaContabilizacion.activo == True
    ).order_by(models.ReglaContabilizacion.evento).all()
    return [schemas.ReglaContabilizacionOut.model_validate(r) for r in reglas]


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
