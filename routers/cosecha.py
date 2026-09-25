"""Cosecha — registro diario de kg por campo y calibre, con entrada a inventario.

Cada registro valida el período de carencia de las aplicaciones fitosanitarias del
campo: cosechar antes de que venza es un riesgo de inocuidad, y para exportación
un rechazo seguro del lote.
"""
import json
from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, field_validator
from sqlalchemy import func
from sqlalchemy.orm import Session

import audit
import auth
import models
from database import get_db
from routers.contabilidad import _crear_asiento_auto, _get_regla_cuentas
from routers.inventario import _f, _recalc_avg_cost
from routers.sequences import get_next

router = APIRouter(prefix="/api/cosecha", tags=["cosecha"])


# ─── Calibres ───────────────────────────────────────────────────────────────

class CalibreIn(BaseModel):
    nombre: str
    orden: int = 0
    producto_id: Optional[str] = None


class CalibreOut(BaseModel):
    id: int
    nombre: str
    orden: int
    producto_id: Optional[str] = None
    producto_nombre: Optional[str] = None
    producto_unidad: Optional[str] = None
    activo: bool


def _calibre_out(c: models.Calibre) -> CalibreOut:
    return CalibreOut(id=c.id, nombre=c.nombre, orden=c.orden or 0, producto_id=c.producto_id,
                      producto_nombre=c.producto.producto if c.producto else None,
                      producto_unidad=c.producto.unidad if c.producto else None,
                      activo=bool(c.activo))


def _validar_producto_calibre(db: Session, producto_id: Optional[str]):
    if not producto_id:
        return
    p = db.query(models.Producto).filter(models.Producto.id_prod == producto_id,
                                         models.Producto.activo == True).first()
    if not p:
        raise HTTPException(400, f"Producto '{producto_id}' no existe o está inactivo")
    if not p.es_inventariable:
        raise HTTPException(400, f"'{p.producto}' no es inventariable: la fruta no podría entrar al stock")


@router.get("/calibres", response_model=List[CalibreOut])
def list_calibres(incluir_inactivos: bool = False, db: Session = Depends(get_db),
                  _=Depends(auth.get_current_user)):
    q = db.query(models.Calibre)
    if not incluir_inactivos:
        q = q.filter(models.Calibre.activo == True)
    return [_calibre_out(c) for c in q.order_by(models.Calibre.orden, models.Calibre.nombre).all()]


@router.post("/calibres", response_model=CalibreOut)
def create_calibre(data: CalibreIn, db: Session = Depends(get_db), _=Depends(auth.require_admin)):
    _validar_producto_calibre(db, data.producto_id)
    nombre = data.nombre.strip()
    existente = db.query(models.Calibre).filter(models.Calibre.nombre == nombre).first()
    if existente:
        if existente.activo:
            raise HTTPException(400, f"Ya existe el calibre '{nombre}'")
        existente.activo, existente.orden, existente.producto_id = True, data.orden, data.producto_id or None
        db.commit()
        db.refresh(existente)
        return _calibre_out(existente)
    c = models.Calibre(nombre=nombre, orden=data.orden, producto_id=data.producto_id or None)
    db.add(c)
    db.commit()
    db.refresh(c)
    return _calibre_out(c)


@router.put("/calibres/{cal_id}", response_model=CalibreOut)
def update_calibre(cal_id: int, data: CalibreIn, db: Session = Depends(get_db),
                   _=Depends(auth.require_admin)):
    c = db.query(models.Calibre).get(cal_id)
    if not c:
        raise HTTPException(404, "Calibre no encontrado")
    _validar_producto_calibre(db, data.producto_id)
    nombre = data.nombre.strip()
    if db.query(models.Calibre).filter(models.Calibre.nombre == nombre, models.Calibre.id != cal_id).first():
        raise HTTPException(400, f"Ya existe otro calibre '{nombre}'")
    c.nombre, c.orden, c.producto_id = nombre, data.orden, data.producto_id or None
    db.commit()
    db.refresh(c)
    return _calibre_out(c)


@router.delete("/calibres/{cal_id}")
def delete_calibre(cal_id: int, db: Session = Depends(get_db), _=Depends(auth.require_admin)):
    c = db.query(models.Calibre).get(cal_id)
    if not c:
        raise HTTPException(404, "Calibre no encontrado")
    usos = db.query(models.CosechaLinea).filter(models.CosechaLinea.calibre_id == cal_id).count()
    if usos:
        c.activo = False
        db.commit()
        return {"ok": True, "message": f"Desactivado (usado en {usos} registros de cosecha)"}
    db.delete(c)
    db.commit()
    return {"ok": True}


