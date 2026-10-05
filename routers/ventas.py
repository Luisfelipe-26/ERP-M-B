"""Venta de fruta por calibre: despacho → liquidación del cliente → factura.

El cliente (empacador o exportador) clasifica la fruta en su planta y envía una liquidación
con los kg que reconoce por calibre y el rechazo. Por eso la venta tiene dos momentos:

- Despacho: la fruta sale del inventario al costo promedio y queda en "Fruta despachada
  por liquidar". La finca ya no la tiene, pero todavía no está vendida.
- Liquidación: los kg por calibre y los precios del cliente, su rechazo y la merma de peso.
- Factura: agrupa una o varias liquidaciones del cliente (la planta suele facturar varias
  recepciones juntas). Emite la CxC (en US$ llevada a pesos) y reconoce el costo de los
  despachos: la parte liquidada a costo de venta; el rechazo y la merma, a merma.

El importador carga de una vez el reporte de liquidaciones de la planta.
"""
import re
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
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
SALTO = chr(10)


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


class FacturaVentaDatos(BaseModel):
    ncf: str
    fecha: Optional[date] = None          # None = la de la liquidación más reciente
    tasa_cambio: Optional[float] = None
    fecha_vencimiento: Optional[date] = None


class FacturaVentaIn(FacturaVentaDatos):
    liquidacion_ids: List[int]


class LiquidacionIn(BaseModel):
    fecha: date
    referencia_cliente: Optional[str] = None
    moneda: str = "USD"
    kg_rechazo: float = 0
    observaciones: Optional[str] = None
    lineas: List[LiquidacionLineaIn]
    # Atajo: facturar en el mismo paso. Si no, la liquidación queda por facturar y puede
    # agruparse con otras en una sola factura, como hace la planta en su factura semanal.
    facturar: bool = True
    ncf: Optional[str] = None
    tasa_cambio: Optional[float] = None
    fecha_vencimiento: Optional[date] = None


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
    margen = _f(l.venta_dop) - costo if cxc else None
    return {
        "id": l.id, "numero": l.numero, "fecha": l.fecha, "estado": l.estado,
        "facturada": bool(cxc), "por_facturar": l.estado == "activa" and not cxc,
        "despacho_id": l.despacho_id, "despacho": l.despacho.numero if l.despacho else None,
        "campo_id": l.despacho.campo_id if l.despacho else None,
        "cliente_id": l.cliente_id, "cliente": l.cliente.nombre if l.cliente else None,
        "referencia_cliente": l.referencia_cliente, "moneda": l.moneda,
        "tasa_cambio": _f(l.tasa_cambio) if l.tasa_cambio else None,
        "kg_despachados": _f(l.despacho.kg_total) if l.despacho else None,
        "kg_liquidados": _f(l.kg_liquidados), "kg_rechazo": _f(l.kg_rechazo), "kg_merma": _f(l.kg_merma),
        "subtotal": _f(l.subtotal), "venta_dop": _f(l.venta_dop),
        "costo_venta": _f(l.costo_venta), "costo_rechazo": _f(l.costo_rechazo), "costo_total": costo,
        "margen_dop": round(margen, 2) if margen is not None else None,
        "margen_pct": round(margen / _f(l.venta_dop) * 100, 1) if margen is not None and _f(l.venta_dop) else None,
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
                    "es_granel": bool(c.es_granel), "producto": p.producto if p else None,
                    "stock_kg": _f(p.stock_actual) if p else 0,
                    "costo_promedio": _f(p.costo_promedio) if p else 0})
    return out


@router.get("/precios-sugeridos")
def precios_sugeridos(cliente_id: int, fecha: Optional[date] = None, moneda: str = "USD",
                      db: Session = Depends(get_db), _=Depends(auth.get_current_user)):
    """Precio del libro para cada calibre: el del cliente, o el base si no tiene propio."""
    fecha = fecha or date.today()
    out = {}
    for c in db.query(models.Calibre).filter(models.Calibre.activo == True,
                                             models.Calibre.es_granel.isnot(True)).all():
        p = precio_vigente(db, c.id, fecha, cliente_id, moneda.upper())
        out[str(c.id)] = ({"precio": _f(p.precio), "origen": "propio" if p.cliente_id == cliente_id else "base"}
                          if p else None)
    return out


# ─── Despacho ────────────────────────────────────────────────────────────────

def _registrar_despacho(db: Session, data: DespachoIn, current_user) -> models.DespachoFruta:
    """La fruta sale del inventario al costo promedio y queda por liquidar. Sin confirmar."""
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
    # Una recepción de la planta puede traer fruta de varios campos: el conduce se repite,
    # pero no para el mismo campo.
    conduce = (data.conduce or "").strip() or None
    if conduce:
        otro = db.query(models.DespachoFruta).filter(
            models.DespachoFruta.cliente_id == cli.id, models.DespachoFruta.conduce == conduce,
            models.DespachoFruta.campo_id == data.campo_id,
            models.DespachoFruta.estado != "anulado").first()
        if otro:
            raise HTTPException(400, f"El conduce {conduce} ya está registrado para ese campo en el despacho {otro.numero}")

    numero = get_next("DES", db)
    d = models.DespachoFruta(
        numero=numero, fecha=data.fecha, cliente_id=cli.id, campo_id=data.campo_id,
        temporada=(data.temporada or "").strip() or str(data.fecha.year),
        conduce=conduce, observaciones=data.observaciones, usuario_id=current_user.id)
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
        # Se bloquea la fila: otra operación simultánea sobre la misma fruta espera a que
        # esta termine y ve la existencia ya descontada.
        prod = db.query(models.Producto).filter(models.Producto.id_prod == cal.producto_id).with_for_update().first()
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
    return d


