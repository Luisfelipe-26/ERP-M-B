from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload
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
        "asiento_id": getattr(m, "asiento_id", None),
        "almacen_id": m.almacen_id,
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
    try:
        db.commit()
        db.refresh(p)
    except Exception:
        db.rollback()
        raise HTTPException(500, "Error al crear artículo")
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
    try:
        db.commit()
        db.refresh(p)
    except Exception:
        db.rollback()
        raise HTTPException(500, "Error al actualizar artículo")
    return p


@router.delete("/articulos/{id_prod}")
def delete_articulo(id_prod: str, db: Session = Depends(get_db), _=Depends(auth.require_admin)):
    p = db.query(models.Producto).filter(models.Producto.id_prod == id_prod).first()
    if not p:
        raise HTTPException(status_code=404, detail="Producto no encontrado")
    p.activo = False
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(500, "Error al desactivar artículo")
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
        almacen_id=data.almacen_id,
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
                     "almacen_id": data.almacen_id,
                     "descripcion_linea": f"Inventario entrada {p.producto}"},
                    {"cuenta_id": cta_haber, "debe": 0, "haber": monto,
                     "descripcion_linea": f"Contrapartida GR {num_doc}"},
                ],
                current_user.nombre, origen_id=mov.id
            )
            if asiento:
                mov.asiento_id = asiento.id

    try:
        db.commit()
        db.refresh(mov)
    except Exception:
        db.rollback()
        import logging
        logging.getLogger(__name__).exception("Error en entrada inventario %s", num_doc)
        raise HTTPException(500, "Error al registrar entrada de inventario")
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
        almacen_id=data.almacen_id,
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
        fecha_gi = data.fecha or datetime.now()
        fecha_asiento = fecha_gi.date() if hasattr(fecha_gi, 'date') else fecha_gi
        asiento = _crear_asiento_auto(
            db, fecha_asiento, "GI", num_doc,
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

    try:
        db.commit()
        db.refresh(mov)
    except Exception:
        db.rollback()
        import logging
        logging.getLogger(__name__).exception("Error en salida inventario %s", num_doc)
        raise HTTPException(500, "Error al registrar salida de inventario")
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
        almacen_id=data.almacen_id,
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
                     "almacen_id": mov.almacen_id,
                     "descripcion_linea": f"Ajuste positivo inventario {p.producto}"},
                    {"cuenta_id": cta_ajuste, "debe": 0, "haber": monto_ajuste,
                     "descripcion_linea": f"Contrapartida ajuste {num_doc}"},
                ]
            else:
                lineas = [
                    {"cuenta_id": cta_ajuste, "debe": monto_ajuste, "haber": 0,
                     "descripcion_linea": f"Ajuste negativo inventario {p.producto}"},
                    {"cuenta_id": p.cuenta_inventario_id, "debe": 0, "haber": monto_ajuste,
                     "almacen_id": mov.almacen_id,
                     "descripcion_linea": f"Inventario ajuste {num_doc}"},
                ]
            fecha_aj = data.fecha or datetime.now()
            fecha_asiento_aj = fecha_aj.date() if hasattr(fecha_aj, 'date') else fecha_aj
            asiento = _crear_asiento_auto(
                db, fecha_asiento_aj, "AJ", num_doc,
                f"Ajuste inventario {num_doc} — {p.producto} (dif: {diferencia:+.2f})",
                lineas, current_user.nombre, origen_id=mov.id
            )
            if asiento:
                mov.asiento_id = asiento.id

    try:
        db.commit()
        db.refresh(mov)
    except Exception:
        db.rollback()
        import logging
        logging.getLogger(__name__).exception("Error en ajuste inventario %s", num_doc)
        raise HTTPException(500, "Error al registrar ajuste de inventario")
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
    q = db.query(models.MovimientoInventario).options(joinedload(models.MovimientoInventario.producto))
    if producto_id:
        q = q.filter(models.MovimientoInventario.producto_id == producto_id)
    if tipo_doc:
        q = q.filter(models.MovimientoInventario.tipo_doc == tipo_doc)
    if fecha_desde:
        q = q.filter(models.MovimientoInventario.fecha >= fecha_desde)
    if fecha_hasta:
        from datetime import timedelta
        try:
            dt_hasta = datetime.strptime(fecha_hasta, "%Y-%m-%d") + timedelta(days=1)
        except ValueError:
            dt_hasta = datetime.fromisoformat(fecha_hasta)
        q = q.filter(models.MovimientoInventario.fecha < dt_hasta)
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