# ─── Carencia ───────────────────────────────────────────────────────────────

def _nombres_productos(products_json: Optional[str]) -> str:
    try:
        items = json.loads(products_json or "[]")
    except (ValueError, TypeError):
        return ""
    if isinstance(items, dict):
        items = [items]
    nombres = []
    for it in items if isinstance(items, list) else []:
        if isinstance(it, dict):
            n = it.get("nombre") or it.get("producto") or it.get("product") or it.get("producto_id")
            if n:
                nombres.append(str(n))
        elif isinstance(it, str):
            nombres.append(it)
    return ", ".join(nombres)


def _carencias_vigentes(db: Session, campo_id: str, fecha: date) -> list:
    """Aplicaciones en el campo, hechas hasta `fecha`, cuya carencia aún no vence ese día."""
    fin_dia = datetime.combine(fecha, datetime.max.time())
    logs = db.query(models.SprayLog).filter(
        models.SprayLog.campo_id == campo_id,
        models.SprayLog.earliest_harvest.isnot(None),
        models.SprayLog.application_date <= fin_dia,
    ).all()
    vigentes = []
    for s in logs:
        permitida = s.earliest_harvest.date() if isinstance(s.earliest_harvest, datetime) else s.earliest_harvest
        if permitida > fecha:
            vigentes.append({
                "spray_code": s.spray_code,
                "fecha_aplicacion": s.application_date.date() if s.application_date else None,
                "productos": _nombres_productos(s.products_json),
                "phi_dias": s.phi_days,
                "cosecha_permitida_desde": permitida,
                "dias_restantes": (permitida - fecha).days,
            })
    return sorted(vigentes, key=lambda v: v["cosecha_permitida_desde"], reverse=True)


@router.get("/verificar-carencia")
def verificar_carencia(campo_id: str, fecha: date, db: Session = Depends(get_db),
                       _=Depends(auth.get_current_user)):
    vigentes = _carencias_vigentes(db, campo_id, fecha)
    return {"permitido": not vigentes, "aplicaciones": vigentes}


# ─── Registros de cosecha ───────────────────────────────────────────────────

class CosechaLineaIn(BaseModel):
    calibre_id: int
    kg: float

    @field_validator("kg")
    @classmethod
    def _kg_positivo(cls, v):
        if v < 0:
            raise ValueError("kg no puede ser negativo")
        return v


class CosechaIn(BaseModel):
    fecha: date
    campo_id: str
    temporada: Optional[str] = None
    ot_id: Optional[int] = None
    observaciones: Optional[str] = None
    lineas: List[CosechaLineaIn]
    forzar_carencia: bool = False
    justificacion_carencia: Optional[str] = None


def _cosecha_out(db: Session, c: models.Cosecha) -> dict:
    campo = db.query(models.Campo).filter(models.Campo.id_campo == c.campo_id).first()
    return {
        "id": c.id, "numero": c.numero, "fecha": c.fecha, "campo_id": c.campo_id,
        "campo_nombre": campo.nombre if campo else None, "temporada": c.temporada,
        "ot_id": c.ot_id, "total_kg": _f(c.total_kg), "observaciones": c.observaciones,
        "estado": c.estado, "carencia_forzada": bool(c.carencia_forzada),
        "justificacion_carencia": c.justificacion_carencia, "creado_en": c.creado_en,
        "lineas": [{
            "id": l.id, "calibre_id": l.calibre_id, "calibre": l.calibre.nombre if l.calibre else None,
            "producto_id": l.producto_id, "kg": _f(l.kg), "costo_unitario": _f(l.costo_unitario),
            "valor": round(_f(l.kg) * _f(l.costo_unitario), 2),
        } for l in c.lineas],
    }