def _confirmar(db: Session, accion, error: str):
    """Ejecuta `accion` y confirma; deshace todo si algo falla."""
    try:
        out = accion()
        db.commit()
        return out
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        import logging
        logging.getLogger(__name__).exception(error)
        raise HTTPException(500, error)


@router.post("/despachos")
def crear_despacho(data: DespachoIn, db: Session = Depends(get_db),
                   current_user: models.Usuario = Depends(auth.require_supervisor)):
    d = _confirmar(db, lambda: _registrar_despacho(db, data, current_user), "Error al registrar el despacho")
    db.refresh(d)
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

    def anular():
        for l in d.lineas:
            prod = db.query(models.Producto).filter(models.Producto.id_prod == l.producto_id).with_for_update().first()
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

    _confirmar(db, anular, "Error al anular el despacho")
    return {"ok": True, "numero": d.numero, "estado": "anulado"}


# ─── Liquidación ─────────────────────────────────────────────────────────────

def _reparto_costo(db: Session, d: models.DespachoFruta, kg_liq: Decimal, kg_rech: Decimal):
    """Reparte el costo del despacho: lo liquidado a la Cuenta Costo de su producto, el resto a merma.

    Si la báscula del cliente pesa más que la de la finca, se reparte sobre lo que él pesó, así
    el rechazo igual lleva su costo. Devuelve ({cuenta: monto}, costo_venta, costo_rechazo).
    """
    costo_total = _d(d.costo_total)
    base = max(_d(d.kg_total), kg_liq + kg_rech)
    factor = kg_liq / base if base > 0 else Decimal("1")
    debitos: dict = {}
    costo_venta = Decimal("0")
    for dl in d.lineas:
        prod = db.query(models.Producto).filter(models.Producto.id_prod == dl.producto_id).first()
        parte = ((_d(dl.kg) * _d(dl.costo_unitario)).quantize(D2) * factor).quantize(D2)
        if parte <= 0:
            continue
        # El costo de la fruta vendida va a la cuenta de costo de su producto. Caer en la regla
        # genérica (por defecto "Insumos agrícolas (consumo)") clasificaba mal la utilidad bruta.
        cta = _cuentas_producto(prod)[1]
        if not cta:
            raise HTTPException(400, (
                f"El producto {dl.producto_id} no tiene Cuenta Costo (ni su categoría): asígnele en "
                "Productos la cuenta de costo de ventas de la fruta"))
        debitos[cta] = debitos.get(cta, Decimal("0")) + parte
        costo_venta += parte
    costo_rechazo = costo_total - costo_venta
    if costo_rechazo < 0 and debitos:          # centavos de redondeo
        primera = next(iter(debitos))
        debitos[primera] += costo_rechazo
        costo_venta += costo_rechazo
        costo_rechazo = Decimal("0")
    return debitos, costo_venta, costo_rechazo


def _registrar_liquidacion(db: Session, d: models.DespachoFruta, data: LiquidacionIn, current_user):
    """Registra la clasificación y los precios del cliente. No factura ni contabiliza: eso
    lo hace la factura, que puede agrupar varias liquidaciones. Sin confirmar."""
    if d.estado != "despachado":
        raise HTTPException(400, f"El despacho {d.numero} está {d.estado}: no se puede liquidar")
    if data.fecha > date.today():
        raise HTTPException(400, "La fecha de la liquidación no puede ser futura")
    if data.fecha < d.fecha:
        raise HTTPException(400, "La liquidación no puede ser anterior al despacho")
    moneda = (data.moneda or "USD").upper()
    if moneda not in ("USD", "DOP"):
        raise HTTPException(400, "Moneda debe ser USD o DOP")
    if data.kg_rechazo < 0:
        raise HTTPException(400, "El rechazo no puede ser negativo")
    lineas_in = [l for l in data.lineas if l.kg > 0]
    if not lineas_in:
        raise HTTPException(400, "Indique los kg liquidados de al menos un calibre")
    if len({l.calibre_id for l in lineas_in}) != len(lineas_in):
        raise HTTPException(400, "Un calibre aparece dos veces en la liquidación")
    ref = (data.referencia_cliente or "").strip() or None
    if ref:
        otra = db.query(models.LiquidacionVenta).join(
            models.DespachoFruta, models.DespachoFruta.id == models.LiquidacionVenta.despacho_id).filter(
            models.LiquidacionVenta.cliente_id == d.cliente_id, models.LiquidacionVenta.referencia_cliente == ref,
            models.DespachoFruta.campo_id == d.campo_id, models.LiquidacionVenta.estado == "activa").first()
        if otra:
            raise HTTPException(400, f"La liquidación {ref} del cliente ya está registrada para ese campo ({otra.numero})")

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
        if cal.es_granel:
            raise HTTPException(400, (
                f"{cal.nombre} es fruta sin clasificar: la liquidación va por los calibres "
                "en que la clasificó el cliente"))
        libro = precio_vigente(db, cal.id, data.fecha, d.cliente_id, moneda)
        precio = l.precio if l.precio is not None else (_f(libro.precio) if libro else None)
        if precio is None:
            raise HTTPException(400, (
                f"{cal.nombre}: no hay precio en el libro para este cliente en {moneda} al "
                f"{data.fecha:%d/%m/%Y}. Indíquelo en la liquidación."))
        if precio < 0:
            raise HTTPException(400, f"{cal.nombre}: el precio no puede ser negativo")
        # Redondeo comercial (hacia arriba en la mitad), el mismo de la liquidación de la planta.
        sub = (_d(l.kg) * Decimal(str(precio))).quantize(D2, rounding=ROUND_HALF_UP)
        precios.append((cal, l.kg, precio, libro, sub))
        subtotal += sub

    # El costo se calcula ya, para avisar de una cuenta faltante antes de facturar.
    _, costo_venta, costo_rechazo = _reparto_costo(db, d, kg_liq, kg_rech)
    if costo_rechazo > 0 and not _get_regla_cuentas(db, "inventario", "ajuste"):
        raise HTTPException(400, "Configure la regla inventario / ajuste (cuenta de merma) para el costo del rechazo")

    numero = get_next("LIQ", db)
    liq = models.LiquidacionVenta(
        numero=numero, despacho_id=d.id, cliente_id=d.cliente_id, fecha=data.fecha,
        referencia_cliente=ref, moneda=moneda,
        kg_liquidados=kg_liq, kg_rechazo=kg_rech, kg_merma=kg_merma, subtotal=subtotal,
        venta_dop=0, costo_venta=costo_venta, costo_rechazo=costo_rechazo,
        observaciones=data.observaciones, usuario_id=current_user.id)
    db.add(liq)
    db.flush()
    for cal, kg, precio, libro, sub in precios:
        db.add(models.LiquidacionLinea(liquidacion_id=liq.id, calibre_id=cal.id, kg=kg, precio=precio,
                                       precio_libro=libro.precio if libro else None, subtotal=sub))
    d.estado = "liquidado"
    audit.log(db, current_user, "CREAR", "LIQUIDACION", numero,
              f"Liquidación {numero} (despacho {d.numero}, ref. {ref or '—'}): {kg_liq:,.2f} kg, "
              f"{moneda} {subtotal:,.2f}",
              {"kg_liquidados": float(kg_liq), "kg_rechazo": float(kg_rech), "kg_merma": float(kg_merma),
               "subtotal": float(subtotal)})
    db.flush()
    return liq