# ─── Alertas de Vencimiento ────────────────────────────────────────────────

@router.get("/alertas-vencimiento")
def alertas_vencimiento(dias: int = Query(60, ge=1, le=365),
                        db: Session = Depends(get_db), _=Depends(auth.get_current_user)):
    from datetime import timedelta
    hoy = datetime.now().date()
    limite = hoy + timedelta(days=dias)

    movs = db.query(models.MovimientoInventario).filter(
        models.MovimientoInventario.tipo_doc == "GR",
        models.MovimientoInventario.vencimiento != None,
        models.MovimientoInventario.vencimiento <= limite,
    ).order_by(models.MovimientoInventario.vencimiento.asc()).all()

    alertas = []
    for m in movs:
        venc = m.vencimiento.date() if hasattr(m.vencimiento, 'date') else m.vencimiento
        dias_restantes = (venc - hoy).days
        alertas.append({
            "id": m.id,
            "producto_id": m.producto_id,
            "producto_nombre": m.producto.producto if m.producto else None,
            "lote": m.lote,
            "vencimiento": venc.isoformat(),
            "dias_restantes": dias_restantes,
            "estado": "vencido" if dias_restantes < 0 else "critico" if dias_restantes <= 15 else "proximo",
            "cantidad": m.cantidad,
            "num_documento": m.num_documento,
        })
    return {
        "total": len(alertas),
        "vencidos": len([a for a in alertas if a["estado"] == "vencido"]),
        "criticos": len([a for a in alertas if a["estado"] == "critico"]),
        "proximos": len([a for a in alertas if a["estado"] == "proximo"]),
        "alertas": alertas,
    }


# ─── Corrección masiva de fecha en movimientos ─────────────────────────────

@router.put("/movimientos/corregir-fecha")
def corregir_fecha_movimientos(
    fecha_incorrecta: str = Query(..., description="Fecha actual incorrecta YYYY-MM-DD"),
    fecha_correcta: str = Query(..., description="Fecha correcta YYYY-MM-DD"),
    tipo_doc: Optional[str] = Query(None, description="Filtrar por tipo doc (GR, GI, AJ)"),
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(auth.require_admin),
):
    from datetime import timedelta
    fi = datetime.strptime(fecha_incorrecta, "%Y-%m-%d")
    fc = datetime.strptime(fecha_correcta, "%Y-%m-%d")
    fi_inicio = fi
    fi_fin = fi + timedelta(days=1)

    q = db.query(models.MovimientoInventario).filter(
        models.MovimientoInventario.fecha >= fi_inicio,
        models.MovimientoInventario.fecha < fi_fin,
    )
    if tipo_doc:
        q = q.filter(models.MovimientoInventario.tipo_doc == tipo_doc)

    movs = q.all()
    if not movs:
        raise HTTPException(404, f"No se encontraron movimientos en {fecha_incorrecta}")

    asiento_ids = set()
    actualizados = []
    for m in movs:
        hora = m.fecha.time() if hasattr(m.fecha, 'time') else None
        m.fecha = fc.replace(hour=hora.hour, minute=hora.minute, second=hora.second) if hora else fc
        actualizados.append({"id": m.id, "num_documento": m.num_documento, "producto_id": m.producto_id})
        if m.asiento_id:
            asiento_ids.add(m.asiento_id)

    asientos_corregidos = 0
    for aid in asiento_ids:
        asiento = db.query(models.AsientoContable).filter(models.AsientoContable.id == aid).first()
        if asiento:
            asiento.fecha = fc.date() if hasattr(fc, 'date') else fc
            asientos_corregidos += 1

    audit.log(db, current_user, "CORREGIR_FECHA", "INV", fecha_incorrecta,
              f"Corregir fecha: {fecha_incorrecta} → {fecha_correcta} ({len(actualizados)} movs, {asientos_corregidos} asientos)",
              {"movimientos": [m["num_documento"] for m in actualizados]})

    try:
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(500, "Error al corregir fechas")

    return {
        "ok": True,
        "movimientos_actualizados": len(actualizados),
        "asientos_corregidos": asientos_corregidos,
        "detalle": actualizados,
    }