@router.get("")
def list_cosechas(
    campo_id: Optional[str] = None, temporada: Optional[str] = None,
    fecha_desde: Optional[date] = None, fecha_hasta: Optional[date] = None,
    incluir_anuladas: bool = False, skip: int = 0, limit: int = 200,
    db: Session = Depends(get_db), _=Depends(auth.get_current_user),
):
    q = db.query(models.Cosecha)
    if campo_id:
        q = q.filter(models.Cosecha.campo_id == campo_id)
    if temporada:
        q = q.filter(models.Cosecha.temporada == temporada)
    if fecha_desde:
        q = q.filter(models.Cosecha.fecha >= fecha_desde)
    if fecha_hasta:
        q = q.filter(models.Cosecha.fecha <= fecha_hasta)
    if not incluir_anuladas:
        q = q.filter(models.Cosecha.estado != "anulada")
    total = q.count()
    items = q.order_by(models.Cosecha.fecha.desc(), models.Cosecha.id.desc()).offset(skip).limit(limit).all()
    return {"total": total, "items": [_cosecha_out(db, c) for c in items]}


@router.get("/temporadas")
def list_temporadas(db: Session = Depends(get_db), _=Depends(auth.get_current_user)):
    rows = db.query(models.Cosecha.temporada).distinct().order_by(models.Cosecha.temporada.desc()).all()
    return [r[0] for r in rows]


@router.get("/resumen")
def resumen_cosecha(temporada: Optional[str] = None, fecha_desde: Optional[date] = None,
                    fecha_hasta: Optional[date] = None, db: Session = Depends(get_db),
                    _=Depends(auth.get_current_user)):
    """Kg por campo y calibre, con rendimiento por hectárea."""
    q = db.query(models.Cosecha.campo_id, models.CosechaLinea.calibre_id,
                 func.coalesce(func.sum(models.CosechaLinea.kg), 0).label("kg")).join(
        models.CosechaLinea, models.CosechaLinea.cosecha_id == models.Cosecha.id
    ).filter(models.Cosecha.estado != "anulada")
    if temporada:
        q = q.filter(models.Cosecha.temporada == temporada)
    if fecha_desde:
        q = q.filter(models.Cosecha.fecha >= fecha_desde)
    if fecha_hasta:
        q = q.filter(models.Cosecha.fecha <= fecha_hasta)
    rows = q.group_by(models.Cosecha.campo_id, models.CosechaLinea.calibre_id).all()

    calibres = {c.id: c for c in db.query(models.Calibre).all()}
    campos = {c.id_campo: c for c in db.query(models.Campo).all()}
    por_campo: dict = {}
    tot_calibre: dict = {}
    for r in rows:
        kg = _f(r.kg)
        fila = por_campo.setdefault(r.campo_id, {"kg_por_calibre": {}, "total_kg": 0.0})
        fila["kg_por_calibre"][r.calibre_id] = fila["kg_por_calibre"].get(r.calibre_id, 0.0) + kg
        fila["total_kg"] += kg
        tot_calibre[r.calibre_id] = tot_calibre.get(r.calibre_id, 0.0) + kg

    campos_out = []
    for campo_id, fila in por_campo.items():
        c = campos.get(campo_id)
        area = _f(c.area_ha) if c else 0.0
        campos_out.append({
            "campo_id": campo_id, "campo_nombre": c.nombre if c else None,
            "variedad": c.variedad if c else None, "area_ha": area,
            "total_kg": round(fila["total_kg"], 2),
            "kg_por_ha": round(fila["total_kg"] / area, 2) if area else None,
            "kg_por_calibre": {str(k): round(v, 2) for k, v in fila["kg_por_calibre"].items()},
        })
    campos_out.sort(key=lambda x: x["campo_id"])
    total = sum(tot_calibre.values())
    return {
        "calibres": [{"id": cid, "nombre": calibres[cid].nombre if cid in calibres else str(cid),
                      "orden": calibres[cid].orden if cid in calibres else 0,
                      "kg": round(kg, 2), "porcentaje": round(kg / total * 100, 1) if total else 0}
                     for cid, kg in sorted(tot_calibre.items(),
                                           key=lambda x: (calibres[x[0]].orden if x[0] in calibres else 0))],
        "campos": campos_out,
        "total_kg": round(total, 2),
    }