# ─── Factura de venta (una o varias liquidaciones) ──────────────────────────

def _facturar(db: Session, liqs: list, datos: FacturaVentaDatos, current_user):
    """Emite una factura por una o varias liquidaciones del mismo cliente y moneda.

    Reconoce la venta (CxC en pesos a la tasa de la factura) y el costo de los despachos:
    lo liquidado a costo de venta y el rechazo y la merma a merma, cada uno con su campo.
    Sin confirmar.
    """
    if not liqs:
        raise HTTPException(400, "Seleccione al menos una liquidación")
    for l in liqs:
        if l.estado != "activa":
            raise HTTPException(400, f"La liquidación {l.numero} está anulada")
        if l.cxc_id:
            raise HTTPException(400, f"La liquidación {l.numero} ya está facturada")
    if len({l.cliente_id for l in liqs}) > 1:
        raise HTTPException(400, "Las liquidaciones de una factura deben ser del mismo cliente")
    if len({l.moneda for l in liqs}) > 1:
        raise HTTPException(400, "Las liquidaciones de una factura deben estar en la misma moneda")
    moneda = liqs[0].moneda
    if not (datos.ncf or "").strip():
        raise HTTPException(400, "Indique el NCF de la factura")
    if moneda == "USD" and (not datos.tasa_cambio or datos.tasa_cambio <= 1):
        raise HTTPException(400, "Indique la tasa de cambio de la factura (RD$ por US$)")
    fecha = datos.fecha or max(l.fecha for l in liqs)
    if fecha < max(l.fecha for l in liqs):
        raise HTTPException(400, "La factura no puede ser anterior a sus liquidaciones")

    cli = db.query(models.Cliente).get(liqs[0].cliente_id)
    subtotal = sum((_d(l.subtotal) for l in liqs), Decimal("0"))
    kg = sum((_d(l.kg_liquidados) for l in liqs), Decimal("0"))
    campos = {l.despacho.campo_id for l in liqs}
    cxc, _ = _registrar_cxc(db, schemas.CuentaPorCobrarCreate(
        cliente_id=cli.id, fecha=fecha, ncf=datos.ncf, moneda=moneda,
        tasa_cambio=datos.tasa_cambio if moneda == "USD" else 1,
        fecha_vencimiento=datos.fecha_vencimiento or fecha + timedelta(days=cli.condicion_pago_dias or 30),
        subtotal=float(subtotal), itbis=0, total=float(subtotal),
        campo_id=campos.pop() if len(campos) == 1 else None, temporada=liqs[0].despacho.temporada,
        kg_vendidos=float(kg), precio_por_kg=float((subtotal / kg).quantize(D2)) if kg else None,
    ), current_user, origen="LIQ")

    # Costo de los despachos, por cuenta y campo
    r_desp = _get_regla_cuentas(db, "venta", "despacho_por_liquidar")
    r_merma = _get_regla_cuentas(db, "inventario", "ajuste")
    debe: dict = {}
    haber: dict = {}
    for l in liqs:
        d = l.despacho
        debitos, costo_venta, costo_rechazo = _reparto_costo(db, d, _d(l.kg_liquidados), _d(l.kg_rechazo))
        for cta, m in debitos.items():
            debe[(cta, d.campo_id)] = debe.get((cta, d.campo_id), Decimal("0")) + m
        if costo_rechazo > 0:
            if not r_merma:
                raise HTTPException(400, "Configure la regla inventario / ajuste (cuenta de merma)")
            debe[(r_merma[0], d.campo_id)] = debe.get((r_merma[0], d.campo_id), Decimal("0")) + costo_rechazo
        if _d(d.costo_total) > 0:
            if not r_desp:
                raise HTTPException(400, "Configure la regla venta / despacho_por_liquidar")
            haber[d.campo_id] = haber.get(d.campo_id, Decimal("0")) + _d(d.costo_total)
        l.costo_venta, l.costo_rechazo = costo_venta, costo_rechazo
    asiento_costo = None
    if haber:
        asiento_costo = _crear_asiento_auto(
            db, fecha, "LIQ", cxc.numero,
            f"Costo de venta de la factura {cxc.ncf or cxc.numero} ({len(liqs)} liquidación/es)",
            [{"cuenta_id": cta, "debe": m, "haber": 0, "campo_id": campo,
              "descripcion_linea": f"Costo de venta {cxc.numero}"} for (cta, campo), m in debe.items()] +
            [{"cuenta_id": r_desp[0], "debe": 0, "haber": m, "campo_id": campo,
              "descripcion_linea": f"Liquida despachos de {cxc.numero}"} for campo, m in haber.items()],
            current_user.nombre, requerido=True)

    # La venta en pesos se reparte entre las liquidaciones; la última absorbe los centavos.
    tasa = _d(cxc.tasa_cambio)
    asignado = Decimal("0")
    for i, l in enumerate(liqs):
        parte = (_d(l.subtotal) * tasa).quantize(D2) if i < len(liqs) - 1 else _d(cxc.total_dop) - asignado
        asignado += parte
        l.venta_dop, l.tasa_cambio, l.cxc_id = parte, cxc.tasa_cambio, cxc.id
        l.asiento_costo_id = asiento_costo.id if asiento_costo else None
    audit.log(db, current_user, "FACTURAR", "LIQUIDACION", cxc.numero,
              f"Factura {cxc.ncf} ({cxc.numero}) a {cli.nombre}: {len(liqs)} liquidación/es, "
              f"{kg:,.2f} kg, {moneda} {subtotal:,.2f}",
              {"liquidaciones": [l.numero for l in liqs], "total_dop": float(cxc.total_dop)})
    return cxc