@router.put("/movimientos/marcar-inventario-inicial")
def marcar_inventario_inicial(
    fecha: str = Query(..., description="Fecha de los movimientos YYYY-MM-DD"),
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(auth.require_admin),
):
    from datetime import timedelta
    fd = datetime.strptime(fecha, "%Y-%m-%d")
    movs = db.query(models.MovimientoInventario).filter(
        models.MovimientoInventario.fecha >= fd,
        models.MovimientoInventario.fecha < fd + timedelta(days=1),
    ).all()
    if not movs:
        raise HTTPException(404, f"No se encontraron movimientos en {fecha}")

    for m in movs:
        m.motivo = "Inventario Inicial"
        m.observacion = "Carga de inventario inicial"

    audit.log(db, current_user, "MARCAR_INV_INICIAL", "INV", fecha,
              f"Marcados {len(movs)} movimientos del {fecha} como Inventario Inicial",
              {"ids": [m.id for m in movs]})

    try:
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(500, "Error al marcar movimientos")

    return {"ok": True, "movimientos_actualizados": len(movs)}


# ─── Reconciliar OTs sin movimiento de inventario ───────────────────────────

@router.post("/reconciliar-ot")
def reconciliar_ot(
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(auth.require_admin),
):
    detalles = db.query(models.OTDetalle).all()

    existentes = set()
    movs_existentes = db.query(
        models.MovimientoInventario.ot_referencia,
        models.MovimientoInventario.producto_id,
    ).filter(
        models.MovimientoInventario.tipo_doc == "OT",
        models.MovimientoInventario.tipo == "salida",
    ).all()
    for ot_ref, prod_id in movs_existentes:
        existentes.add((ot_ref, prod_id))

    agrupados = {}
    for d in detalles:
        key = (d.ot_id, d.producto_id)
        if key in existentes:
            continue
        if key not in agrupados:
            agrupados[key] = {"ot_id": d.ot_id, "producto_id": d.producto_id,
                              "cantidad": 0, "costo_total": 0, "fecha": d.fecha}
        agrupados[key]["cantidad"] += d.cantidad_usada or 0
        agrupados[key]["costo_total"] += d.costo_real or 0
        if d.fecha and (not agrupados[key]["fecha"] or d.fecha > agrupados[key]["fecha"]):
            agrupados[key]["fecha"] = d.fecha

    creados = []
    for key, info in sorted(agrupados.items(), key=lambda x: x[1].get("fecha") or datetime.min):
        prod = db.query(models.Producto).filter(models.Producto.id_prod == info["producto_id"]).first()
        if not prod or not prod.es_inventariable:
            continue
        cantidad = round(info["cantidad"], 4)
        if cantidad <= 0:
            continue

        cp = prod.costo_promedio or prod.costo_unitario or 0
        # Inventario manda: valuar el backfill al costo promedio actual (mejor
        # aproximación disponible; luego resincronizar alinea la OTDetalle).
        nuevo_stock = round(max(0.0, (prod.stock_actual or 0) - cantidad), 4)

        mov = models.MovimientoInventario(
            num_documento=f"OT-{info['ot_id']}",
            producto_id=info["producto_id"],
            tipo_doc="OT",
            tipo="salida",
            motivo="Consumo OT",
            cantidad=cantidad,
            costo_unitario=round(cp, 4),
            costo_promedio_post=round(cp, 4),
            stock_post=nuevo_stock,
            ot_referencia=info["ot_id"],
            observacion=f"Reconciliación retroactiva — OT #{info['ot_id']}",
            fecha=info["fecha"] or datetime.now(),
            usuario_id=current_user.id,
        )
        db.add(mov)
        prod.stock_actual = nuevo_stock
        creados.append({"ot_id": info["ot_id"], "producto_id": info["producto_id"],
                        "cantidad": cantidad, "stock_post": nuevo_stock})

    if creados:
        audit.log(db, current_user, "RECONCILIAR_OT", "INV", f"{len(creados)} movimientos",
                  f"Reconciliación OT: {len(creados)} movimientos de inventario creados retroactivamente",
                  {"movimientos": len(creados)})
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise HTTPException(500, "Error al reconciliar")

    return {
        "ok": True,
        "movimientos_creados": len(creados),
        "detalle": creados,
    }


