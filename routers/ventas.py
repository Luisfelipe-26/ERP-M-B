"""Venta de fruta por calibre: despacho → liquidación del cliente → factura.

El cliente (empacador o exportador) clasifica la fruta en su planta y envía una liquidación
con los kg que reconoce por calibre y el rechazo. Por eso la venta tiene dos momentos:

- Despacho: la fruta sale del inventario al costo promedio y queda en "Fruta despachada
  por liquidar". La finca ya no la tiene, pero todavía no está vendida.
- Liquidación: con los kg y precios del cliente se emite la factura (CxC, en US$ llevada a
  pesos) y se reconoce el costo del despacho: la parte liquidada a costo de venta; el
  rechazo y la merma de peso, a merma.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

import audit
import auth
import models
import schemas
from database import get_db
from routers.compras import _cuentas_producto
from routers.contabilidad import (_crear_asiento_auto, _get_regla_cuentas, _registrar_cxc,
                                  _reversar_asiento)
from routers.cosecha import precio_vigente
from routers.inventario import _f, _recalc_avg_cost
from routers.sequences import get_next

router = APIRouter(prefix="/api/ventas", tags=["ventas"])

_EPS = 1e-6
TOL_PESO = 0.02   # la báscula del cliente puede marcar algo más que la de la finca
D2 = Decimal("0.01")


def _d(x) -> Decimal:
    return Decimal(str(x or 0))


# ─── Esquemas ────────────────────────────────────────────────────────────────

class DespachoLineaIn(BaseModel):
    calibre_id: int
    kg: float


class DespachoIn(BaseModel):
    cliente_id: int
    fecha: date
    campo_id: Optional[str] = None
    temporada: Optional[str] = None
    conduce: Optional[str] = None
    observaciones: Optional[str] = None
    lineas: List[DespachoLineaIn]


class LiquidacionLineaIn(BaseModel):
    calibre_id: int
    kg: float
    precio: Optional[float] = None        # por kg; None = el del libro de precios


class LiquidacionIn(BaseModel):
    fecha: date
    ncf: str
    referencia_cliente: Optional[str] = None
    moneda: str = "USD"
    tasa_cambio: Optional[float] = None
    fecha_vencimiento: Optional[date] = None
    kg_rechazo: float = 0
    observaciones: Optional[str] = None
    lineas: List[LiquidacionLineaIn]


# ─── Salidas ─────────────────────────────────────────────────────────────────

def _liquidacion_activa(db: Session, despacho_id: int):
    return db.query(models.LiquidacionVenta).filter(
        models.LiquidacionVenta.despacho_id == despacho_id,
        models.LiquidacionVenta.estado == "activa").first()


def _despacho_out(db: Session, d: models.DespachoFruta) -> dict:
    liq = _liquidacion_activa(db, d.id)
    return {
        "id": d.id, "numero": d.numero, "fecha": d.fecha, "estado": d.estado,
        "cliente_id": d.cliente_id, "cliente": d.cliente.nombre if d.cliente else None,
        "campo_id": d.campo_id, "temporada": d.temporada, "conduce": d.conduce,
        "observaciones": d.observaciones,
        "kg_total": _f(d.kg_total), "costo_total": _f(d.costo_total),
        "dias": (date.today() - d.fecha).days,
        "lineas": [{"calibre_id": l.calibre_id, "calibre": l.calibre.nombre if l.calibre else None,
                    "producto_id": l.producto_id, "kg": _f(l.kg), "costo_unitario": _f(l.costo_unitario)}
                   for l in d.lineas],
        "liquidacion": liq.numero if liq else None,
    }


def _liquidacion_out(db: Session, l: models.LiquidacionVenta) -> dict:
    cxc = db.query(models.CuentaPorCobrar).get(l.cxc_id) if l.cxc_id else None
    costo = _f(l.costo_venta) + _f(l.costo_rechazo)
    margen = _f(l.venta_dop) - costo
    return {
        "id": l.id, "numero": l.numero, "fecha": l.fecha, "estado": l.estado,
        "despacho_id": l.despacho_id, "despacho": l.despacho.numero if l.despacho else None,
        "cliente_id": l.cliente_id, "cliente": l.cliente.nombre if l.cliente else None,
        "referencia_cliente": l.referencia_cliente, "moneda": l.moneda, "tasa_cambio": _f(l.tasa_cambio),
        "kg_despachados": _f(l.despacho.kg_total) if l.despacho else None,
        "kg_liquidados": _f(l.kg_liquidados), "kg_rechazo": _f(l.kg_rechazo), "kg_merma": _f(l.kg_merma),
        "subtotal": _f(l.subtotal), "venta_dop": _f(l.venta_dop),
        "costo_venta": _f(l.costo_venta), "costo_rechazo": _f(l.costo_rechazo), "costo_total": costo,
        "margen_dop": round(margen, 2),
        "margen_pct": round(margen / _f(l.venta_dop) * 100, 1) if _f(l.venta_dop) else None,
        "precio_promedio": round(_f(l.subtotal) / _f(l.kg_liquidados), 4) if _f(l.kg_liquidados) else None,
        "cxc_id": l.cxc_id, "cxc": cxc.numero if cxc else None, "ncf": cxc.ncf if cxc else None,
        "cxc_estado": cxc.estado if cxc else None,
        "lineas": [{"calibre_id": x.calibre_id, "calibre": x.calibre.nombre if x.calibre else None,
                    "kg": _f(x.kg), "precio": _f(x.precio),
                    "precio_libro": _f(x.precio_libro) if x.precio_libro is not None else None,
                    "subtotal": _f(x.subtotal)} for x in l.lineas],
    }


# ─── Apoyo para los formularios ──────────────────────────────────────────────

@router.get("/calibres-stock")
def calibres_stock(db: Session = Depends(get_db), _=Depends(auth.get_current_user)):
    """Calibres con el producto donde está su fruta, su existencia y su costo promedio."""
    out = []
    for c in db.query(models.Calibre).filter(models.Calibre.activo == True).order_by(
            models.Calibre.orden, models.Calibre.nombre).all():
        p = db.query(models.Producto).filter(models.Producto.id_prod == c.producto_id).first() if c.producto_id else None
        out.append({"id": c.id, "nombre": c.nombre, "producto_id": c.producto_id,
                    "producto": p.producto if p else None,
                    "stock_kg": _f(p.stock_actual) if p else 0,
                    "costo_promedio": _f(p.costo_promedio) if p else 0})
    return out


@router.get("/precios-sugeridos")
def precios_sugeridos(cliente_id: int, fecha: Optional[date] = None, moneda: str = "USD",
                      db: Session = Depends(get_db), _=Depends(auth.get_current_user)):
    """Precio del libro para cada calibre: el del cliente, o el base si no tiene propio."""
    fecha = fecha or date.today()
    out = {}
    for c in db.query(models.Calibre).filter(models.Calibre.activo == True).all():
        p = precio_vigente(db, c.id, fecha, cliente_id, moneda.upper())
        out[str(c.id)] = ({"precio": _f(p.precio), "origen": "propio" if p.cliente_id == cliente_id else "base"}
                          if p else None)
    return out


# ─── Despacho ────────────────────────────────────────────────────────────────

@router.post("/despachos")
def crear_despacho(data: DespachoIn, db: Session = Depends(get_db),
                   current_user: models.Usuario = Depends(auth.require_supervisor)):
    """La fruta sale del inventario al costo promedio y queda por liquidar."""
    cli = db.query(models.Cliente).get(data.cliente_id)
    if not cli or cli.activo is False:
        raise HTTPException(400, "Cliente no existe o está inactivo")
    if data.fecha > date.today():
        raise HTTPException(400, "La fecha del despacho no puede ser futura")
    if data.campo_id and not db.query(models.Campo).filter(models.Campo.id_campo == data.campo_id).first():
        raise HTTPException(400, f"Campo {data.campo_id} no existe")
    r_desp = _get_regla_cuentas(db, "venta", "despacho_por_liquidar")
    if not r_desp:
        raise HTTPException(400, "Configure la regla venta / despacho_por_liquidar")
    lineas_in = [l for l in data.lineas if l.kg > 0]
    if not lineas_in:
        raise HTTPException(400, "Indique los kg de al menos un calibre")
    if len({l.calibre_id for l in lineas_in}) != len(lineas_in):
        raise HTTPException(400, "Un calibre aparece dos veces en el despacho")

    numero = get_next("DES", db)
    try:
        d = models.DespachoFruta(
            numero=numero, fecha=data.fecha, cliente_id=cli.id, campo_id=data.campo_id,
            temporada=(data.temporada or "").strip() or str(data.fecha.year),
            conduce=data.conduce, observaciones=data.observaciones, usuario_id=current_user.id)
        db.add(d)
        db.flush()
        momento = datetime.combine(data.fecha, datetime.now().time())
        creditos: dict = {}
        kg_total = costo_total = Decimal("0")
        for l in lineas_in:
            cal = db.query(models.Calibre).get(l.calibre_id)
            if not cal:
                raise HTTPException(400, f"Calibre {l.calibre_id} no existe")
            if not cal.producto_id:
                raise HTTPException(400, f"El calibre {cal.nombre} no está vinculado a un producto de inventario")
            prod = db.query(models.Producto).filter(models.Producto.id_prod == cal.producto_id).first()
            if not prod or not prod.es_inventariable:
                raise HTTPException(400, f"El producto del calibre {cal.nombre} no lleva inventario")
            stock = _f(prod.stock_actual)
            if l.kg > stock + _EPS:
                raise HTTPException(400, f"{cal.nombre}: hay {stock:,.2f} kg en inventario y se intentó despachar {l.kg:,.2f}")
            costo = _f(prod.costo_promedio)
            monto = Decimal(str(round(l.kg * costo, 2)))
            cta_inv = _cuentas_producto(prod)[0]
            if monto > 0 and not cta_inv:
                raise HTTPException(400, f"Configure la cuenta de inventario del producto {prod.id_prod} (o de su categoría)")
            nuevo_stock = stock - l.kg
            mov = models.MovimientoInventario(
                num_documento=numero, producto_id=prod.id_prod, tipo_doc="DES", tipo="salida",
                motivo="Despacho", cantidad=l.kg, costo_unitario=round(costo, 4),
                costo_promedio_post=round(costo, 4), stock_post=round(nuevo_stock, 4),
                referencia=numero, fecha=momento, usuario_id=current_user.id,
                observacion=f"Despacho {numero} a {cli.nombre} — {cal.nombre}")
            db.add(mov)
            db.flush()
            prod.stock_actual = round(nuevo_stock, 4)
            db.add(models.DespachoLinea(despacho_id=d.id, calibre_id=cal.id, producto_id=prod.id_prod,
                                        kg=l.kg, costo_unitario=round(costo, 4), movimiento_id=mov.id))
            if monto > 0:
                creditos[cta_inv] = creditos.get(cta_inv, Decimal("0")) + monto
            kg_total += Decimal(str(l.kg))
            costo_total += monto

        d.kg_total, d.costo_total = kg_total, costo_total
        if costo_total > 0:
            asiento = _crear_asiento_auto(
                db, data.fecha, "DES", numero, f"Despacho {numero} a {cli.nombre} — {kg_total:,.2f} kg",
                [{"cuenta_id": r_desp[0], "debe": costo_total, "haber": 0, "campo_id": data.campo_id,
                  "tercero_id": str(cli.id), "descripcion_linea": f"Fruta despachada por liquidar {numero}"}] +
                [{"cuenta_id": cta, "debe": 0, "haber": m, "campo_id": data.campo_id,
                  "descripcion_linea": f"Salida de fruta {numero}"} for cta, m in creditos.items()],
                current_user.nombre, requerido=True)
            d.asiento_id = asiento.id
        audit.log(db, current_user, "CREAR", "DESPACHO", numero,
                  f"Despacho {numero} a {cli.nombre}: {kg_total:,.2f} kg, costo RD$ {costo_total:,.2f}",
                  {"cliente_id": cli.id, "kg": float(kg_total), "costo": float(costo_total)})
        db.commit()
        db.refresh(d)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        import logging
        logging.getLogger(__name__).exception("Error registrando despacho %s", numero)
        raise HTTPException(500, "Error al registrar el despacho")
    return _despacho_out(db, d)


@router.get("/despachos")
def listar_despachos(estado: Optional[str] = None, cliente_id: Optional[int] = None,
                     temporada: Optional[str] = None, db: Session = Depends(get_db),
                     _=Depends(auth.get_current_user)):
    q = db.query(models.DespachoFruta)
    if estado:
        q = q.filter(models.DespachoFruta.estado == estado)
    if cliente_id:
        q = q.filter(models.DespachoFruta.cliente_id == cliente_id)
    if temporada:
        q = q.filter(models.DespachoFruta.temporada == temporada)
    return [_despacho_out(db, d) for d in q.order_by(models.DespachoFruta.fecha.desc(),
                                                     models.DespachoFruta.id.desc()).limit(500).all()]


@router.get("/despachos/{despacho_id}")
def ver_despacho(despacho_id: int, db: Session = Depends(get_db), _=Depends(auth.get_current_user)):
    d = db.query(models.DespachoFruta).get(despacho_id)
    if not d:
        raise HTTPException(404, "Despacho no encontrado")
    return _despacho_out(db, d)


@router.post("/despachos/{despacho_id}/anular")
def anular_despacho(despacho_id: int, motivo: str = Query(..., min_length=5), db: Session = Depends(get_db),
                    current_user: models.Usuario = Depends(auth.require_supervisor)):
    """La fruta vuelve al inventario a su costo; solo si el cliente aún no la liquidó."""
    d = db.query(models.DespachoFruta).get(despacho_id)
    if not d:
        raise HTTPException(404, "Despacho no encontrado")
    if d.estado == "liquidado":
        raise HTTPException(400, "El despacho ya está liquidado: anule primero su liquidación")
    if d.estado != "despachado":
        raise HTTPException(400, f"El despacho está {d.estado}")
    try:
        for l in d.lineas:
            prod = db.query(models.Producto).filter(models.Producto.id_prod == l.producto_id).first()
            costo = _f(l.costo_unitario)
            nuevo_costo = _recalc_avg_cost(prod, _f(l.kg), costo)
            nuevo_stock = _f(prod.stock_actual) + _f(l.kg)
            db.add(models.MovimientoInventario(
                num_documento=d.numero, producto_id=prod.id_prod, tipo_doc="DES", tipo="entrada",
                motivo="Anulación despacho", cantidad=_f(l.kg), costo_unitario=round(costo, 4),
                costo_promedio_post=round(nuevo_costo, 4), stock_post=round(nuevo_stock, 4),
                referencia=d.numero, fecha=datetime.now(), usuario_id=current_user.id,
                observacion=f"Anulación despacho {d.numero}: {motivo}"))
            prod.stock_actual = round(nuevo_stock, 4)
            prod.costo_promedio = round(nuevo_costo, 4)
        if d.asiento_id:
            a = db.query(models.AsientoContable).get(d.asiento_id)
            if a and a.estado not in ("anulado", "revertido"):
                _reversar_asiento(db, a, f"Anulación despacho {d.numero}: {motivo}", current_user.nombre)
        d.estado = "anulado"
        audit.log(db, current_user, "ANULAR", "DESPACHO", d.numero, f"Despacho {d.numero} anulado — {motivo}")
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        import logging
        logging.getLogger(__name__).exception("Error anulando despacho %s", d.numero)
        raise HTTPException(500, "Error al anular el despacho")
    return {"ok": True, "numero": d.numero, "estado": "anulado"}


# ─── Liquidación y factura ───────────────────────────────────────────────────

@router.post("/despachos/{despacho_id}/liquidacion")
def liquidar_despacho(despacho_id: int, data: LiquidacionIn, db: Session = Depends(get_db),
                      current_user: models.Usuario = Depends(auth.require_supervisor)):
    """Registra la liquidación del cliente, emite la factura y reconoce el costo del despacho."""
    d = db.query(models.DespachoFruta).get(despacho_id)
    if not d:
        raise HTTPException(404, "Despacho no encontrado")
    if d.estado != "despachado":
        raise HTTPException(400, f"El despacho está {d.estado}: no se puede liquidar")
    if data.fecha > date.today():
        raise HTTPException(400, "La fecha de la liquidación no puede ser futura")
    if data.fecha < d.fecha:
        raise HTTPException(400, "La liquidación no puede ser anterior al despacho")
    moneda = (data.moneda or "USD").upper()
    if moneda not in ("USD", "DOP"):
        raise HTTPException(400, "Moneda debe ser USD o DOP")
    if moneda == "USD" and (not data.tasa_cambio or data.tasa_cambio <= 1):
        raise HTTPException(400, "Indique la tasa de cambio (RD$ por US$)")
    if data.kg_rechazo < 0:
        raise HTTPException(400, "El rechazo no puede ser negativo")
    lineas_in = [l for l in data.lineas if l.kg > 0]
    if not lineas_in:
        raise HTTPException(400, "Indique los kg liquidados de al menos un calibre")
    if len({l.calibre_id for l in lineas_in}) != len(lineas_in):
        raise HTTPException(400, "Un calibre aparece dos veces en la liquidación")

    kg_desp = _d(d.kg_total)
    kg_liq = sum((_d(l.kg) for l in lineas_in), Decimal("0"))
    kg_rech = _d(data.kg_rechazo)
    if kg_liq + kg_rech > kg_desp * Decimal(str(1 + TOL_PESO)) + Decimal("0.005"):
        raise HTTPException(400, (
            f"El cliente liquida {kg_liq:,.2f} kg y rechaza {kg_rech:,.2f}, pero se despacharon {kg_desp:,.2f}. "
            f"Revise la liquidación: la diferencia supera el {TOL_PESO:.0%} de tolerancia de báscula."))
    kg_merma = max(Decimal("0"), kg_desp - kg_liq - kg_rech)

    precios = []
    subtotal = Decimal("0")
    for l in lineas_in:
        cal = db.query(models.Calibre).get(l.calibre_id)
        if not cal:
            raise HTTPException(400, f"Calibre {l.calibre_id} no existe")
        libro = precio_vigente(db, cal.id, data.fecha, d.cliente_id, moneda)
        precio = l.precio if l.precio is not None else (_f(libro.precio) if libro else None)
        if precio is None:
            raise HTTPException(400, (
                f"{cal.nombre}: no hay precio en el libro para este cliente en {moneda} al "
                f"{data.fecha:%d/%m/%Y}. Indíquelo en la liquidación."))
        if precio < 0:
            raise HTTPException(400, f"{cal.nombre}: el precio no puede ser negativo")
        sub = (_d(l.kg) * Decimal(str(precio))).quantize(D2)
        precios.append((cal, l.kg, precio, libro, sub))
        subtotal += sub

    # Cuentas del costo: la parte liquidada a costo de venta, el rechazo y la merma a merma.
    r_desp = _get_regla_cuentas(db, "venta", "despacho_por_liquidar")
    r_costo = _get_regla_cuentas(db, "venta", "costo_venta")
    r_merma = _get_regla_cuentas(db, "inventario", "ajuste")
    costo_total = _d(d.costo_total)
    factor = min(Decimal("1"), kg_liq / kg_desp) if kg_desp > 0 else Decimal("1")
    debitos: dict = {}
    costo_venta = Decimal("0")
    for dl in d.lineas:
        prod = db.query(models.Producto).filter(models.Producto.id_prod == dl.producto_id).first()
        costo_l = (_d(dl.kg) * _d(dl.costo_unitario)).quantize(D2)
        parte = (costo_l * factor).quantize(D2)
        cta = _cuentas_producto(prod)[1] or (r_costo[0] if r_costo else None)
        if parte > 0 and not cta:
            raise HTTPException(400, f"Configure la cuenta de costo del producto {dl.producto_id} o la regla venta / costo_venta")
        if parte > 0:
            debitos[cta] = debitos.get(cta, Decimal("0")) + parte
        costo_venta += parte
    # El total de líneas puede diferir en centavos del costo del despacho: se ajusta en la merma.
    costo_rechazo = costo_total - costo_venta
    if costo_total > 0 and not r_desp:
        raise HTTPException(400, "Configure la regla venta / despacho_por_liquidar")
    if costo_rechazo > 0 and not r_merma:
        raise HTTPException(400, "Configure la regla inventario / ajuste (cuenta de merma) para el costo del rechazo")

    cli = db.query(models.Cliente).get(d.cliente_id)
    numero = get_next("LIQ", db)
    try:
        cxc, _ = _registrar_cxc(db, schemas.CuentaPorCobrarCreate(
            cliente_id=d.cliente_id, fecha=data.fecha, ncf=data.ncf, moneda=moneda,
            tasa_cambio=data.tasa_cambio if moneda == "USD" else 1,
            fecha_vencimiento=data.fecha_vencimiento or data.fecha + timedelta(days=cli.condicion_pago_dias or 30),
            subtotal=float(subtotal), itbis=0, total=float(subtotal),
            campo_id=d.campo_id, temporada=d.temporada, kg_vendidos=float(kg_liq),
            precio_por_kg=float((subtotal / kg_liq).quantize(D2)) if kg_liq else None,
        ), current_user, origen="LIQ")

        liq = models.LiquidacionVenta(
            numero=numero, despacho_id=d.id, cliente_id=d.cliente_id, fecha=data.fecha,
            referencia_cliente=data.referencia_cliente, moneda=moneda, tasa_cambio=cxc.tasa_cambio,
            kg_liquidados=kg_liq, kg_rechazo=kg_rech, kg_merma=kg_merma,
            subtotal=subtotal, venta_dop=cxc.total_dop, costo_venta=costo_venta, costo_rechazo=costo_rechazo,
            cxc_id=cxc.id, observaciones=data.observaciones, usuario_id=current_user.id)
        db.add(liq)
        db.flush()
        for cal, kg, precio, libro, sub in precios:
            db.add(models.LiquidacionLinea(liquidacion_id=liq.id, calibre_id=cal.id, kg=kg,
                                           precio=precio, precio_libro=libro.precio if libro else None,
                                           subtotal=sub))

        if costo_total > 0:
            lineas = [{"cuenta_id": cta, "debe": m, "haber": 0, "campo_id": d.campo_id,
                       "descripcion_linea": f"Costo de venta {numero}"} for cta, m in debitos.items()]
            if costo_rechazo > 0:
                lineas.append({"cuenta_id": r_merma[0], "debe": costo_rechazo, "haber": 0, "campo_id": d.campo_id,
                               "descripcion_linea": f"Rechazo {kg_rech:,.2f} kg y merma {kg_merma:,.2f} kg — {numero}"})
            elif costo_rechazo < 0:   # centavos de redondeo
                lineas[0]["debe"] += costo_rechazo
            lineas.append({"cuenta_id": r_desp[0], "debe": 0, "haber": costo_total, "campo_id": d.campo_id,
                           "descripcion_linea": f"Liquida despacho {d.numero}"})
            asiento = _crear_asiento_auto(db, data.fecha, "LIQ", numero,
                                          f"Costo de la liquidación {numero} (despacho {d.numero})",
                                          lineas, current_user.nombre, requerido=True)
            liq.asiento_costo_id = asiento.id
            if costo_rechazo < 0:
                liq.costo_venta, liq.costo_rechazo = costo_total, Decimal("0")

        d.estado = "liquidado"
        margen = _d(cxc.total_dop) - costo_total
        audit.log(db, current_user, "CREAR", "LIQUIDACION", numero,
                  f"Liquidación {numero} de {cli.nombre} (despacho {d.numero}): {kg_liq:,.2f} kg, "
                  f"{moneda} {subtotal:,.2f}, margen RD$ {margen:,.2f}",
                  {"cxc": cxc.numero, "kg_liquidados": float(kg_liq), "kg_rechazo": float(kg_rech),
                   "kg_merma": float(kg_merma), "venta_dop": float(cxc.total_dop), "costo": float(costo_total)})
        db.commit()
        db.refresh(liq)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        import logging
        logging.getLogger(__name__).exception("Error liquidando despacho %s", d.numero)
        raise HTTPException(500, "Error al registrar la liquidación")
    return _liquidacion_out(db, liq)


@router.get("/liquidaciones")
def listar_liquidaciones(cliente_id: Optional[int] = None, estado: Optional[str] = None,
                         db: Session = Depends(get_db), _=Depends(auth.get_current_user)):
    q = db.query(models.LiquidacionVenta)
    if cliente_id:
        q = q.filter(models.LiquidacionVenta.cliente_id == cliente_id)
    if estado:
        q = q.filter(models.LiquidacionVenta.estado == estado)
    return [_liquidacion_out(db, l) for l in q.order_by(models.LiquidacionVenta.fecha.desc(),
                                                        models.LiquidacionVenta.id.desc()).limit(500).all()]


@router.post("/liquidaciones/{liq_id}/anular")
def anular_liquidacion(liq_id: int, motivo: str = Query(..., min_length=5), db: Session = Depends(get_db),
                       current_user: models.Usuario = Depends(auth.require_supervisor)):
    """Anula la liquidación y su factura (si no tiene cobros); el despacho vuelve a por liquidar."""
    liq = db.query(models.LiquidacionVenta).get(liq_id)
    if not liq or liq.estado != "activa":
        raise HTTPException(404, "Liquidación no encontrada o ya anulada")
    cxc = db.query(models.CuentaPorCobrar).get(liq.cxc_id) if liq.cxc_id else None
    if cxc and db.query(models.Cobro).filter(models.Cobro.cxc_id == cxc.id).count():
        raise HTTPException(400, f"La factura {cxc.numero} ya tiene cobros registrados y no se puede anular")
    try:
        texto = f"Anulación liquidación {liq.numero}: {motivo}"
        for aid in (cxc.asiento_id if cxc else None, liq.asiento_costo_id):
            a = db.query(models.AsientoContable).get(aid) if aid else None
            if a and a.estado not in ("anulado", "revertido"):
                _reversar_asiento(db, a, texto, current_user.nombre)
        if cxc:
            cxc.estado = "anulada"
            cxc.saldo_pendiente = 0
        liq.estado = "anulada"
        liq.despacho.estado = "despachado"
        audit.log(db, current_user, "ANULAR", "LIQUIDACION", liq.numero, texto)
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        import logging
        logging.getLogger(__name__).exception("Error anulando liquidación %s", liq.numero)
        raise HTTPException(500, "Error al anular la liquidación")
    return {"ok": True, "numero": liq.numero, "estado": "anulada"}


# ─── Reportes ────────────────────────────────────────────────────────────────

def _saldo_cuenta(db: Session, cuenta_id: int) -> float:
    debe, haber = db.query(sqlfunc.coalesce(sqlfunc.sum(models.LineaAsiento.debe), 0),
                           sqlfunc.coalesce(sqlfunc.sum(models.LineaAsiento.haber), 0)).join(
        models.AsientoContable, models.AsientoContable.id == models.LineaAsiento.asiento_id).filter(
        models.LineaAsiento.cuenta_id == cuenta_id, models.AsientoContable.estado != "anulado").one()
    return round(float(debe or 0) - float(haber or 0), 2)


@router.get("/pendientes")
def despachos_pendientes(db: Session = Depends(get_db), _=Depends(auth.get_current_user)):
    """Despachos que el cliente aún no liquida, contra el saldo de la cuenta de fruta despachada."""
    items = [_despacho_out(db, d) for d in db.query(models.DespachoFruta).filter(
        models.DespachoFruta.estado == "despachado").order_by(models.DespachoFruta.fecha).all()]
    r_desp = _get_regla_cuentas(db, "venta", "despacho_por_liquidar")
    total = round(sum(i["costo_total"] for i in items), 2)
    saldo = _saldo_cuenta(db, r_desp[0]) if r_desp else None
    return {"kg": round(sum(i["kg_total"] for i in items), 2), "costo": total, "saldo_mayor": saldo,
            "diferencia": round(total - saldo, 2) if saldo is not None else None, "items": items}


@router.get("/rentabilidad")
def rentabilidad(temporada: Optional[str] = None, db: Session = Depends(get_db),
                 _=Depends(auth.get_current_user)):
    """Por campo y por calibre: cosechado, despachado, liquidado, rechazo, venta, costo y margen."""
    temporada = temporada or str(date.today().year)
    campos: dict = {}

    def campo(cid):
        return campos.setdefault(cid or "—", {
            "campo_id": cid, "kg_cosechados": 0.0, "kg_despachados": 0.0, "kg_liquidados": 0.0,
            "kg_rechazo": 0.0, "kg_merma": 0.0, "venta_dop": 0.0, "costo": 0.0, "kg_por_liquidar": 0.0})

    for c in db.query(models.Cosecha).filter(models.Cosecha.temporada == temporada,
                                             models.Cosecha.estado != "anulada").all():
        campo(c.campo_id)["kg_cosechados"] += _f(c.total_kg)

    por_calibre: dict = {}
    monedas = set()
    for d in db.query(models.DespachoFruta).filter(models.DespachoFruta.temporada == temporada,
                                                   models.DespachoFruta.estado != "anulado").all():
        fila = campo(d.campo_id)
        fila["kg_despachados"] += _f(d.kg_total)
        liq = _liquidacion_activa(db, d.id)
        if not liq:
            fila["kg_por_liquidar"] += _f(d.kg_total)
            continue
        monedas.add(liq.moneda)
        fila["kg_liquidados"] += _f(liq.kg_liquidados)
        fila["kg_rechazo"] += _f(liq.kg_rechazo)
        fila["kg_merma"] += _f(liq.kg_merma)
        fila["venta_dop"] += _f(liq.venta_dop)
        fila["costo"] += _f(liq.costo_venta) + _f(liq.costo_rechazo)
        for x in liq.lineas:
            k = por_calibre.setdefault(x.calibre_id, {"calibre_id": x.calibre_id,
                                                     "calibre": x.calibre.nombre if x.calibre else None,
                                                     "orden": x.calibre.orden if x.calibre else 0,
                                                     "kg": 0.0, "venta": 0.0, "venta_dop": 0.0})
            k["kg"] += _f(x.kg)
            k["venta"] += _f(x.subtotal)
            k["venta_dop"] += _f(x.subtotal) * _f(liq.tasa_cambio or 1)

    nombres = {c.id_campo: c.nombre for c in db.query(models.Campo).all()}
    filas = []
    for f in campos.values():
        f["campo"] = nombres.get(f["campo_id"], "Sin campo") if f["campo_id"] else "Sin campo"
        f["margen_dop"] = round(f["venta_dop"] - f["costo"], 2)
        f["pct_rechazo"] = round(f["kg_rechazo"] / f["kg_despachados"] * 100, 1) if f["kg_despachados"] else None
        for k in ("kg_cosechados", "kg_despachados", "kg_liquidados", "kg_rechazo", "kg_merma",
                  "venta_dop", "costo", "kg_por_liquidar"):
            f[k] = round(f[k], 2)
        filas.append(f)
    calibres = sorted(por_calibre.values(), key=lambda k: (k["orden"] or 0, k["calibre"] or ""))
    for k in calibres:
        k["precio_promedio"] = round(k["venta"] / k["kg"], 4) if k["kg"] else None
        k["kg"], k["venta"], k["venta_dop"] = round(k["kg"], 2), round(k["venta"], 2), round(k["venta_dop"], 2)
    tot = {k: round(sum(f[k] for f in filas), 2) for k in
           ("kg_cosechados", "kg_despachados", "kg_liquidados", "kg_rechazo", "kg_merma",
            "venta_dop", "costo", "margen_dop", "kg_por_liquidar")}
    return {"temporada": temporada, "moneda_venta": sorted(monedas)[0] if len(monedas) == 1 else None,
            "por_campo": sorted(filas, key=lambda f: f["campo"]), "por_calibre": calibres, "totales": tot}
