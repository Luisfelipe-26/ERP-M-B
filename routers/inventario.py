from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func
from database import get_db
import models, schemas, auth
from routers.sequences import get_next, peek_next
from routers.contabilidad import _crear_asiento_auto, _get_regla_cuentas
from typing import List, Optional
from datetime import datetime
import audit

router = APIRouter(prefix="/api/inventario", tags=["inventario"])

MOTIVOS_GI = ["Merma", "Vencimiento", "Devolucion Proveedor", "Muestra", "Uso No Productivo", "Consumo OT", "Otro"]


def _recalc_avg_cost(producto: models.Producto, qty_in: float, precio_compra: float) -> float:
    """Weighted average cost recalculation on goods receipt."""
    stock = producto.stock_actual or 0
    costo_actual = producto.costo_promedio or producto.costo_unitario or 0
    if stock + qty_in == 0:
        return precio_compra
    return ((stock * costo_actual) + (qty_in * precio_compra)) / (stock + qty_in)


def _mov_out(m: models.MovimientoInventario) -> dict:
    return {
        "id": m.id,
        "num_documento": m.num_documento,
        "producto_id": m.producto_id,
        "tipo_doc": m.tipo_doc,
        "tipo": m.tipo,
        "motivo": m.motivo,
        "cantidad": m.cantidad,
        "costo_unitario": m.costo_unitario,
        "costo_promedio_post": m.costo_promedio_post,
        "stock_post": m.stock_post,
        "lote": m.lote,
        "vencimiento": m.vencimiento.isoformat() if m.vencimiento else None,
        "proveedor": m.proveedor,
        "num_factura": m.num_factura,
        "referencia": m.referencia,
        "ot_referencia": m.ot_referencia,
        "oc_referencia": m.oc_referencia,
        "observacion": m.observacion,
        "fecha": m.fecha.isoformat() if m.fecha else None,
        "usuario_id": m.usuario_id,
        "producto_nombre": m.producto.producto if m.producto else None,
        "producto_unidad": m.producto.unidad if m.producto else None,
    }


# ─── Sugerencia de próximo ID ───────────────────────────────────────────────

@router.get("/articulos/next-id")
def next_id_articulo(db: Session = Depends(get_db), _=Depends(auth.get_current_user)):
    return {"next_id": peek_next("PROD", db)}


@router.get("/ordenes-trabajo-lista")
def list_ots_for_select(db: Session = Depends(get_db), _=Depends(auth.get_current_user)):
    """OTs activas para selector de salida de mercancía."""
    ots = db.query(models.OrdenTrabajo).filter(
        models.OrdenTrabajo.estado.in_(["Abierta", "En Proceso"])
    ).order_by(models.OrdenTrabajo.ot_id.desc()).limit(100).all()
    return [{"ot_id": o.ot_id, "campo_id": o.campo_id, "actividad_id": o.actividad_id,
             "fecha": o.fecha_ejecucion.isoformat() if o.fecha_ejecucion else None,
             "label": f"OT-{o.ot_id} · {o.campo_id} · {o.actividad_id}"} for o in ots]


@router.get("/ordenes-compra-lista")
def list_ocs_for_select(db: Session = Depends(get_db), _=Depends(auth.get_current_user)):
    """OCs pendientes/parciales para selector de entrada de mercancía."""
    ocs = db.query(models.OrdenCompra).filter(
        models.OrdenCompra.estado.in_(["Pendiente", "Parcial"])
    ).order_by(models.OrdenCompra.fecha.desc()).limit(100).all()
    return [{"oc_id": o.oc_id, "proveedor": o.proveedor,
             "fecha": o.fecha.isoformat() if o.fecha else None,
             "label": f"{o.oc_id} · {o.proveedor or 'Sin proveedor'}"} for o in ocs]


# ─── Listado de productos (artículos maestros) ──────────────────────────────

@router.get("/articulos")
def list_articulos(db: Session = Depends(get_db), _=Depends(auth.get_current_user)):
    productos = db.query(models.Producto).filter(models.Producto.activo == True).order_by(models.Producto.tipo, models.Producto.producto).all()
    result = []
    for p in productos:
        cp = p.costo_promedio or p.costo_unitario or 0
        result.append({
            "id": p.id,
            "id_prod": p.id_prod,
            "producto": p.producto,
            "tipo": p.tipo,
            "unidad": p.unidad,
            "costo_unitario": p.costo_unitario,
            "costo_promedio": round(cp, 4),
            "stock_actual": p.stock_actual or 0,
            "stock_minimo": p.stock_minimo or 0,
            "stock_maximo": p.stock_maximo,
            "proveedor": p.proveedor,
            "concentracion": p.concentracion,
            "activo": p.activo,
            "es_inventariable": p.es_inventariable if p.es_inventariable is not None else True,
            "valor_inventario": round((p.stock_actual or 0) * cp, 2),
            "bajo_minimo": bool(p.stock_minimo and p.stock_actual is not None and p.stock_actual <= p.stock_minimo),
        })
    return result