# ─── Re-sincronizar costos de movimientos OT existentes ─────────────────────

@router.post("/resincronizar-costos-ot")
def resincronizar_costos_ot(
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(auth.require_admin),
):
    """Inventario manda (Valor INV): re-valúa OTDetalle.costo_real al costo de sus
    movimientos de inventario y recalcula los totales de cada OT afectada.
    Así el consumo de insumos del módulo OT refleja el costo promedio real del
    inventario, no un costo tecleado. NO modifica asientos contables (ver nota)."""
    # Costo del inventario por (ot, producto) desde las salidas OT
    movs = db.query(models.MovimientoInventario).filter(
        models.MovimientoInventario.tipo_doc == "OT",
        models.MovimientoInventario.tipo == "salida",
    ).all()
    inv_cost = {}
    for m in movs:
        key = (m.ot_referencia, m.producto_id)
        if key not in inv_cost:
            inv_cost[key] = {"cantidad": 0, "valor": 0}
        inv_cost[key]["cantidad"] += m.cantidad or 0
        inv_cost[key]["valor"] += (m.cantidad or 0) * (m.costo_unitario or 0)

    detalles = db.query(models.OTDetalle).all()
    ajustados = 0
    sin_movimiento = 0
    ots_afectadas = set()
    for d in detalles:
        inv = inv_cost.get((d.ot_id, d.producto_id))
        if not inv or inv["cantidad"] <= 0:
            sin_movimiento += 1  # no inventariable o sin movimiento -> no se toca
            continue
        costo_unit_inv = round(inv["valor"] / inv["cantidad"], 4)
        nuevo_real = round((d.cantidad_usada or 0) * costo_unit_inv, 2)
        if round(d.costo_unitario or 0, 4) != costo_unit_inv or round(d.costo_real or 0, 2) != nuevo_real:
            d.costo_unitario = costo_unit_inv
            d.costo_real = nuevo_real
            ajustados += 1
            ots_afectadas.add(d.ot_id)

    # Recalcular totales de las OTs afectadas
    ots_recalc = 0
    for ot_id in ots_afectadas:
        orden = db.query(models.OrdenTrabajo).filter(models.OrdenTrabajo.ot_id == ot_id).first()
        if not orden:
            continue
        dets = db.query(models.OTDetalle).filter(models.OTDetalle.ot_id == ot_id).all()
        costo_insumos = round(sum(x.costo_real or 0 for x in dets), 2)
        orden.costo_insumos = costo_insumos
        orden.costo_total = round((orden.costo_mano_obra or 0) + costo_insumos + (orden.costo_equipo or 0), 2)
        campo = db.query(models.Campo).filter(models.Campo.id_campo == orden.campo_id).first() if orden.campo_id else None
        area = campo.area_ha if campo and campo.area_ha and campo.area_ha > 0 else 1
        orden.costo_ha = round(orden.costo_total / area, 2)
        ots_recalc += 1

    if ajustados:
        audit.log(db, current_user, "RESYNC_COSTOS_OT", "OT", f"{ajustados} líneas",
                  f"Re-valuación OTDetalle desde inventario: {ajustados} líneas, {ots_recalc} OTs recalculadas",
                  {"lineas": ajustados, "ots": ots_recalc})
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise HTTPException(500, "Error al re-sincronizar costos")

    return {
        "ok": True,
        "lineas_ajustadas": ajustados,
        "ots_recalculadas": ots_recalc,
        "lineas_sin_movimiento": sin_movimiento,
    }


# ─── Limpiar reversiones OT huérfanas (de OTs borradas con código viejo) ────