@router.post("")
def create_cosecha(data: CosechaIn, db: Session = Depends(get_db),
                   current_user: models.Usuario = Depends(auth.require_operador)):
    campo = db.query(models.Campo).filter(models.Campo.id_campo == data.campo_id).first()
    if not campo or campo.activo is False:
        raise HTTPException(400, f"Campo '{data.campo_id}' no existe o está inactivo")

    lineas_in = [l for l in data.lineas if l.kg > 0]
    if not lineas_in:
        raise HTTPException(400, "Registre al menos un calibre con kg")
    if len({l.calibre_id for l in lineas_in}) != len(lineas_in):
        raise HTTPException(400, "Un calibre aparece dos veces en el registro")
    calibres = {c.id: c for c in db.query(models.Calibre).filter(
        models.Calibre.id.in_([l.calibre_id for l in lineas_in]), models.Calibre.activo == True).all()}
    faltan = [l.calibre_id for l in lineas_in if l.calibre_id not in calibres]
    if faltan:
        raise HTTPException(400, f"Calibre(s) inexistente(s) o inactivo(s): {faltan}")

    if data.ot_id is not None:
        ot = db.query(models.OrdenTrabajo).filter(models.OrdenTrabajo.ot_id == data.ot_id).first()
        if not ot:
            raise HTTPException(400, f"La OT {data.ot_id} no existe")
        if ot.campo_id != data.campo_id:
            raise HTTPException(400, f"La OT {data.ot_id} es del campo {ot.campo_id}, no de {data.campo_id}")

    vigentes = _carencias_vigentes(db, data.campo_id, data.fecha)
    if vigentes:
        if not data.forzar_carencia:
            primera = vigentes[0]
            raise HTTPException(400, {
                "detail": (f"Cosecha bloqueada por período de carencia: la aplicación "
                           f"{primera['spray_code'] or ''} permite cosechar desde "
                           f"{primera['cosecha_permitida_desde']:%d/%m/%Y}."),
                "aplicaciones": vigentes,
                "requiere_override": True,
            })
        auth.require_admin(current_user)
        if len((data.justificacion_carencia or "").strip()) < 15:
            raise HTTPException(400, "Forzar la carencia exige una justificación de al menos 15 caracteres")

    numero = get_next("COS", db)
    temporada = (data.temporada or "").strip() or str(data.fecha.year)
    try:
        cos = models.Cosecha(
            numero=numero, fecha=data.fecha, campo_id=data.campo_id, temporada=temporada,
            ot_id=data.ot_id, observaciones=data.observaciones, usuario_id=current_user.id,
            carencia_forzada=bool(vigentes), justificacion_carencia=(data.justificacion_carencia if vigentes else None),
        )
        db.add(cos)
        db.flush()

        total_kg = 0.0
        lineas_asiento, total_asiento = [], Decimal("0")
        r_cos = _get_regla_cuentas(db, "cosecha", "produccion")
        for l in lineas_in:
            cal = calibres[l.calibre_id]
            linea = models.CosechaLinea(cosecha_id=cos.id, calibre_id=cal.id, kg=l.kg,
                                        producto_id=cal.producto_id, costo_unitario=0)
            total_kg += l.kg

            prod = None
            if cal.producto_id:
                prod = db.query(models.Producto).filter(models.Producto.id_prod == cal.producto_id,
                                                        models.Producto.activo == True).first()
            if prod and prod.es_inventariable:
                # Entra al costo estándar del producto del calibre (su costo unitario).
                costo = _f(prod.costo_unitario)
                nuevo_costo = _recalc_avg_cost(prod, l.kg, costo)
                nuevo_stock = _f(prod.stock_actual) + l.kg
                mov = models.MovimientoInventario(
                    num_documento=numero, producto_id=prod.id_prod, tipo_doc="COS", tipo="entrada",
                    motivo="Cosecha", cantidad=l.kg, costo_unitario=round(costo, 4),
                    costo_promedio_post=round(nuevo_costo, 4), stock_post=round(nuevo_stock, 4),
                    lote=f"{data.campo_id}/{data.fecha:%Y%m%d}", referencia=numero,
                    observacion=f"Cosecha {numero} — {campo.nombre or data.campo_id} — {cal.nombre}",
                    fecha=datetime.combine(data.fecha, datetime.now().time()),
                    usuario_id=current_user.id,
                )
                db.add(mov)
                db.flush()
                prod.stock_actual = round(nuevo_stock, 4)
                prod.costo_promedio = round(nuevo_costo, 4)
                linea.movimiento_id = mov.id
                linea.costo_unitario = round(costo, 4)

                monto = Decimal(str(round(l.kg * costo, 2)))
                if monto > 0 and prod.cuenta_inventario_id and r_cos:
                    lineas_asiento.append({"cuenta_id": prod.cuenta_inventario_id, "debe": monto, "haber": 0,
                                           "campo_id": data.campo_id,
                                           "descripcion_linea": f"Fruta cosechada {cal.nombre}"})
                    total_asiento += monto
            db.add(linea)

        cos.total_kg = round(total_kg, 2)

        if lineas_asiento:
            lineas_asiento.append({"cuenta_id": r_cos[1], "debe": 0, "haber": total_asiento,
                                   "campo_id": data.campo_id,
                                   "descripcion_linea": f"Producción agrícola {numero}"})
            asiento = _crear_asiento_auto(
                db, data.fecha, "COS", numero,
                f"Cosecha {numero} — {campo.nombre or data.campo_id} — {total_kg:,.2f} kg",
                lineas_asiento, current_user.nombre, requerido=True,
            )
            cos.asiento_id = asiento.id if asiento else None

        audit.log(db, current_user, "CREAR", "COSECHA", numero,
                  f"Cosecha {numero}: {data.campo_id} {total_kg:,.2f} kg"
                  + (" (CARENCIA FORZADA)" if vigentes else ""),
                  {"campo_id": data.campo_id, "fecha": str(data.fecha), "total_kg": total_kg,
                   "carencia_forzada": bool(vigentes),
                   "aplicaciones_vigentes": [v["spray_code"] for v in vigentes]})
        db.commit()
        db.refresh(cos)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        import logging
        logging.getLogger(__name__).exception("Error registrando cosecha %s", numero)
        raise HTTPException(500, "Error al registrar la cosecha")
    return _cosecha_out(db, cos)