def _factura_out(db: Session, cxc) -> dict:
    liqs = db.query(models.LiquidacionVenta).filter(models.LiquidacionVenta.cxc_id == cxc.id).all()
    costo = sum(_f(l.costo_venta) + _f(l.costo_rechazo) for l in liqs)
    return {"cxc_id": cxc.id, "cxc": cxc.numero, "ncf": cxc.ncf, "fecha": cxc.fecha, "moneda": cxc.moneda,
            "tasa_cambio": _f(cxc.tasa_cambio), "kg": round(sum(_f(l.kg_liquidados) for l in liqs), 2),
            "total": _f(cxc.total), "total_dop": _f(cxc.total_dop), "costo": round(costo, 2),
            "margen_dop": round(_f(cxc.total_dop) - costo, 2), "liquidaciones": [l.numero for l in liqs]}


@router.post("/despachos/{despacho_id}/liquidacion")
def liquidar_despacho(despacho_id: int, data: LiquidacionIn, db: Session = Depends(get_db),
                      current_user: models.Usuario = Depends(auth.require_supervisor)):
    """Registra la liquidación del cliente; con `facturar`, emite también su factura."""
    d = db.query(models.DespachoFruta).get(despacho_id)
    if not d:
        raise HTTPException(404, "Despacho no encontrado")
    if data.facturar and not (data.ncf or "").strip():
        raise HTTPException(400, "Indique el NCF, o deje la liquidación por facturar para agruparla con otras")

    def registrar():
        liq = _registrar_liquidacion(db, d, data, current_user)
        if data.facturar:
            _facturar(db, [liq], FacturaVentaDatos(ncf=data.ncf, fecha=data.fecha, tasa_cambio=data.tasa_cambio,
                                                   fecha_vencimiento=data.fecha_vencimiento), current_user)
        return liq

    liq = _confirmar(db, registrar, "Error al registrar la liquidación")
    db.refresh(liq)
    return _liquidacion_out(db, liq)


@router.post("/facturas")
def facturar_liquidaciones(data: FacturaVentaIn, db: Session = Depends(get_db),
                           current_user: models.Usuario = Depends(auth.require_supervisor)):
    """Una factura por varias liquidaciones del mismo cliente (p. ej. la factura semanal)."""
    liqs = db.query(models.LiquidacionVenta).filter(models.LiquidacionVenta.id.in_(data.liquidacion_ids)).order_by(
        models.LiquidacionVenta.fecha, models.LiquidacionVenta.id).all()
    if len(liqs) != len(set(data.liquidacion_ids)):
        raise HTTPException(400, "Alguna liquidación no existe")
    cxc = _confirmar(db, lambda: _facturar(db, liqs, data, current_user), "Error al emitir la factura")
    return _factura_out(db, cxc)


def _anular_factura(db: Session, cxc, liqs: list, motivo: str, current_user):
    """Revierte venta y costo de una factura de liquidaciones sin cobros; sus liquidaciones
    quedan otra vez por facturar. Sin confirmar."""
    if cxc.estado == "anulada":
        raise HTTPException(400, "La factura ya está anulada")
    if db.query(models.Cobro).filter(models.Cobro.cxc_id == cxc.id).count():
        raise HTTPException(400, f"La factura {cxc.ncf or cxc.numero} ya tiene cobros registrados y no se puede anular")
    texto = f"Anulación factura {cxc.ncf or cxc.numero}: {motivo}"
    for aid in {cxc.asiento_id, *(l.asiento_costo_id for l in liqs)}:
        a = db.query(models.AsientoContable).get(aid) if aid else None
        if a and a.estado not in ("anulado", "revertido"):
            _reversar_asiento(db, a, texto, current_user.nombre)
    cxc.estado = "anulada"
    cxc.saldo_pendiente = 0
    for l in liqs:
        l.cxc_id = l.asiento_costo_id = l.tasa_cambio = None
        l.venta_dop = 0
    audit.log(db, current_user, "ANULAR", "FACTURA_VENTA", cxc.numero, texto,
              {"liquidaciones": [l.numero for l in liqs]})