@router.post("/limpiar-reversiones-huerfanas")
def limpiar_reversiones_huerfanas(
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(auth.require_admin),
):
    """Elimina reversiones OT (entradas) que NO tienen una salida OT pareja para el
    mismo (ot, producto). Son artefactos de OTs eliminadas con el código viejo, que
    borraba la salida y dejaba la entrada huérfana, inflando el neto de movimientos OT.
    Borrar el registro NO altera stock_actual (valor almacenado aparte)."""
    salidas = db.query(
        models.MovimientoInventario.ot_referencia,
        models.MovimientoInventario.producto_id,
    ).filter(
        models.MovimientoInventario.tipo_doc == "OT",
        models.MovimientoInventario.tipo == "salida",
    ).all()
    salida_keys = {(ot, pid) for ot, pid in salidas}

    reversiones = db.query(models.MovimientoInventario).filter(
        models.MovimientoInventario.tipo_doc == "OT",
        models.MovimientoInventario.tipo == "entrada",
        models.MovimientoInventario.motivo == "Reversión OT eliminada",
    ).all()

    huerfanas = [r for r in reversiones if (r.ot_referencia, r.producto_id) not in salida_keys]
    valor_eliminado = round(sum((r.cantidad or 0) * (r.costo_unitario or 0) for r in huerfanas), 2)
    detalle = [{"id": r.id, "ot_id": r.ot_referencia, "producto_id": r.producto_id,
                "cantidad": r.cantidad, "costo_unitario": r.costo_unitario} for r in huerfanas]

    for r in huerfanas:
        db.delete(r)

    if huerfanas:
        audit.log(db, current_user, "LIMPIAR_REV_HUERFANAS", "INV", f"{len(huerfanas)} reversiones",
                  f"Eliminadas {len(huerfanas)} reversiones OT huérfanas (valor RD$ {valor_eliminado:,.2f})",
                  {"eliminadas": detalle})
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise HTTPException(500, "Error al limpiar reversiones")

    return {
        "ok": True,
        "reversiones_eliminadas": len(huerfanas),
        "valor_eliminado": valor_eliminado,
    }


# ─── Conciliación Consumos OT vs Inventario ─────────────────────────────────