@router.post("/articulos", response_model=schemas.ProductoOut)
def create_articulo(data: schemas.ProductoCreate, db: Session = Depends(get_db), _=Depends(auth.require_operador)):
    d = data.model_dump()
    if not d.get("id_prod"):
        d["id_prod"] = get_next("PROD", db)
    elif db.query(models.Producto).filter(models.Producto.id_prod == d["id_prod"]).first():
        raise HTTPException(status_code=400, detail="ID de producto ya existe")
    if not d.get("costo_promedio") and d.get("costo_unitario"):
        d["costo_promedio"] = d["costo_unitario"]
    p = models.Producto(**d)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


@router.put("/articulos/{id_prod}", response_model=schemas.ProductoOut)
def update_articulo(id_prod: str, data: schemas.ProductoCreate, db: Session = Depends(get_db), _=Depends(auth.require_admin)):
    p = db.query(models.Producto).filter(models.Producto.id_prod == id_prod).first()
    if not p:
        raise HTTPException(status_code=404, detail="Producto no encontrado")
    payload = data.model_dump()
    payload.pop("stock_actual", None)      # stock_actual solo se toca vía movimientos
    payload.pop("costo_promedio", None)    # costo promedio solo se toca vía GR
    # C-5 FIX: validar stock_minimo >= 0
    if payload.get("stock_minimo") is not None and payload["stock_minimo"] < 0:
        raise HTTPException(status_code=400, detail="Stock mínimo no puede ser negativo")
    for k, v in payload.items():
        setattr(p, k, v)
    db.commit()
    db.refresh(p)
    return p


@router.delete("/articulos/{id_prod}")
def delete_articulo(id_prod: str, db: Session = Depends(get_db), _=Depends(auth.require_admin)):
    p = db.query(models.Producto).filter(models.Producto.id_prod == id_prod).first()
    if not p:
        raise HTTPException(status_code=404, detail="Producto no encontrado")
    p.activo = False
    db.commit()
    return {"ok": True}


# ─── Goods Receipt (GR) — Entrada de Mercancía ──────────────────────────────