@router.post("/facturas/{cxc_id}/anular")
def anular_factura_venta(cxc_id: int, motivo: str = Query(..., min_length=5), db: Session = Depends(get_db),
                         current_user: models.Usuario = Depends(auth.require_supervisor)):
    """Anula una factura de liquidaciones; sus liquidaciones vuelven a por facturar."""
    cxc = db.query(models.CuentaPorCobrar).get(cxc_id)
    liqs = db.query(models.LiquidacionVenta).filter(models.LiquidacionVenta.cxc_id == cxc_id).all() if cxc else []
    if not cxc or not liqs:
        raise HTTPException(404, "Factura de liquidaciones no encontrada")
    _confirmar(db, lambda: _anular_factura(db, cxc, liqs, motivo, current_user), "Error al anular la factura")
    return {"ok": True, "cxc": cxc.numero, "liquidaciones_por_facturar": [l.numero for l in liqs]}


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
    """Anula una liquidación; el despacho vuelve a por liquidar.

    Si está facturada sola, anula también su factura; si su factura agrupa otras
    liquidaciones, hay que anular primero la factura.
    """
    liq = db.query(models.LiquidacionVenta).get(liq_id)
    if not liq or liq.estado != "activa":
        raise HTTPException(404, "Liquidación no encontrada o ya anulada")
    cxc = db.query(models.CuentaPorCobrar).get(liq.cxc_id) if liq.cxc_id else None
    if cxc:
        otras = db.query(models.LiquidacionVenta).filter(models.LiquidacionVenta.cxc_id == cxc.id,
                                                         models.LiquidacionVenta.id != liq.id).count()
        if otras:
            raise HTTPException(400, (
                f"La liquidación está en la factura {cxc.ncf or cxc.numero} junto con {otras} más: "
                "anule primero la factura"))

    def anular():
        if cxc:
            _anular_factura(db, cxc, [liq], motivo, current_user)
        liq.estado = "anulada"
        liq.despacho.estado = "despachado"
        audit.log(db, current_user, "ANULAR", "LIQUIDACION", liq.numero, f"Anulación liquidación {liq.numero}: {motivo}")

    _confirmar(db, anular, "Error al anular la liquidación")
    return {"ok": True, "numero": liq.numero, "estado": "anulada"}


# ─── Importar liquidaciones de la planta ─────────────────────────────────────

class FilaImport(BaseModel):
    model_config = ConfigDict(coerce_numbers_to_str=True)   # factura, referencia y campo pueden venir como número
    fecha: date
    factura: str
    referencia: str
    calibre: str
    campo: str
    precio: float
    kg: float


class FacturaImport(BaseModel):
    ncf: Optional[str] = None
    tasa_cambio: Optional[float] = None
    fecha: Optional[date] = None
    fecha_vencimiento: Optional[date] = None


class ImportarIn(BaseModel):
    cliente_id: int
    moneda: str = "USD"
    temporada: Optional[str] = None
    campos: Dict[str, str]                     # campo del archivo -> id_campo del sistema
    calibres: Dict[str, Optional[int]] = {}    # calibre del archivo -> calibre_id; sin él, se crea
    facturas: Dict[str, FacturaImport]         # número de factura del archivo -> sus datos
    registrar_cosecha: bool = True
    forzar_carencia: bool = False
    justificacion_carencia: Optional[str] = None
    filas: List[FilaImport]


def _nombre_calibre(texto: str) -> str:
    """'Aguacate Hass Calibre 10/12' -> 'Cal 10/12'; 'Aguacate Hass Industria' -> 'Industria'."""
    t = " ".join(texto.split())
    m = re.search(r"calibre\s+(.+)$", t, re.IGNORECASE)
    if m:
        return f"Cal {m.group(1).strip()}"[:50]
    if "industria" in t.lower():
        return "Industria"
    return t[:50]