@router.get("/conciliacion-ot")
def conciliacion_ot(
    fecha_desde: Optional[str] = None,
    fecha_hasta: Optional[str] = None,
    db: Session = Depends(get_db),
    _=Depends(auth.get_current_user),
):
    from datetime import timedelta
    from sqlalchemy import case

    q_det = db.query(models.OTDetalle).join(
        models.OrdenTrabajo, models.OTDetalle.ot_id == models.OrdenTrabajo.ot_id
    )
    q_mov = db.query(models.MovimientoInventario).filter(
        models.MovimientoInventario.tipo_doc == "OT",
        models.MovimientoInventario.tipo == "salida",
    )

    if fecha_desde:
        q_det = q_det.filter(models.OrdenTrabajo.fecha_ejecucion >= fecha_desde)
        q_mov = q_mov.filter(models.MovimientoInventario.fecha >= fecha_desde)
    if fecha_hasta:
        try:
            dt_h = datetime.strptime(fecha_hasta, "%Y-%m-%d") + timedelta(days=1)
        except ValueError:
            dt_h = datetime.fromisoformat(fecha_hasta)
        q_det = q_det.filter(models.OrdenTrabajo.fecha_ejecucion < dt_h)
        q_mov = q_mov.filter(models.MovimientoInventario.fecha < dt_h)

    detalles = q_det.all()
    movimientos = q_mov.all()

    # Cache de productos (evita N+1) con flag es_inventariable
    prod_ids = {d.producto_id for d in detalles} | {m.producto_id for m in movimientos}
    prod_map = {}
    if prod_ids:
        for p in db.query(models.Producto).filter(models.Producto.id_prod.in_(prod_ids)).all():
            prod_map[p.id_prod] = p

    ot_consumos = {}
    for d in detalles:
        key = (d.ot_id, d.producto_id)
        if key not in ot_consumos:
            prod = prod_map.get(d.producto_id)
            ot_consumos[key] = {
                "ot_id": d.ot_id,
                "producto_id": d.producto_id,
                "producto_nombre": prod.producto if prod else d.producto_id,
                "unidad": d.unidad or (prod.unidad if prod else ""),
                "cantidad_ot": 0,
                "valor_ot": 0,
            }
        ot_consumos[key]["cantidad_ot"] += d.cantidad_usada or 0
        ot_consumos[key]["valor_ot"] += d.costo_real or 0

    inv_consumos = {}
    for m in movimientos:
        ot_ref = m.ot_referencia
        if not ot_ref:
            continue
        key = (ot_ref, m.producto_id)
        if key not in inv_consumos:
            inv_consumos[key] = {
                "ot_id": ot_ref,
                "producto_id": m.producto_id,
                "producto_nombre": m.producto.producto if m.producto else m.producto_id,
                "unidad": m.producto.unidad if m.producto else "",
                "cantidad_inv": 0,
                "valor_inv": 0,
            }
        inv_consumos[key]["cantidad_inv"] += m.cantidad or 0
        inv_consumos[key]["valor_inv"] += round((m.cantidad or 0) * (m.costo_unitario or 0), 2)

    all_keys = set(ot_consumos.keys()) | set(inv_consumos.keys())
    items = []
    conciliados = 0
    diferencias_qty = 0
    solo_ot = 0
    solo_inv = 0
    no_inventariable = 0

    for key in sorted(all_keys, key=lambda k: (k[0], k[1])):
        ot = ot_consumos.get(key, {})
        inv = inv_consumos.get(key, {})
        prod = prod_map.get(key[1])
        es_inv = prod.es_inventariable if prod else True
        q_ot = round(ot.get("cantidad_ot", 0), 4)
        q_inv = round(inv.get("cantidad_inv", 0), 4)
        v_ot = round(ot.get("valor_ot", 0), 2)
        v_inv = round(inv.get("valor_inv", 0), 2)
        dif_qty = round(q_ot - q_inv, 4)
        dif_val = round(v_ot - v_inv, 2)

        if q_ot > 0 and q_inv == 0 and not es_inv:
            # Insumo no inventariable: está en la OT pero no pasa por inventario (correcto)
            estado = "no_inventariable"
            no_inventariable += 1
        elif q_ot > 0 and q_inv == 0:
            estado = "solo_ot"
            solo_ot += 1
        elif q_inv > 0 and q_ot == 0:
            estado = "solo_inv"
            solo_inv += 1
        elif abs(dif_qty) < 0.001 and abs(dif_val) < 0.01:
            estado = "conciliado"
            conciliados += 1
        else:
            estado = "diferencia"
            diferencias_qty += 1

        items.append({
            "ot_id": key[0],
            "producto_id": key[1],
            "producto_nombre": ot.get("producto_nombre") or inv.get("producto_nombre", ""),
            "unidad": ot.get("unidad") or inv.get("unidad", ""),
            "es_inventariable": es_inv,
            "cantidad_ot": q_ot,
            "cantidad_inv": q_inv,
            "diferencia_qty": dif_qty,
            "valor_ot": v_ot,
            "valor_inv": v_inv,
            "diferencia_val": dif_val,
            "estado": estado,
        })

    valor_total_ot = round(sum(i["valor_ot"] for i in items), 2)
    valor_no_inv = round(sum(i["valor_ot"] for i in items if i["estado"] == "no_inventariable"), 2)
    # Diferencia real a explicar (excluye lo no inventariable, que nunca pasa por inventario)
    valor_conciliable_ot = round(valor_total_ot - valor_no_inv, 2)
    valor_total_inv = round(sum(i["valor_inv"] for i in items), 2)

    return {
        "total": len(items),
        "conciliados": conciliados,
        "diferencias": diferencias_qty,
        "solo_ot": solo_ot,
        "solo_inv": solo_inv,
        "no_inventariable": no_inventariable,
        "valor_total_ot": valor_total_ot,
        "valor_no_inventariable": valor_no_inv,
        "valor_conciliable_ot": valor_conciliable_ot,
        "valor_total_inv": valor_total_inv,
        "diferencia_neta": round(valor_conciliable_ot - valor_total_inv, 2),
        "items": items,
    }
