"""
Módulo Contabilidad — Núcleo contable: plan de cuentas, periodos, asientos, libro mayor, reportes.
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
    seq = db.query(models.Sequence).filter(models.Sequence.tipo == tipo).first()
    if not seq:
        seq = models.Sequence(tipo=tipo, ultimo_numero=0)
        db.add(seq)
    seq.ultimo_numero += 1
    db.flush()
    return f"{prefix}-{seq.ultimo_numero:04d}"


def find_periodo(db: Session, fecha: date):
    return db.query(models.PeriodoContable).filter(
        models.PeriodoContable.fecha_inicio <= fecha,
        models.PeriodoContable.fecha_fin >= fecha
    ).first()


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
        _actualizar_saldos(db, a, -1)
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
        debe_val = float(l.debe or 0) * factor
        haber_val = float(l.haber or 0) * factor
        sm.saldo_deudor = float(sm.saldo_deudor or 0) + debe_val
        sm.saldo_acreedor = float(sm.saldo_acreedor or 0) + haber_val
        sm.saldo_neto = float(sm.saldo_deudor or 0) - float(sm.saldo_acreedor or 0)


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
    if cuenta_id:
        q = q.filter(models.LineaAsiento.cuenta_id == cuenta_id)
    elif codigo:
        cuenta = db.query(models.CuentaContable).filter(models.CuentaContable.codigo == codigo).first()
        if cuenta:
            q = q.filter(models.LineaAsiento.cuenta_id == cuenta.id)
    if desde:
        q = q.filter(models.AsientoContable.fecha >= desde)
    if hasta:
        q = q.filter(models.AsientoContable.fecha <= hasta)

    total = q.count()
    lineas = q.order_by(models.AsientoContable.fecha, models.AsientoContable.id
                        ).offset(skip).limit(limit).all()
    result = []
    saldo_acum = Decimal(0)
    for l in lineas:
        saldo_acum += (l.debe or 0) - (l.haber or 0)
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
            acum[cid] = {"deudor": 0, "acreedor": 0}
        acum[cid]["deudor"] += float(s.saldo_deudor or 0)
        acum[cid]["acreedor"] += float(s.saldo_acreedor or 0)

    cuentas = db.query(models.CuentaContable).filter(
        models.CuentaContable.activo == True
    ).order_by(models.CuentaContable.codigo).all()

    result = []
    for c in cuentas:
        d = acum.get(c.id, {"deudor": 0, "acreedor": 0})
        saldo = d["deudor"] - d["acreedor"]
        if d["deudor"] == 0 and d["acreedor"] == 0:
            continue
        result.append({
            "codigo": c.codigo, "nombre": c.nombre, "tipo": c.tipo,
            "naturaleza": c.naturaleza,
            "sumas_debe": round(d["deudor"], 2),
            "sumas_haber": round(d["acreedor"], 2),
            "saldo_deudor": round(saldo, 2) if saldo > 0 else 0,
            "saldo_acreedor": round(abs(saldo), 2) if saldo < 0 else 0,
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
            acum[cid] = 0
        acum[cid] += float(s.saldo_deudor or 0) - float(s.saldo_acreedor or 0)

    cuentas = {c.id: c for c in db.query(models.CuentaContable).filter(
        models.CuentaContable.activo == True).all()}

    activos, pasivos, patrimonio = [], [], []
    total_a, total_p, total_pat = 0, 0, 0

    for cid, saldo in acum.items():
        c = cuentas.get(cid)
        if not c:
            continue
        entry = {"codigo": c.codigo, "nombre": c.nombre, "saldo": round(abs(saldo), 2)}
        if c.tipo == "activo":
            activos.append(entry)
            total_a += saldo
        elif c.tipo == "pasivo":
            pasivos.append(entry)
            total_p += abs(saldo)
        elif c.tipo == "patrimonio":
            patrimonio.append(entry)
            total_pat += abs(saldo)

    return {
        "periodo": f"{mes:02d}/{anio}",
        "activos": sorted(activos, key=lambda x: x["codigo"]),
        "total_activos": round(total_a, 2),
        "pasivos": sorted(pasivos, key=lambda x: x["codigo"]),
        "total_pasivos": round(total_p, 2),
        "patrimonio": sorted(patrimonio, key=lambda x: x["codigo"]),
        "total_patrimonio": round(total_pat, 2),
        "cuadra": round(total_a - total_p - total_pat, 2) == 0
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
        subtotal=data.subtotal,
        itbis=data.itbis,
        retencion_isr=data.retencion_isr,
        total=data.total,
        saldo_pendiente=data.total,
        notas=data.notas,
    )
    db.add(cxp)
    db.commit()
    db.refresh(cxp)
    return {"ok": True, "numero": numero, "id": cxp.id}


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
    if data.monto > float(cxp.saldo_pendiente):
        raise HTTPException(400, f"Monto ({data.monto}) excede saldo pendiente ({cxp.saldo_pendiente})")

    numero = get_next_seq(db, "PAG", "PAG")
    pago = models.Pago(
        numero=numero, cxp_id=data.cxp_id, fecha=data.fecha,
        monto=data.monto, metodo_pago=data.metodo_pago,
        referencia_bancaria=data.referencia_bancaria,
        cuenta_bancaria_id=data.cuenta_bancaria_id,
    )
    db.add(pago)

    cxp.saldo_pendiente = float(cxp.saldo_pendiente) - data.monto
    if cxp.saldo_pendiente <= 0.005:
        cxp.saldo_pendiente = 0
        cxp.estado = "pagada"
    else:
        cxp.estado = "parcial"

    db.commit()
    db.refresh(pago)
    return {"ok": True, "numero": numero, "id": pago.id, "saldo_restante": float(cxp.saldo_pendiente)}


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
    numero = get_next_seq(db, "CXC", "CXC")
    tasa = data.tasa_cambio or 1
    total_dop = data.total * tasa if data.moneda == "USD" else data.total
    cxc = models.CuentaPorCobrar(
        numero=numero,
        cliente_id=data.cliente_id,
        tipo_ncf=data.tipo_ncf,
        fecha=data.fecha,
        fecha_vencimiento=data.fecha_vencimiento,
        moneda=data.moneda,
        tasa_cambio=tasa,
        subtotal=data.subtotal,
        itbis=data.itbis,
        total=data.total,
        total_dop=total_dop,
        saldo_pendiente=data.total,
        campo_id=data.campo_id,
        temporada=data.temporada,
        kg_vendidos=data.kg_vendidos,
        precio_por_kg=data.precio_por_kg,
    )
    db.add(cxc)
    db.commit()
    db.refresh(cxc)
    return {"ok": True, "numero": numero, "id": cxc.id}


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
    if data.monto > float(cxc.saldo_pendiente):
        raise HTTPException(400, f"Monto ({data.monto}) excede saldo pendiente ({cxc.saldo_pendiente})")

    numero = get_next_seq(db, "COB", "COB")
    cobro = models.Cobro(
        numero=numero, cxc_id=data.cxc_id, fecha=data.fecha,
        monto=data.monto, metodo_pago=data.metodo_pago,
        referencia_bancaria=data.referencia_bancaria,
        cuenta_bancaria_id=data.cuenta_bancaria_id,
    )
    db.add(cobro)

    cxc.saldo_pendiente = float(cxc.saldo_pendiente) - data.monto
    if cxc.saldo_pendiente <= 0.005:
        cxc.saldo_pendiente = 0
        cxc.estado = "cobrada"
    else:
        cxc.estado = "parcial"

    db.commit()
    db.refresh(cobro)
    return {"ok": True, "numero": numero, "id": cobro.id, "saldo_restante": float(cxc.saldo_pendiente)}


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