@router.post("/importar-liquidaciones")
def importar_liquidaciones(data: ImportarIn, dry_run: bool = Query(True), db: Session = Depends(get_db),
                           current_user: models.Usuario = Depends(auth.require_supervisor)):
    """Carga las liquidaciones de la planta tal como vienen en su reporte.

    Por cada recepción y campo crea la cosecha a granel (opcional), el despacho y la
    liquidación con los kg y precios del reporte; por cada número de factura, una factura
    que agrupa sus liquidaciones. Todo o nada; en modo prueba solo devuelve el resumen.
    """
    from routers.cosecha import CosechaIn, CosechaLineaIn, _registrar_cosecha
    cli = db.query(models.Cliente).get(data.cliente_id)
    if not cli or cli.activo is False:
        raise HTTPException(400, "Cliente no existe o está inactivo")
    moneda = data.moneda.upper()
    if not data.filas:
        raise HTTPException(400, "No hay filas que importar")

    errores = []
    for txt in sorted({f.campo for f in data.filas}):
        cid = data.campos.get(txt)
        if not cid:
            errores.append(f"Indique a qué campo del sistema corresponde el campo {txt} del archivo")
        elif not db.query(models.Campo).filter(models.Campo.id_campo == cid).first():
            errores.append(f"El campo {cid} no existe")
    for txt in sorted({f.calibre for f in data.filas}):
        cid = data.calibres.get(txt)
        if cid:
            cal = db.query(models.Calibre).get(int(cid))
            if not cal or cal.es_granel:
                errores.append(f"El calibre elegido para '{txt}' no existe o es a granel")
    for num in sorted({f.factura for f in data.filas}):
        fi = data.facturas.get(num)
        if not fi or not (fi.ncf or "").strip():
            errores.append(f"Indique el NCF de la factura {num}")
        elif moneda == "USD" and not (fi.tasa_cambio or 0) > 1:
            errores.append(f"Indique la tasa de cambio de la factura {num}")
    granel = db.query(models.Calibre).filter(models.Calibre.es_granel == True, models.Calibre.activo == True,
                                             models.Calibre.producto_id.isnot(None)).order_by(models.Calibre.orden).first()
    if not granel:
        errores.append("Configure en Cosecha → Calibres el calibre a granel (fruta sin clasificar) con su producto")

    grupos: dict = {}
    for f in data.filas:
        grupos.setdefault((f.referencia.strip(), f.campo), []).append(f)
    for (ref, campo_txt), filas in grupos.items():
        if len({f.factura for f in filas}) > 1:
            errores.append(f"La recepción {ref} del campo {campo_txt} aparece en más de una factura")
        if any(f.kg <= 0 or f.precio < 0 for f in filas):
            errores.append(f"La recepción {ref} del campo {campo_txt} tiene kg o precios inválidos")
        cid = data.campos.get(campo_txt)
        if cid and db.query(models.DespachoFruta).filter(
                models.DespachoFruta.cliente_id == cli.id, models.DespachoFruta.conduce == ref,
                models.DespachoFruta.campo_id == cid, models.DespachoFruta.estado != "anulado").first():
            errores.append(f"La recepción {ref} del campo {campo_txt} ya está cargada")
    if errores:
        # Un punto por línea: la pantalla de importación los muestra como lista.
        raise HTTPException(400, f"Hay {len(errores)} punto(s) por resolver antes de importar:" + "".join(SALTO + e for e in errores))

    resumen = {"dry_run": dry_run, "cliente": cli.nombre, "moneda": moneda, "recepciones": len(grupos),
               "cosechas": 0, "despachos": 0, "liquidaciones": 0, "calibres_creados": [], "facturas": []}
    try:
        # Calibres comerciales que faltan, en el orden del archivo
        cal_ids: dict = {}
        orden = (db.query(sqlfunc.max(models.Calibre.orden)).scalar() or 0)
        for txt in dict.fromkeys(f.calibre for f in data.filas):
            if data.calibres.get(txt):
                cal_ids[txt] = int(data.calibres[txt])
                continue
            nombre = _nombre_calibre(txt)
            cal = db.query(models.Calibre).filter(models.Calibre.nombre == nombre).first()
            if not cal:
                orden += 1
                cal = models.Calibre(nombre=nombre, orden=orden, es_granel=False, activo=True)
                db.add(cal)
                db.flush()
                resumen["calibres_creados"].append(nombre)
            elif cal.es_granel:
                raise HTTPException(400, f"'{nombre}' existe como calibre a granel: elija otro para '{txt}'")
            cal.activo = True
            cal_ids[txt] = cal.id

        liq_por_factura: dict = {}
        for (ref, campo_txt), filas in sorted(grupos.items(), key=lambda g: (min(f.fecha for f in g[1]), g[0])):
            campo_id = data.campos[campo_txt]
            fecha = min(f.fecha for f in filas)
            temporada = (data.temporada or "").strip() or str(fecha.year)
            kg_total = round(sum(f.kg for f in filas), 2)
            contexto = f"Recepción {ref}, campo {campo_txt}"
            try:
                if data.registrar_cosecha:
                    _registrar_cosecha(db, CosechaIn(
                        fecha=fecha, campo_id=campo_id, temporada=temporada,
                        lineas=[CosechaLineaIn(calibre_id=granel.id, kg=kg_total)],
                        observaciones=f"Cargada desde la liquidación {ref} de {cli.nombre}",
                        forzar_carencia=data.forzar_carencia, justificacion_carencia=data.justificacion_carencia,
                    ), current_user)
                    resumen["cosechas"] += 1
                d = _registrar_despacho(db, DespachoIn(
                    cliente_id=cli.id, fecha=fecha, campo_id=campo_id, temporada=temporada, conduce=ref,
                    lineas=[DespachoLineaIn(calibre_id=granel.id, kg=kg_total)],
                    observaciones=f"Recepción {ref} de {cli.nombre}"), current_user)
                resumen["despachos"] += 1
                lineas: dict = {}
                for f in filas:
                    cid = cal_ids[f.calibre]
                    if cid in lineas and lineas[cid][1] != f.precio:
                        raise HTTPException(400, f"El calibre '{f.calibre}' aparece dos veces con precios distintos")
                    lineas[cid] = (lineas.get(cid, (0, f.precio))[0] + f.kg, f.precio)
                liq = _registrar_liquidacion(db, d, LiquidacionIn(
                    fecha=fecha, referencia_cliente=ref, moneda=moneda, kg_rechazo=0, facturar=False,
                    lineas=[LiquidacionLineaIn(calibre_id=cid, kg=round(kg, 2), precio=precio)
                            for cid, (kg, precio) in lineas.items()]), current_user)
                resumen["liquidaciones"] += 1
            except HTTPException as e:
                raise HTTPException(e.status_code, f"{contexto}: {e.detail}")
            liq_por_factura.setdefault(filas[0].factura, []).append(liq)

        for num, liqs in liq_por_factura.items():
            fi = data.facturas[num]
            try:
                cxc = _facturar(db, liqs, FacturaVentaDatos(ncf=fi.ncf, fecha=fi.fecha, tasa_cambio=fi.tasa_cambio,
                                                            fecha_vencimiento=fi.fecha_vencimiento), current_user)
            except HTTPException as e:
                raise HTTPException(e.status_code, f"Factura {num}: {e.detail}")
            db.flush()
            resumen["facturas"].append({"factura": num, **_factura_out(db, cxc)})

        resumen["kg"] = round(sum(f["kg"] for f in resumen["facturas"]), 2)
        resumen["total"] = round(sum(f["total"] for f in resumen["facturas"]), 2)
        resumen["total_dop"] = round(sum(f["total_dop"] for f in resumen["facturas"]), 2)
        if dry_run:
            db.rollback()
        else:
            audit.log(db, current_user, "IMPORTAR", "LIQUIDACION", cli.nombre,
                      f"Importadas {resumen['liquidaciones']} liquidaciones y {len(resumen['facturas'])} facturas "
                      f"de {cli.nombre}: {resumen['kg']:,.2f} kg, {moneda} {resumen['total']:,.2f}",
                      {k: v for k, v in resumen.items() if k != "facturas"})
            db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        import logging
        logging.getLogger(__name__).exception("Error importando liquidaciones")
        raise HTTPException(500, "Error al importar las liquidaciones")
    return resumen