@router.post("/gr")
def goods_receipt(data: schemas.GRCreate, db: Session = Depends(get_db),
                  current_user: models.Usuario = Depends(auth.require_operador)):
    if data.cantidad <= 0:
        raise HTTPException(status_code=400, detail="La cantidad debe ser mayor a 0")
    if data.precio_compra < 0:
        raise HTTPException(status_code=400, detail="El precio de compra no puede ser negativo")

    p = db.query(models.Producto).filter(models.Producto.id_prod == data.producto_id, models.Producto.activo == True).first()
    if not p:
        raise HTTPException(status_code=404, detail="Artículo no encontrado")
    if not p.es_inventariable:
        raise HTTPException(status_code=400, detail=f"'{p.producto}' es un servicio — no transacciona en inventario")

    nuevo_costo_promedio = _recalc_avg_cost(p, data.cantidad, data.precio_compra)
    nuevo_stock = (p.stock_actual or 0) + data.cantidad
    num_doc = get_next("GR", db)

    mov = models.MovimientoInventario(
        num_documento=num_doc,
        producto_id=data.producto_id,
        tipo_doc="GR",
        tipo="entrada",
        motivo="Compra",
        cantidad=data.cantidad,
        costo_unitario=data.precio_compra,
        costo_promedio_post=round(nuevo_costo_promedio, 4),
        stock_post=nuevo_stock,
        lote=data.lote,
        vencimiento=data.vencimiento,
        proveedor=data.proveedor or p.proveedor,
        num_factura=data.num_factura,
        oc_referencia=data.orden_compra_id,
        observacion=data.observacion,
        fecha=data.fecha or datetime.now(),
        usuario_id=current_user.id,
    )
    db.add(mov)

    p.stock_actual = nuevo_stock
    p.costo_promedio = round(nuevo_costo_promedio, 4)

    # Recepción parcial de OC: actualizar cantidad_recibida en la línea de la OC
    if data.orden_compra_id:
        oc_linea = db.query(models.OrdenCompraLinea).filter(
            models.OrdenCompraLinea.oc_id == data.orden_compra_id,
            models.OrdenCompraLinea.producto_id == data.producto_id,
        ).first()
        if oc_linea:
            oc_linea.cantidad_recibida = round((oc_linea.cantidad_recibida or 0) + data.cantidad, 4)
            # Check if all lines are fully received → update OC status
            oc = db.query(models.OrdenCompra).filter(models.OrdenCompra.oc_id == data.orden_compra_id).first()
            if oc:
                all_lines = db.query(models.OrdenCompraLinea).filter(
                    models.OrdenCompraLinea.oc_id == data.orden_compra_id).all()
                all_complete = all(l.cantidad_recibida >= l.cantidad for l in all_lines)
                any_received = any((l.cantidad_recibida or 0) > 0 for l in all_lines)
                if all_complete:
                    oc.estado = "Recibida"
                elif any_received:
                    oc.estado = "Parcial"

    audit.log(db, current_user, "CREAR", "GR", num_doc,
              f"Entrada {num_doc}: {data.cantidad} {p.unidad} de {p.producto} a RD$ {data.precio_compra}",
              {"producto": data.producto_id, "cantidad": data.cantidad, "precio": data.precio_compra,
               "costo_promedio_new": round(nuevo_costo_promedio, 4), "oc": data.orden_compra_id})

    monto = round(data.cantidad * data.precio_compra, 2)
    if monto > 0 and p.cuenta_inventario_id:
        r_inv = _get_regla_cuentas(db, "inventario", "entrada")
        cta_debe = p.cuenta_inventario_id
        cta_haber = r_inv[1] if r_inv else p.cuenta_costo_id
        if cta_debe and cta_haber:
            fecha_asiento = data.fecha or datetime.now()
            if hasattr(fecha_asiento, 'date'):
                fecha_asiento = fecha_asiento.date()
            asiento = _crear_asiento_auto(
                db, fecha_asiento,
                "GR", num_doc,
                f"Entrada inventario {num_doc} — {p.producto}",
                [
                    {"cuenta_id": cta_debe, "debe": monto, "haber": 0,
                     "almacen_id": getattr(data, 'almacen_id', None) or mov.almacen_id,
                     "descripcion_linea": f"Inventario entrada {p.producto}"},
                    {"cuenta_id": cta_haber, "debe": 0, "haber": monto,
                     "descripcion_linea": f"Contrapartida GR {num_doc}"},
                ],
                current_user.nombre, origen_id=mov.id
            )
            if asiento:
                mov.asiento_id = asiento.id

    db.commit()
    db.refresh(mov)
    return _mov_out(mov)


# ─── Goods Issue (GI) — Salida de Mercancía ─────────────────────────────────

@router.post("/gi")
def goods_issue(data: schemas.GICreate, db: Session = Depends(get_db),
                current_user: models.Usuario = Depends(auth.require_supervisor)):
    if data.cantidad <= 0:
        raise HTTPException(status_code=400, detail="La cantidad debe ser mayor a 0")

    # H-3 FIX: Validar motivo contra lista permitida
    if data.motivo not in MOTIVOS_GI:
        raise HTTPException(status_code=400,
            detail=f"Motivo '{data.motivo}' no válido. Permitidos: {', '.join(MOTIVOS_GI)}")

    p = db.query(models.Producto).filter(models.Producto.id_prod == data.producto_id, models.Producto.activo == True).first()
    if not p:
        raise HTTPException(status_code=404, detail="Artículo no encontrado")
    if not p.es_inventariable:
        raise HTTPException(status_code=400, detail=f"'{p.producto}' es un servicio — no transacciona en inventario")

    if (p.stock_actual or 0) < data.cantidad:
        raise HTTPException(status_code=400, detail=f"Stock insuficiente. Disponible: {p.stock_actual} {p.unidad}")

    nuevo_stock = (p.stock_actual or 0) - data.cantidad
    cp = p.costo_promedio or p.costo_unitario or 0
    num_doc = get_next("GI", db)

    mov = models.MovimientoInventario(
        num_documento=num_doc,
        producto_id=data.producto_id,
        tipo_doc="GI",
        tipo="salida",
        motivo=data.motivo,
        cantidad=data.cantidad,
        costo_unitario=cp,
        costo_promedio_post=cp,
        stock_post=nuevo_stock,
        referencia=data.referencia,
        ot_referencia=data.ot_id,
        observacion=data.observacion,
        fecha=data.fecha or datetime.now(),
        usuario_id=current_user.id,
    )
    db.add(mov)
    p.stock_actual = nuevo_stock

    audit.log(db, current_user, "CREAR", "GI", num_doc,
              f"Salida {num_doc}: {data.cantidad} {p.unidad} de {p.producto} — Motivo: {data.motivo}",
              {"producto": data.producto_id, "cantidad": data.cantidad, "motivo": data.motivo,
               "ot_id": data.ot_id})

    monto = round(data.cantidad * cp, 2)
    if monto > 0 and p.cuenta_inventario_id and p.cuenta_costo_id:
        r_inv = _get_regla_cuentas(db, "inventario", "salida")
        cta_debe = r_inv[0] if r_inv else p.cuenta_costo_id
        cta_haber = p.cuenta_inventario_id
        asiento = _crear_asiento_auto(
            db, datetime.now().date(), "GI", num_doc,
            f"Salida inventario {num_doc} — {p.producto} ({data.motivo})",
            [
                {"cuenta_id": cta_debe, "debe": monto, "haber": 0,
                 "descripcion_linea": f"Costo salida {p.producto} — {data.motivo}"},
                {"cuenta_id": cta_haber, "debe": 0, "haber": monto,
                 "almacen_id": mov.almacen_id,
                 "descripcion_linea": f"Inventario salida {num_doc}"},
            ],
            current_user.nombre, origen_id=mov.id
        )
        if asiento:
            mov.asiento_id = asiento.id

    db.commit()
    db.refresh(mov)
    return _mov_out(mov)