@router.post("/{cosecha_id}/anular")
def anular_cosecha(cosecha_id: int, motivo: str = Query(..., min_length=5),
                   db: Session = Depends(get_db),
                   current_user: models.Usuario = Depends(auth.require_supervisor)):
    cos = db.query(models.Cosecha).get(cosecha_id)
    if not cos:
        raise HTTPException(404, "Registro de cosecha no encontrado")
    if cos.estado == "anulada":
        raise HTTPException(400, "El registro ya está anulado")

    try:
        for l in cos.lineas:
            if not l.movimiento_id:
                continue
            prod = db.query(models.Producto).filter(models.Producto.id_prod == l.producto_id).first()
            if not prod:
                continue
            kg = _f(l.kg)
            if _f(prod.stock_actual) < kg - 0.0001:
                raise HTTPException(400, (
                    f"No se puede anular: '{prod.producto}' tiene {_f(prod.stock_actual):,.2f} kg y la cosecha "
                    f"aportó {kg:,.2f}. La fruta ya salió de inventario; ajuste o devuelva primero."))
            nuevo_stock = _f(prod.stock_actual) - kg
            db.add(models.MovimientoInventario(
                num_documento=cos.numero, producto_id=prod.id_prod, tipo_doc="COS", tipo="salida",
                motivo="Anulación cosecha", cantidad=kg, costo_unitario=l.costo_unitario,
                costo_promedio_post=prod.costo_promedio, stock_post=round(nuevo_stock, 4),
                referencia=cos.numero, observacion=f"Anulación {cos.numero}: {motivo}",
                fecha=datetime.now(), usuario_id=current_user.id,
            ))
            prod.stock_actual = round(nuevo_stock, 4)

        if cos.asiento_id:
            original = db.query(models.AsientoContable).get(cos.asiento_id)
            if original:
                reverso = [{"cuenta_id": ln.cuenta_id, "debe": ln.haber or 0, "haber": ln.debe or 0,
                            "campo_id": ln.campo_id,
                            "descripcion_linea": f"Reverso {cos.numero}"} for ln in original.lineas]
                _crear_asiento_auto(db, date.today(), "COS", f"{cos.numero}-ANU",
                                    f"Anulación cosecha {cos.numero}: {motivo}",
                                    reverso, current_user.nombre, requerido=True)

        cos.estado = "anulada"
        cos.observaciones = ((cos.observaciones or "") + f"\n[Anulada] {motivo}").strip()
        audit.log(db, current_user, "ANULAR", "COSECHA", cos.numero,
                  f"Cosecha {cos.numero} anulada: {motivo}", {"total_kg": _f(cos.total_kg)})
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise HTTPException(500, "Error al anular la cosecha")
    return {"ok": True, "numero": cos.numero, "estado": "anulada"}