# ─── Reportes ────────────────────────────────────────────────────────────────

def _saldo_cuenta(db: Session, cuenta_id: int) -> float:
    debe, haber = db.query(sqlfunc.coalesce(sqlfunc.sum(models.LineaAsiento.debe), 0),
                           sqlfunc.coalesce(sqlfunc.sum(models.LineaAsiento.haber), 0)).join(
        models.AsientoContable, models.AsientoContable.id == models.LineaAsiento.asiento_id).filter(
        models.LineaAsiento.cuenta_id == cuenta_id, models.AsientoContable.estado != "anulado").one()
    return round(float(debe or 0) - float(haber or 0), 2)


@router.get("/pendientes")
def despachos_pendientes(db: Session = Depends(get_db), _=Depends(auth.get_current_user)):
    """Despachos que el cliente aún no liquida y liquidaciones aún sin facturar, contra el saldo
    de la cuenta de fruta despachada (su costo sale de ella al facturar)."""
    items = [_despacho_out(db, d) for d in db.query(models.DespachoFruta).filter(
        models.DespachoFruta.estado == "despachado").order_by(models.DespachoFruta.fecha).all()]
    por_facturar = db.query(models.LiquidacionVenta).filter(
        models.LiquidacionVenta.estado == "activa", models.LiquidacionVenta.cxc_id.is_(None)).all()
    costo_pf = round(sum(_f(l.despacho.costo_total) for l in por_facturar), 2)
    r_desp = _get_regla_cuentas(db, "venta", "despacho_por_liquidar")
    total = round(sum(i["costo_total"] for i in items), 2)
    saldo = _saldo_cuenta(db, r_desp[0]) if r_desp else None
    return {"kg": round(sum(i["kg_total"] for i in items), 2), "costo": total, "saldo_mayor": saldo,
            "liquidaciones_por_facturar": len(por_facturar), "costo_por_facturar": costo_pf,
            "diferencia": round(total + costo_pf - saldo, 2) if saldo is not None else None, "items": items}


def _rango_temporada(db: Session, temporada: str):
    """Fechas de la temporada: el año si es un año ("2026"); si no, de su primera a su última cosecha."""
    if temporada.isdigit() and len(temporada) == 4:
        return date(int(temporada), 1, 1), date(int(temporada), 12, 31)
    desde, hasta = db.query(sqlfunc.min(models.Cosecha.fecha), sqlfunc.max(models.Cosecha.fecha)).filter(
        models.Cosecha.temporada == temporada, models.Cosecha.estado != "anulada").one()
    return desde, hasta