# ─── Ajuste de Inventario (AJ) — Recuento Físico ────────────────────────────

@router.post("/ajuste")
def ajuste_inventario(data: schemas.AjusteCreate, db: Session = Depends(get_db),
                      current_user: models.Usuario = Depends(auth.require_supervisor)):
    if data.cantidad_contada < 0:
        raise HTTPException(status_code=400, detail="La cantidad contada no puede ser negativa")

    p = db.query(models.Producto).filter(models.Producto.id_prod == data.producto_id, models.Producto.activo == True).first()
    if not p:
        raise HTTPException(status_code=404, detail="Artículo no encontrado")
    if not p.es_inventariable:
        raise HTTPException(status_code=400, detail=f"'{p.producto}' es un servicio — no transacciona en inventario")

    diferencia = data.cantidad_contada - (p.stock_actual or 0)
    if diferencia == 0:
        raise HTTPException(status_code=400, detail="No hay diferencia entre el conteo y el stock del sistema")

    cp = p.costo_promedio or p.costo_unitario or 0
    tipo = "entrada" if diferencia > 0 else "salida"
    num_doc = get_next("AJ", db)

    mov = models.MovimientoInventario(
        num_documento=num_doc,
        producto_id=data.producto_id,
        tipo_doc="AJ",
        tipo=tipo,
        motivo="Ajuste Conteo Físico",
        cantidad=abs(diferencia),
        costo_unitario=cp,
        costo_promedio_post=cp,
        stock_post=data.cantidad_contada,
        observacion=data.observacion or f"Stock sistema: {p.stock_actual} → Conteo físico: {data.cantidad_contada}",
        fecha=data.fecha or datetime.now(),
        usuario_id=current_user.id,
    )
    db.add(mov)

    # C-7 FIX: Guardar stock anterior ANTES de sobrescribir
    stock_anterior = p.stock_actual or 0
    p.stock_actual = data.cantidad_contada

    audit.log(db, current_user, "AJUSTE", "AJ", num_doc,
              f"Ajuste {num_doc}: {p.producto} — Sistema: {stock_anterior} → Conteo: {data.cantidad_contada} (dif: {diferencia:+.2f})",
              {"producto": data.producto_id, "stock_sistema": float(stock_anterior),
               "conteo": data.cantidad_contada, "diferencia": diferencia})

    monto_ajuste = round(abs(diferencia) * cp, 2)
    if monto_ajuste > 0 and p.cuenta_inventario_id:
        r_aj = _get_regla_cuentas(db, "inventario", "ajuste")
        cta_ajuste = r_aj[0] if r_aj else p.cuenta_costo_id
        if cta_ajuste:
            if diferencia > 0:
                lineas = [
                    {"cuenta_id": p.cuenta_inventario_id, "debe": monto_ajuste, "haber": 0,
                     "descripcion_linea": f"Ajuste positivo inventario {p.producto}"},
                    {"cuenta_id": cta_ajuste, "debe": 0, "haber": monto_ajuste,
                     "descripcion_linea": f"Contrapartida ajuste {num_doc}"},
                ]
            else:
                lineas = [
                    {"cuenta_id": cta_ajuste, "debe": monto_ajuste, "haber": 0,
                     "descripcion_linea": f"Ajuste negativo inventario {p.producto}"},
                    {"cuenta_id": p.cuenta_inventario_id, "debe": 0, "haber": monto_ajuste,
                     "descripcion_linea": f"Inventario ajuste {num_doc}"},
                ]
            asiento = _crear_asiento_auto(
                db, datetime.now().date(), "AJ", num_doc,
                f"Ajuste inventario {num_doc} — {p.producto} (dif: {diferencia:+.2f})",
                lineas, current_user.nombre, origen_id=mov.id
            )
            if asiento:
                mov.asiento_id = asiento.id

    db.commit()
    db.refresh(mov)
    return _mov_out(mov)