@router.get("/rentabilidad")
def rentabilidad(temporada: Optional[str] = None, db: Session = Depends(get_db),
                 _=Depends(auth.get_current_user)):
    """Por campo y por calibre: cosechado, despachado, liquidado, rechazo, venta, costo y margen."""
    temporada = temporada or str(date.today().year)
    campos: dict = {}

    def campo(cid):
        return campos.setdefault(cid or "—", {
            "campo_id": cid, "kg_cosechados": 0.0, "kg_despachados": 0.0, "kg_liquidados": 0.0,
            "kg_rechazo": 0.0, "kg_merma": 0.0, "venta_dop": 0.0, "costo": 0.0, "kg_por_liquidar": 0.0,
            "kg_desp_liquidados": 0.0, "kg_por_facturar": 0.0, "venta_por_facturar": 0.0,
            "kg_desp_facturados": 0.0})

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
        fila["kg_desp_liquidados"] += _f(d.kg_total)
        fila["kg_liquidados"] += _f(liq.kg_liquidados)
        fila["kg_rechazo"] += _f(liq.kg_rechazo)
        fila["kg_merma"] += _f(liq.kg_merma)
        # La venta y el costo se reconocen al facturar; antes solo se sabe en la moneda del cliente.
        if liq.cxc_id:
            fila["kg_desp_facturados"] += _f(d.kg_total)
            fila["venta_dop"] += _f(liq.venta_dop)
            fila["costo"] += _f(liq.costo_venta) + _f(liq.costo_rechazo)
        else:
            fila["kg_por_facturar"] += _f(liq.kg_liquidados)
            fila["venta_por_facturar"] += _f(liq.subtotal)
        for x in liq.lineas:
            k = por_calibre.setdefault(x.calibre_id, {"calibre_id": x.calibre_id,
                                                     "calibre": x.calibre.nombre if x.calibre else None,
                                                     "orden": x.calibre.orden if x.calibre else 0,
                                                     "kg": 0.0, "venta": 0.0, "venta_dop": 0.0})
            k["kg"] += _f(x.kg)
            k["venta"] += _f(x.subtotal)
            if liq.cxc_id:
                k["venta_dop"] += _f(x.subtotal) * _f(liq.tasa_cambio or 1)

    # Costo real de producción del campo en la temporada: sus OT (mano de obra, insumos y
    # equipo) y los servicios comprados para él. El margen de cada liquidación usa el costo
    # estándar de la fruta; este es el que dice si el campo ganó o perdió.
    desde, hasta = _rango_temporada(db, temporada)
    if desde:
        for campo_id, costo in db.query(models.OrdenTrabajo.campo_id,
                                        sqlfunc.coalesce(sqlfunc.sum(models.OrdenTrabajo.costo_total), 0)).filter(
                models.OrdenTrabajo.campo_id.isnot(None),
                models.OrdenTrabajo.fecha_ejecucion >= datetime.combine(desde, datetime.min.time()),
                models.OrdenTrabajo.fecha_ejecucion <= datetime.combine(hasta, datetime.max.time()),
        ).group_by(models.OrdenTrabajo.campo_id).all():
            campo(campo_id)["costo_produccion"] = campo(campo_id).get("costo_produccion", 0.0) + _f(costo)
        for l, oc in db.query(models.OrdenCompraLinea, models.OrdenCompra).join(
                models.OrdenCompra, models.OrdenCompra.oc_id == models.OrdenCompraLinea.oc_id).join(
                models.Producto, models.Producto.id_prod == models.OrdenCompraLinea.producto_id).filter(
                models.OrdenCompra.campo_id.isnot(None), models.OrdenCompra.estado != "Cancelada",
                models.Producto.es_inventariable == False,
                models.OrdenCompra.fecha >= datetime.combine(desde, datetime.min.time()),
                models.OrdenCompra.fecha <= datetime.combine(hasta, datetime.max.time())).all():
            servicio = _f(l.cantidad_recibida) * _f(l.precio_unitario) * (1 - _f(l.descuento_pct) / 100)
            campo(oc.campo_id)["costo_produccion"] = campo(oc.campo_id).get("costo_produccion", 0.0) + servicio

    nombres = {c.id_campo: c.nombre for c in db.query(models.Campo).all()}
    filas = []
    for f in campos.values():
        f["campo"] = nombres.get(f["campo_id"], "Sin campo") if f["campo_id"] else "Sin campo"
        f["margen_dop"] = round(f["venta_dop"] - f["costo"], 2)
        f["costo_produccion"] = round(f.get("costo_produccion", 0.0), 2)
        f["costo_kg"] = round(f["costo_produccion"] / f["kg_cosechados"], 2) if f["kg_cosechados"] else None
        f["resultado"] = round(f["venta_dop"] - f["costo_produccion"], 2)
        # Sobre lo ya liquidado: lo que aún está en la planta no tiene clasificación.
        base = f["kg_desp_liquidados"]
        f["pct_rechazo"] = round(f["kg_rechazo"] / base * 100, 1) if base else None
        # Packout: parte de lo despachado que la planta paga; retorno: pesos por kg despachado.
        f["packout_pct"] = round(f["kg_liquidados"] / base * 100, 1) if base else None
        fac = f["kg_desp_facturados"]
        f["retorno_kg"] = round(f["venta_dop"] / fac, 2) if fac else None
        for k in ("kg_cosechados", "kg_despachados", "kg_liquidados", "kg_rechazo", "kg_merma",
                  "venta_dop", "costo", "kg_por_liquidar", "kg_desp_liquidados", "kg_por_facturar",
                  "venta_por_facturar", "kg_desp_facturados"):
            f[k] = round(f[k], 2)
        filas.append(f)
    calibres = sorted(por_calibre.values(), key=lambda k: (k["orden"] or 0, k["calibre"] or ""))
    kg_cal_total = sum(k["kg"] for k in calibres)
    for k in calibres:
        k["precio_promedio"] = round(k["venta"] / k["kg"], 4) if k["kg"] else None
        k["pct"] = round(k["kg"] / kg_cal_total * 100, 1) if kg_cal_total else None
        k["kg"], k["venta"], k["venta_dop"] = round(k["kg"], 2), round(k["venta"], 2), round(k["venta_dop"], 2)
    tot = {k: round(sum(f[k] for f in filas), 2) for k in
           ("kg_cosechados", "kg_despachados", "kg_liquidados", "kg_rechazo", "kg_merma",
            "venta_dop", "costo", "margen_dop", "kg_por_liquidar", "costo_produccion", "resultado",
            "kg_desp_liquidados", "kg_por_facturar", "venta_por_facturar", "kg_desp_facturados")}
    tot["costo_kg"] = round(tot["costo_produccion"] / tot["kg_cosechados"], 2) if tot["kg_cosechados"] else None
    b = tot["kg_desp_liquidados"]
    tot["packout_pct"] = round(tot["kg_liquidados"] / b * 100, 1) if b else None
    tot["pct_rechazo"] = round(tot["kg_rechazo"] / b * 100, 1) if b else None
    tot["retorno_kg"] = round(tot["venta_dop"] / tot["kg_desp_facturados"], 2) if tot["kg_desp_facturados"] else None
    return {"temporada": temporada, "moneda_venta": sorted(monedas)[0] if len(monedas) == 1 else None,
            "periodo_costos": {"desde": desde, "hasta": hasta},
            "por_campo": sorted(filas, key=lambda f: f["campo"]), "por_calibre": calibres, "totales": tot}