# ─── Kardex — Historial por producto ────────────────────────────────────────

@router.get("/kardex/{id_prod}")
def get_kardex(id_prod: str, limit: int = Query(200, le=500),
               db: Session = Depends(get_db), _=Depends(auth.get_current_user)):
    p = db.query(models.Producto).filter(models.Producto.id_prod == id_prod).first()
    if not p:
        raise HTTPException(status_code=404, detail="Artículo no encontrado")

    movs = db.query(models.MovimientoInventario).filter(
        models.MovimientoInventario.producto_id == id_prod
    ).order_by(models.MovimientoInventario.fecha.asc(), models.MovimientoInventario.id.asc()).limit(limit).all()

    return {
        "producto": {
            "id_prod": p.id_prod,
            "producto": p.producto,
            "unidad": p.unidad,
            "stock_actual": p.stock_actual,
            "costo_promedio": p.costo_promedio or p.costo_unitario,
            "valor_inventario": round((p.stock_actual or 0) * (p.costo_promedio or p.costo_unitario or 0), 2),
        },
        "movimientos": [_mov_out(m) for m in movs],
    }


# ─── Movimientos (todos los productos) ──────────────────────────────────────

@router.get("/movimientos")
def list_movimientos(
    producto_id: Optional[str] = None,
    tipo_doc: Optional[str] = None,
    fecha_desde: Optional[str] = None,
    fecha_hasta: Optional[str] = None,
    limit: int = Query(500, le=1000),
    db: Session = Depends(get_db), _=Depends(auth.get_current_user)
):
    q = db.query(models.MovimientoInventario)
    if producto_id:
        q = q.filter(models.MovimientoInventario.producto_id == producto_id)
    if tipo_doc:
        q = q.filter(models.MovimientoInventario.tipo_doc == tipo_doc)
    if fecha_desde:
        q = q.filter(models.MovimientoInventario.fecha >= fecha_desde)
    if fecha_hasta:
        q = q.filter(models.MovimientoInventario.fecha <= fecha_hasta + "T23:59:59")
    movs = q.order_by(models.MovimientoInventario.fecha.desc()).limit(limit).all()
    return [_mov_out(m) for m in movs]


# ─── Valoración de Inventario ────────────────────────────────────────────────

@router.get("/valoracion")
def valoracion_inventario(db: Session = Depends(get_db), _=Depends(auth.get_current_user)):
    productos = db.query(models.Producto).filter(models.Producto.activo == True).all()
    total_valor = 0
    por_tipo = {}
    bajo_minimo = []
    sin_stock = []

    items = []
    for p in productos:
        cp = p.costo_promedio or p.costo_unitario or 0
        stock = p.stock_actual or 0
        valor = round(stock * cp, 2)
        total_valor += valor

        tipo = p.tipo or "Sin clasificar"
        por_tipo[tipo] = por_tipo.get(tipo, 0) + valor

        if p.stock_minimo and stock <= p.stock_minimo:
            bajo_minimo.append({"id_prod": p.id_prod, "producto": p.producto, "stock": stock, "minimo": p.stock_minimo, "unidad": p.unidad})
        if stock == 0:
            sin_stock.append({"id_prod": p.id_prod, "producto": p.producto})

        items.append({
            "id_prod": p.id_prod,
            "producto": p.producto,
            "tipo": tipo,
            "unidad": p.unidad,
            "stock": stock,
            "costo_promedio": round(cp, 4),
            "valor": valor,
        })

    items_sorted = sorted(items, key=lambda x: x["valor"], reverse=True)

    return {
        "total_valor": round(total_valor, 2),
        "total_articulos": len(productos),
        "por_tipo": [{"tipo": k, "valor": round(v, 2)} for k, v in sorted(por_tipo.items(), key=lambda x: -x[1])],
        "bajo_minimo": bajo_minimo,
        "sin_stock": sin_stock,
        "items": items_sorted,
    }
