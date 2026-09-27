"""
Órdenes de Compra (OC) — CRUD completo con líneas de producto.
H-16 FIX: Implementación del ciclo de compra vinculado a GR.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from database import get_db
import models, schemas, auth
from routers.sequences import get_next, peek_next
from routers.contabilidad import (_crear_asiento_auto, _get_regla_cuentas, _verificar_presupuesto,
                                  _cuentas_retencion, _devengar_cxp_contra_compromisos, NCF_SIN_CREDITO,
                                  _registrar_mov_pres, _monto_presupuestario,
                                  _reversar_devengado_cxp, _itbis_compra)
from typing import List, Optional
from datetime import date, datetime, time
from decimal import Decimal
from sqlalchemy import func as sqlfunc, extract, Integer, case
import audit

router = APIRouter(prefix="/api/ordenes-compra", tags=["ordenes-compra"])

ESTADOS_OC = ["Borrador", "Aprobada", "Parcial", "Recibida", "Cerrada", "Cancelada"]


@router.get("/preview/next-id")
def next_oc_id_preview(db: Session = Depends(get_db), _=Depends(auth.get_current_user)):
    return {"next_oc_id": peek_next("OC", db)}


@router.get("")
def list_ocs(
    estado: Optional[str] = None,
    proveedor: Optional[str] = None,
    fecha_desde: Optional[str] = None,
    fecha_hasta: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    _=Depends(auth.get_current_user)
):
    q = db.query(models.OrdenCompra)
    if estado:
        q = q.filter(models.OrdenCompra.estado == estado)
    if proveedor:
        q = q.filter(models.OrdenCompra.proveedor.ilike(f"%{proveedor}%"))
    if fecha_desde:
        q = q.filter(models.OrdenCompra.fecha >= datetime.strptime(fecha_desde, "%Y-%m-%d"))
    if fecha_hasta:
        q = q.filter(models.OrdenCompra.fecha <= datetime.strptime(fecha_hasta + " 23:59:59", "%Y-%m-%d %H:%M:%S"))
    total = q.count()
    items = q.order_by(models.OrdenCompra.fecha.desc()).offset(skip).limit(limit).all()
    return {"items": [schemas.OrdenCompraOut.model_validate(o) for o in items], "total": total}


@router.get("/resumen-cxp")
def resumen_cxp(db: Session = Depends(get_db), _=Depends(auth.get_current_user)):
    """Dashboard summary of CxP linked to OCs."""
    cxp_q = db.query(models.CuentaPorPagar).filter(models.CuentaPorPagar.oc_id.isnot(None))
    total_pendiente = db.query(sqlfunc.coalesce(sqlfunc.sum(models.CuentaPorPagar.saldo_pendiente), 0)).filter(
        models.CuentaPorPagar.oc_id.isnot(None),
        models.CuentaPorPagar.estado.in_(["pendiente", "parcial"]),
    ).scalar()
    num_pendientes = cxp_q.filter(models.CuentaPorPagar.estado.in_(["pendiente", "parcial"])).count()
    num_vencidas = cxp_q.filter(
        models.CuentaPorPagar.estado.in_(["pendiente", "parcial"]),
        models.CuentaPorPagar.fecha_vencimiento < datetime.now().date(),
    ).count()
    monto_vencido = db.query(sqlfunc.coalesce(sqlfunc.sum(models.CuentaPorPagar.saldo_pendiente), 0)).filter(
        models.CuentaPorPagar.oc_id.isnot(None),
        models.CuentaPorPagar.estado.in_(["pendiente", "parcial"]),
        models.CuentaPorPagar.fecha_vencimiento < datetime.now().date(),
    ).scalar()
    return {
        "total_pendiente": float(total_pendiente),
        "num_pendientes": num_pendientes,
        "num_vencidas": num_vencidas,
        "monto_vencido": float(monto_vencido),
    }


MESES = ["", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
         "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]


@router.get("/reportes/compras-periodo")
def reporte_compras_periodo(
    anio: int = None,
    campo_id: Optional[str] = None,
    unidad_negocio_id: Optional[int] = None,
    departamento_id: Optional[int] = None,
    db: Session = Depends(get_db), _=Depends(auth.get_current_user),
):
    anio = anio or datetime.now().year
    q = db.query(
        extract("month", models.OrdenCompra.fecha).label("mes"),
        sqlfunc.coalesce(sqlfunc.sum(models.OrdenCompra.total_estimado), 0).label("total_estimado"),
        sqlfunc.coalesce(sqlfunc.sum(models.OrdenCompra.total_recibido), 0).label("total_recibido"),
        sqlfunc.count().label("num_ocs"),
        sqlfunc.sum(case((models.OrdenCompra.estado == "Recibida", 1), else_=0)).label("num_recibidas"),
    ).filter(extract("year", models.OrdenCompra.fecha) == anio)
    if campo_id:
        q = q.filter(models.OrdenCompra.campo_id == campo_id)
    if unidad_negocio_id:
        q = q.filter(models.OrdenCompra.unidad_negocio_id == unidad_negocio_id)
    if departamento_id:
        q = q.filter(models.OrdenCompra.departamento_id == departamento_id)
    rows = q.group_by("mes").all()
    by_month = {int(r.mes): r for r in rows}
    result = []
    for m in range(1, 13):
        r = by_month.get(m)
        result.append({
            "mes": m, "nombre_mes": MESES[m],
            "total_estimado": round(float(r.total_estimado), 2) if r else 0,
            "total_recibido": round(float(r.total_recibido), 2) if r else 0,
            "num_ocs": int(r.num_ocs) if r else 0,
            "num_recibidas": int(r.num_recibidas or 0) if r else 0,
        })
    return {"anio": anio, "datos": result}


@router.get("/reportes/top-proveedores")
def reporte_top_proveedores(
    anio: Optional[int] = None,
    mes: Optional[int] = None,
    limit: int = 10,
    campo_id: Optional[str] = None,
    unidad_negocio_id: Optional[int] = None,
    db: Session = Depends(get_db), _=Depends(auth.get_current_user),
):
    q = db.query(
        models.OrdenCompra.proveedor,
        models.OrdenCompra.proveedor_id,
        sqlfunc.coalesce(sqlfunc.sum(models.OrdenCompra.total_estimado), 0).label("total"),
        sqlfunc.count().label("num_ocs"),
    ).filter(models.OrdenCompra.proveedor.isnot(None))
    if anio:
        q = q.filter(extract("year", models.OrdenCompra.fecha) == anio)
    if mes:
        q = q.filter(extract("month", models.OrdenCompra.fecha) == mes)
    if campo_id:
        q = q.filter(models.OrdenCompra.campo_id == campo_id)
    if unidad_negocio_id:
        q = q.filter(models.OrdenCompra.unidad_negocio_id == unidad_negocio_id)
    rows = q.group_by(models.OrdenCompra.proveedor, models.OrdenCompra.proveedor_id)\
            .order_by(sqlfunc.sum(models.OrdenCompra.total_estimado).desc())\
            .limit(limit).all()
    grand = sum(float(r.total) for r in rows)
    return [{
        "proveedor": r.proveedor,
        "proveedor_id": r.proveedor_id,
        "total": round(float(r.total), 2),
        "num_ocs": int(r.num_ocs),
        "porcentaje": round(float(r.total) / grand * 100, 1) if grand else 0,
    } for r in rows]


@router.get("/reportes/compras-dimension")
def reporte_compras_dimension(
    dimension: str = Query("campo", pattern="^(campo|unidad_negocio|departamento)$"),
    anio: Optional[int] = None,
    mes: Optional[int] = None,
    db: Session = Depends(get_db), _=Depends(auth.get_current_user),
):
    OC = models.OrdenCompra
    if dimension == "campo":
        dim_col = OC.campo_id
        name_expr = OC.campo_id
    elif dimension == "unidad_negocio":
        dim_col = OC.unidad_negocio_id
        name_expr = models.UnidadNegocio.nombre
    else:
        dim_col = OC.departamento_id
        name_expr = models.Departamento.nombre

    q = db.query(
        dim_col.label("dim_id"),
        name_expr.label("nombre"),
        sqlfunc.coalesce(sqlfunc.sum(OC.total_estimado), 0).label("total"),
        sqlfunc.count().label("num_ocs"),
    ).filter(dim_col.isnot(None))

    if dimension == "unidad_negocio":
        q = q.join(models.UnidadNegocio, OC.unidad_negocio_id == models.UnidadNegocio.id)
    elif dimension == "departamento":
        q = q.join(models.Departamento, OC.departamento_id == models.Departamento.id)

    if anio:
        q = q.filter(extract("year", OC.fecha) == anio)
    if mes:
        q = q.filter(extract("month", OC.fecha) == mes)

    rows = q.group_by(dim_col, name_expr).order_by(sqlfunc.sum(OC.total_estimado).desc()).all()
    grand = sum(float(r.total) for r in rows)
    return [{
        "id": str(r.dim_id) if r.dim_id else None,
        "nombre": str(r.nombre or "Sin asignar"),
        "total": round(float(r.total), 2),
        "num_ocs": int(r.num_ocs),
        "porcentaje": round(float(r.total) / grand * 100, 1) if grand else 0,
    } for r in rows]


@router.get("/reportes/productos-frecuentes")
def reporte_productos_frecuentes(
    anio: Optional[int] = None,
    mes: Optional[int] = None,
    limit: int = 15,
    db: Session = Depends(get_db), _=Depends(auth.get_current_user),
):
    L = models.OrdenCompraLinea
    P = models.Producto
    OC = models.OrdenCompra
    q = db.query(
        L.producto_id,
        P.producto.label("producto_nombre"),
        P.unidad,
        sqlfunc.coalesce(sqlfunc.sum(L.cantidad), 0).label("total_cantidad"),
        sqlfunc.coalesce(sqlfunc.sum(L.subtotal), 0).label("total_monto"),
        sqlfunc.count(sqlfunc.distinct(L.oc_id)).label("num_ocs"),
    ).join(P, L.producto_id == P.id_prod)\
     .join(OC, L.oc_id == OC.oc_id)
    if anio:
        q = q.filter(extract("year", OC.fecha) == anio)
    if mes:
        q = q.filter(extract("month", OC.fecha) == mes)
    rows = q.group_by(L.producto_id, P.producto, P.unidad)\
            .order_by(sqlfunc.sum(L.subtotal).desc())\
            .limit(limit).all()
    return [{
        "producto_id": r.producto_id,
        "producto_nombre": r.producto_nombre,
        "unidad": r.unidad,
        "total_cantidad": round(float(r.total_cantidad), 2),
        "total_monto": round(float(r.total_monto), 2),
        "num_ocs": int(r.num_ocs),
    } for r in rows]


@router.get("/auditoria/descuentos-brutos")
def auditoria_descuentos_brutos(db: Session = Depends(get_db),
                                _=Depends(auth.require_admin)):
    """Recepciones que entraron al precio bruto ignorando el descuento de la línea.

    Hasta el fix, recibir_oc valuaba el GR y la CxP con precio_unitario sin aplicar
    descuento_pct. Lista cada línea afectada con lo registrado, lo correcto y la
    diferencia, para decidir qué corregir.
    """
    lineas = db.query(models.OrdenCompraLinea).filter(
        models.OrdenCompraLinea.descuento_pct > 0,
        models.OrdenCompraLinea.cantidad_recibida > 0,
    ).all()

    items, sobre_inv, sobre_cxp = [], Decimal("0"), Decimal("0")
    for l in lineas:
        bruto = float(l.precio_unitario or 0)
        neto = _precio_neto(l)
        if abs(bruto - neto) < 0.0001:
            continue

        grs = db.query(models.MovimientoInventario).filter(
            models.MovimientoInventario.oc_referencia == l.oc_id,
            models.MovimientoInventario.producto_id == l.producto_id,
            models.MovimientoInventario.tipo_doc == "GR",
        ).all()
        grs_mal = [g for g in grs if abs(float(g.costo_unitario or 0) - bruto) < 0.0001]

        lcxps = db.query(models.LineaCxP).filter(models.LineaCxP.oc_linea_id == l.id).all()
        cxps_mal = []
        for lc in lcxps:
            esperado_bruto = round(float(lc.cantidad or 0) * bruto, 2)
            if abs(float(lc.subtotal or 0) - esperado_bruto) < 0.01:
                cxp = db.query(models.CuentaPorPagar).get(lc.cxp_id)
                cxps_mal.append((lc, cxp))

        if not grs_mal and not cxps_mal:
            continue

        dif_inv = sum(round(float(g.cantidad or 0) * (bruto - neto), 2) for g in grs_mal)
        dif_cxp = sum(round(float(lc.cantidad or 0) * (bruto - neto), 2) for lc, _ in cxps_mal)
        sobre_inv += Decimal(str(dif_inv))
        sobre_cxp += Decimal(str(dif_cxp))

        items.append({
            "oc_id": l.oc_id, "linea_id": l.id, "producto_id": l.producto_id,
            "descuento_pct": float(l.descuento_pct), "cantidad_recibida": float(l.cantidad_recibida),
            "precio_bruto": round(bruto, 4), "precio_neto": round(neto, 4),
            "inventario": [{"num_documento": g.num_documento, "fecha": g.fecha,
                            "cantidad": float(g.cantidad or 0),
                            "costo_registrado": float(g.costo_unitario or 0),
                            "costo_correcto": round(neto, 4)} for g in grs_mal],
            "sobrecosto_inventario": dif_inv,
            "cxp": [{"numero": c.numero, "estado": c.estado,
                     "subtotal_registrado": float(lc.subtotal or 0),
                     "subtotal_correcto": round(float(lc.cantidad or 0) * neto, 2),
                     "ya_pagada": c.estado == "pagada",
                     "saldo_pendiente": float(c.saldo_pendiente or 0)} for lc, c in cxps_mal],
            "sobrepago_cxp": dif_cxp,
        })

    return {
        "lineas_afectadas": len(items),
        "sobrecosto_inventario_total": float(sobre_inv),
        "sobrepago_cxp_total": float(sobre_cxp),
        "cxp_ya_pagadas": sum(1 for i in items for c in i["cxp"] if c["ya_pagada"]),
        "items": items,
    }


@router.post("")
def create_oc(data: schemas.OrdenCompraCreate, db: Session = Depends(get_db),
              current_user: models.Usuario = Depends(auth.require_supervisor)):
    if not data.lineas:
        raise HTTPException(status_code=400, detail="La orden de compra debe tener al menos una línea")

    oc_id = get_next("OC", db)

    # Sin proveedor válido la OC no puede generar CxP al recibirse, así que se
    # rechaza aquí en vez de fallar más adelante en el ciclo.
    nombre_proveedor = data.proveedor
    prov_id = data.proveedor_id
    if prov_id:
        prov = db.query(models.Proveedor).filter(models.Proveedor.id == prov_id).first()
        if not prov:
            raise HTTPException(400, f"Proveedor ID {prov_id} no existe")
        if not prov.activo:
            raise HTTPException(400, f"El proveedor '{prov.nombre}' está inactivo")
        nombre_proveedor = prov.nombre
    elif nombre_proveedor:
        prov = db.query(models.Proveedor).filter(
            models.Proveedor.nombre == nombre_proveedor,
            models.Proveedor.activo == True,
        ).first()
        if prov:
            prov_id = prov.id

    try:
        oc = models.OrdenCompra(
            oc_id=oc_id,
            fecha=data.fecha or datetime.now(),
            proveedor=nombre_proveedor,
            proveedor_id=prov_id,
            campo_id=data.campo_id,
            unidad_negocio_id=data.unidad_negocio_id,
            departamento_id=data.departamento_id,
            almacen_id=data.almacen_id,
            estado="Borrador",
            observaciones=data.observaciones,
        )
        db.add(oc)
        db.flush()

        total = _agregar_lineas(db, oc_id, data.lineas)
        oc.total_estimado = round(total, 2)

        audit.log(db, current_user, "CREAR", "OC", oc_id,
                  f"OC {oc_id} creada en Borrador: {nombre_proveedor or 'Sin proveedor'} — Total: RD$ {total:,.2f}",
                  {"proveedor": nombre_proveedor, "proveedor_id": prov_id,
                   "campo_id": data.campo_id, "total_estimado": total,
                   "num_lineas": len(data.lineas)})

        db.commit()
        db.refresh(oc)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise HTTPException(500, "Error al crear la orden de compra")
    return schemas.OrdenCompraOut.model_validate(oc)


@router.get("/{oc_id}")
def get_oc(oc_id: str, db: Session = Depends(get_db), _=Depends(auth.get_current_user)):
    oc = db.query(models.OrdenCompra).filter(models.OrdenCompra.oc_id == oc_id).first()
    if not oc:
        raise HTTPException(status_code=404, detail="Orden de compra no encontrada")
    lineas = db.query(models.OrdenCompraLinea).filter(models.OrdenCompraLinea.oc_id == oc_id).all()

    lineas_out = []
    for l in lineas:
        prod = db.query(models.Producto).filter(models.Producto.id_prod == l.producto_id).first()
        lineas_out.append({
            "id": l.id,
            "oc_id": l.oc_id,
            "producto_id": l.producto_id,
            "producto_nombre": prod.producto if prod else l.producto_id,
            "unidad": prod.unidad if prod else "",
            "cantidad": l.cantidad,
            "cantidad_recibida": l.cantidad_recibida or 0,
            "cantidad_pendiente": round(l.cantidad - (l.cantidad_recibida or 0), 4),
            "cantidad_facturada": l.cantidad_facturada or 0,
            "pendiente_facturar": round(_pendiente_facturar(l), 4),
            "precio_neto": round(_precio_neto(l), 4),
            "precio_unitario": l.precio_unitario,
            "descuento_pct": float(l.descuento_pct or 0),
            "impuesto": l.impuesto or "itbis_18",
            "subtotal": l.subtotal,
            "cuenta_contable_id": l.cuenta_contable_id,
            "unidad_negocio_id": l.unidad_negocio_id,
            "departamento_id": l.departamento_id,
            "almacen_id": l.almacen_id,
        })

    asiento_info = None
    if oc.estado in ("Recibida", "Parcial"):
        asiento = db.query(models.AsientoContable).filter(
            models.AsientoContable.origen == "GR",
            models.AsientoContable.referencia_id == oc_id,
        ).order_by(models.AsientoContable.fecha.desc()).first()
        if asiento:
            asiento_info = {"numero": asiento.numero, "fecha": str(asiento.fecha),
                            "total_debe": float(asiento.total_debe or 0),
                            "estado": asiento.estado}

    compromiso_info = None
    comp = db.query(models.CompromisoPresupuestario).filter(
        models.CompromisoPresupuestario.origen_tipo == "OC",
        models.CompromisoPresupuestario.origen_id == oc_id,
    ).first()
    if comp:
        compromiso_info = {
            "id": comp.id, "monto": float(comp.monto or 0),
            "estado": comp.estado, "anio": comp.anio, "mes": comp.mes,
        }

    cxp_list = db.query(models.CuentaPorPagar).filter(
        models.CuentaPorPagar.oc_id == oc_id,
    ).order_by(models.CuentaPorPagar.fecha_factura.desc()).all()
    cxp_out = [{
        "id": c.id, "numero": c.numero, "fecha": str(c.fecha_factura),
        "total": float(c.total or 0), "saldo": float(c.saldo_pendiente or 0),
        "estado": c.estado, "num_factura": c.num_factura_proveedor, "ncf": c.ncf,
    } for c in cxp_list]

    return {
        "orden": schemas.OrdenCompraOut.model_validate(oc),
        "lineas": lineas_out,
        "asiento_contable": asiento_info,
        "compromiso": compromiso_info,
        "cuentas_por_pagar": cxp_out,
        "por_facturar": round(sum(_pendiente_facturar(l) * _precio_neto(l) for l in lineas), 2),
    }


def _precio_neto(linea) -> float:
    """Precio unitario de una línea de OC después de su descuento."""
    return float(linea.precio_unitario or 0) * (1 - float(linea.descuento_pct or 0) / 100)


def _cuentas_producto(prod):
    """(cuenta de inventario, cuenta de costo) del producto, o de su categoría si no tiene."""
    if prod is None:
        return None, None
    cat = prod.categoria
    return (prod.cuenta_inventario_id or (cat.cuenta_inventario_id if cat else None),
            prod.cuenta_costo_id or (cat.cuenta_costo_id if cat else None))


def _cuenta_presupuesto_linea(linea, prod, cuenta_regla):
    """Cuenta contra la que una línea compromete presupuesto.

    La que eligió el usuario; si no, la de costo del producto, que es donde terminará el
    gasto. La regla de compra queda como último recurso: por defecto es Inventario (clase 1)
    y el control presupuestario solo mira las clases 4-6, así que con ella ninguna OC se
    controlaba.
    """
    return linea.cuenta_contable_id or _cuentas_producto(prod)[1] or cuenta_regla


def _cuenta_debito_recepcion(linea, prod, cuenta_regla):
    """Cuenta que se debita al recibir: inventario si el producto lleva stock, gasto si no.

    La de inventario es la del producto, la misma que acreditan las salidas y la que usa la
    conciliación con el mayor. Un servicio no entra al stock: cargarlo a inventario inflaba
    la cuenta sin que la valuación lo reflejara.
    """
    inv, costo = _cuentas_producto(prod)
    if prod is None or prod.es_inventariable:
        return inv or cuenta_regla
    return linea.cuenta_contable_id or costo or cuenta_regla


def _agregar_lineas(db: Session, oc_id: str, lineas_in) -> float:
    """Valida y crea las líneas de una OC; devuelve el total neto.

    Cada línea guarda su cuenta presupuestaria desde que se crea, para que se vea en la OC
    y no cambie si luego cambia la configuración del producto.
    """
    r_compra = _get_regla_cuentas(db, "compra", "factura_proveedor")
    total = 0.0
    for linea in lineas_in:
        prod = db.query(models.Producto).filter(
            models.Producto.id_prod == linea.producto_id, models.Producto.activo == True
        ).first()
        if not prod:
            raise HTTPException(400, f"Producto '{linea.producto_id}' no existe o está inactivo")
        if linea.cantidad <= 0:
            raise HTTPException(400, f"Cantidad debe ser mayor a 0 para '{prod.producto}'")
        if linea.precio_unitario < 0:
            raise HTTPException(400, f"Precio no puede ser negativo para '{prod.producto}'")
        if linea.cuenta_contable_id and not db.query(models.CuentaContable).get(linea.cuenta_contable_id):
            raise HTTPException(400, f"La cuenta {linea.cuenta_contable_id} de '{prod.producto}' no existe")

        desc = float(linea.descuento_pct or 0)
        subtotal = round(linea.cantidad * linea.precio_unitario * (1 - desc / 100), 2)
        total += subtotal
        db.add(models.OrdenCompraLinea(
            oc_id=oc_id,
            producto_id=linea.producto_id,
            cantidad=linea.cantidad,
            cantidad_recibida=0,
            precio_unitario=linea.precio_unitario,
            descuento_pct=desc,
            impuesto=linea.impuesto or prod.impuesto_compra or "itbis_18",
            subtotal=subtotal,
            cuenta_contable_id=_cuenta_presupuesto_linea(linea, prod, r_compra[0] if r_compra else None),
            unidad_negocio_id=linea.unidad_negocio_id,
            departamento_id=linea.departamento_id,
            almacen_id=linea.almacen_id,
        ))
    return total


def _liberar_compromisos(db: Session, oc_id: str, motivo: str, user) -> Decimal:
    """Cancela los compromisos activos de una OC y libera lo que aún no se ejecutó.

    Lo ya facturado se liberó al devengar, así que solo vuelve el remanente.
    """
    liberado = Decimal("0")
    for comp in db.query(models.CompromisoPresupuestario).filter(
            models.CompromisoPresupuestario.origen_tipo == "OC",
            models.CompromisoPresupuestario.origen_id == oc_id,
            models.CompromisoPresupuestario.estado == "activo").all():
        remanente = Decimal(str(comp.monto or 0)) - Decimal(str(comp.monto_ejecutado or 0))
        comp.estado = "cancelado"
        if remanente <= 0:
            continue
        _registrar_mov_pres(
            db, tipo="LIBERACION", fecha=date.today(),
            cuenta_id=comp.cuenta_id, monto=-remanente,
            anio=comp.anio, mes=comp.mes,
            campo_id=comp.campo_id, unidad_negocio_id=comp.unidad_negocio_id,
            departamento_id=comp.departamento_id,
            origen_tipo="OC", origen_id=oc_id,
            notas=f"Liberación de remanente por {motivo} OC {oc_id}",
            usuario_id=user.id,
        )
        liberado += remanente
    return liberado


def _proveedor_de(db: Session, oc):
    prov = db.query(models.Proveedor).get(oc.proveedor_id) if oc.proveedor_id else None
    if not prov and oc.proveedor:
        prov = db.query(models.Proveedor).filter(
            models.Proveedor.nombre == oc.proveedor, models.Proveedor.activo == True).first()
    return prov


def _entradas_presupuestarias_oc(db: Session, oc, prov, cuenta_fallback: int) -> list:
    """Desglosa una OC en las líneas presupuestarias que afecta.

    Cada línea de OC puede llevar su propia cuenta y dimensiones; usar solo las del
    encabezado imputaría toda la orden a un único centro de costo, que es lo que hacía antes.
    """
    entradas = []
    for l in db.query(models.OrdenCompraLinea).filter(
            models.OrdenCompraLinea.oc_id == oc.oc_id).order_by(models.OrdenCompraLinea.id).all():
        sub = Decimal(str(l.subtotal or 0))
        if sub <= 0:
            continue
        prod = db.query(models.Producto).filter(models.Producto.id_prod == l.producto_id).first()
        entradas.append({
            "oc_linea_id": l.id,
            "producto": prod.producto if prod else l.producto_id,
            "cuenta_id": _cuenta_presupuesto_linea(l, prod, cuenta_fallback),
            "campo_id": oc.campo_id,
            "unidad_negocio_id": l.unidad_negocio_id or oc.unidad_negocio_id,
            "departamento_id": l.departamento_id or oc.departamento_id,
            "monto": _monto_presupuestario(sub, _itbis_compra(sub, l.impuesto, prov), prov)
                     .quantize(Decimal("0.01")),
        })

    if not entradas:
        sub = Decimal(str(oc.total_estimado or 0))
        entradas.append({
            "oc_linea_id": None,
            "cuenta_id": cuenta_fallback,
            "campo_id": oc.campo_id,
            "unidad_negocio_id": oc.unidad_negocio_id,
            "departamento_id": oc.departamento_id,
            "monto": _monto_presupuestario(sub, _itbis_compra(sub, "itbis_18", prov), prov)
                     .quantize(Decimal("0.01")),
        })
    return entradas


@router.post("/{oc_id}/aprobar")
def aprobar_oc(oc_id: str, override: bool = Query(False),
               db: Session = Depends(get_db),
               current_user: models.Usuario = Depends(auth.require_supervisor)):
    """Aprobar OC: crea compromiso presupuestario + bloqueo duro."""
    oc = db.query(models.OrdenCompra).filter(models.OrdenCompra.oc_id == oc_id).first()
    if not oc:
        raise HTTPException(404, "Orden de compra no encontrada")
    if oc.estado != "Borrador":
        raise HTTPException(400, f"Solo se puede aprobar una OC en Borrador (estado actual: {oc.estado})")

    total = float(oc.total_estimado or 0)
    r_compra = _get_regla_cuentas(db, "compra", "factura_proveedor")

    # Sin proveedor registrado la recepción no puede crear la CxP, y el asiento dejaría
    # un saldo en el mayor de proveedores que ningún documento respalda.
    prov = _proveedor_de(db, oc)
    if not prov:
        raise HTTPException(400, "Asigne un proveedor registrado a la OC antes de aprobarla")

    ver = {}
    if total > 0:
        fecha_oc = oc.fecha or datetime.now()
        fecha_check = fecha_oc.date() if hasattr(fecha_oc, 'date') else fecha_oc
        comp_anio = fecha_check.year
        comp_mes = fecha_check.month

        entradas = _entradas_presupuestarias_oc(db, oc, prov, r_compra[0] if r_compra else None)
        sin_cuenta = [e.get("producto") or "encabezado" for e in entradas if not e["cuenta_id"]]
        if sin_cuenta:
            raise HTTPException(400, (
                f"Sin cuenta contable: {', '.join(sin_cuenta)}. Asigne la cuenta en la línea, en el "
                "producto o su categoría, o configure la regla compra / factura_proveedor."))

        ver = _verificar_presupuesto(db, [{
            "cuenta_id": e["cuenta_id"], "debe": float(e["monto"]), "haber": 0,
            "campo_id": e["campo_id"],
            "unidad_negocio_id": e["unidad_negocio_id"],
            "departamento_id": e["departamento_id"],
        } for e in entradas], fecha_check)

        if ver.get("bloqueado") and not override:
            raise HTTPException(400, {
                "detail": "Presupuesto insuficiente — aprobación bloqueada",
                "alertas": ver.get("alertas", []),
                "detalle": ver.get("detalle", []),
                "requiere_override": True,
            })
        if ver.get("bloqueado") and override and current_user.rol != "admin":
            raise HTTPException(403, "Solo un administrador puede autorizar sobregiro presupuestario")

        for e in entradas:
            db.add(models.CompromisoPresupuestario(
                anio=comp_anio, mes=comp_mes,
                cuenta_id=e["cuenta_id"],
                campo_id=e["campo_id"],
                unidad_negocio_id=e["unidad_negocio_id"],
                departamento_id=e["departamento_id"],
                monto=e["monto"], monto_ejecutado=Decimal("0"),
                oc_linea_id=e["oc_linea_id"],
                origen_tipo="OC", origen_id=oc_id, estado="activo",
            ))
            _registrar_mov_pres(
                db, tipo="COMPROMISO", fecha=fecha_check,
                cuenta_id=e["cuenta_id"], monto=e["monto"],
                anio=comp_anio, mes=comp_mes,
                campo_id=e["campo_id"], unidad_negocio_id=e["unidad_negocio_id"],
                departamento_id=e["departamento_id"],
                origen_tipo="OC", origen_id=oc_id,
                notas=f"Compromiso OC {oc_id}" + (f" línea {e['oc_linea_id']}" if e["oc_linea_id"] else ""),
                usuario_id=current_user.id,
            )

    oc.estado = "Aprobada"
    oc.aprobado_por = current_user.nombre
    oc.fecha_aprobacion = datetime.now()

    audit.log(db, current_user, "APROBAR", "OC", oc_id,
              f"OC {oc_id} aprobada por {current_user.nombre}" +
              (" (override presupuestario)" if override else ""),
              {"total": total, "override": override})

    db.commit()
    return {"ok": True, "estado": "Aprobada", "alertas_presupuesto": ver.get("alertas", [])}


@router.post("/{oc_id}/cerrar")
def cerrar_oc(oc_id: str, db: Session = Depends(get_db),
              current_user: models.Usuario = Depends(auth.require_supervisor)):
    """Cerrar OC: libera compromiso presupuestario remanente."""
    oc = db.query(models.OrdenCompra).filter(models.OrdenCompra.oc_id == oc_id).first()
    if not oc:
        raise HTTPException(404, "Orden de compra no encontrada")
    if oc.estado not in ("Aprobada", "Parcial", "Recibida"):
        raise HTTPException(400, f"Solo se puede cerrar una OC Aprobada, Parcial o Recibida (estado actual: {oc.estado})")

    _liberar_compromisos(db, oc_id, "cierre", current_user)

    oc.estado = "Cerrada"
    oc.cerrado_por = current_user.nombre
    oc.fecha_cierre = datetime.now()

    audit.log(db, current_user, "CERRAR", "OC", oc_id,
              f"OC {oc_id} cerrada por {current_user.nombre} — compromiso remanente liberado",
              {"total_estimado": float(oc.total_estimado or 0),
               "total_recibido": float(oc.total_recibido or 0)})

    db.commit()
    return {"ok": True, "estado": "Cerrada"}


@router.put("/{oc_id}/estado")
def update_oc_estado(oc_id: str, estado: str = Query(...),
                      db: Session = Depends(get_db),
                      current_user: models.Usuario = Depends(auth.require_supervisor)):
    """Cambio manual de estado (solo Cancelada desde Borrador/Aprobada)."""
    oc = db.query(models.OrdenCompra).filter(models.OrdenCompra.oc_id == oc_id).first()
    if not oc:
        raise HTTPException(404, "Orden de compra no encontrada")

    if estado == "Cancelada":
        if oc.estado not in ("Borrador", "Aprobada", "Parcial"):
            raise HTTPException(400, f"No se puede cancelar una OC en estado {oc.estado}")
        _liberar_compromisos(db, oc_id, "cancelación", current_user)
        estado_anterior = oc.estado
        oc.estado = "Cancelada"
        audit.log(db, current_user, "CANCELAR", "OC", oc_id,
                  f"OC {oc_id} cancelada por {current_user.nombre}",
                  {"estado_anterior": estado_anterior})
        db.commit()
        return {"ok": True, "estado": "Cancelada"}

    raise HTTPException(400, "Use /aprobar para aprobar o /cerrar para cerrar. Solo se permite cancelar vía este endpoint.")


from pydantic import BaseModel as PydanticBase
from typing import List as TList
import re as _re

_EPS = 1e-6          # holgura de redondeo al comparar cantidades
TOL_PRECIO = 0.02    # diferencia de precio factura vs OC que se acepta sin autorización

# Comprobantes que puede traer (o, en B11/E41, emitir la finca por) una compra.
TIPOS_NCF_COMPRA = {
    "B01": "Crédito fiscal", "E31": "Crédito fiscal",
    "B02": "Consumo", "E32": "Consumo",
    "B11": "Comprobante de compras", "E41": "Comprobante de compras",
    "B13": "Gastos menores", "E43": "Gastos menores",
    "B14": "Regímenes especiales", "E44": "Regímenes especiales",
    "B15": "Gubernamental", "E45": "Gubernamental",
}
_NCF_RE = _re.compile(r"^(B\d{10}|E\d{12})$")


def _normalizar_ncf(ncf: str, tipos: dict, que: str) -> tuple:
    """Valida el NCF (B + 10 dígitos, o e-CF: E + 12) y devuelve (ncf, tipo)."""
    n = (ncf or "").strip().upper().replace("-", "").replace(" ", "")
    if not _NCF_RE.match(n):
        raise HTTPException(400, (
            f"NCF '{ncf}' inválido: debe ser B y 10 dígitos (B0100000123) "
            "o, si es electrónico, E y 12 dígitos (E310000000123)"))
    if n[:3] not in tipos:
        raise HTTPException(400, f"El comprobante {n[:3]} no corresponde a {que}")
    return n, n[:3]


def _pendiente_facturar(linea) -> float:
    return max(0.0, float(linea.cantidad_recibida or 0) - float(linea.cantidad_facturada or 0))


def _cuenta_diferencia(linea, prod, cuenta_recepcion):
    """Dónde va lo que la factura cobra de más o de menos respecto a la OC.

    En un servicio, a su propio gasto. En un producto con stock, a su cuenta de costo: el
    inventario queda valuado al precio de la OC y el ajuste no lo desalinea del mayor.
    """
    if prod is not None and not prod.es_inventariable:
        return cuenta_recepcion
    return _cuentas_producto(prod)[1] or linea.cuenta_contable_id or cuenta_recepcion


class FacturaLinea(PydanticBase):
    linea_id: int
    cantidad: float
    precio_unitario: Optional[float] = None    # neto de descuento; None = el de la OC


class FacturaDatos(PydanticBase):
    ncf: str
    num_factura: Optional[str] = None
    fecha_factura: Optional[date] = None
    fecha_vencimiento: Optional[date] = None
    notas: Optional[str] = None


class FacturaPayload(FacturaDatos):
    lineas: TList[FacturaLinea] = []           # vacío = todo lo recibido sin facturar
    aceptar_diferencia: bool = False           # solo admin: precio fuera de tolerancia


def _registrar_factura(db: Session, oc, prov, data: FacturaPayload, user):
    """Registra la factura del proveedor contra lo recibido de una OC (three-way match).

    Como en Dynamics 365: la recepción dejó la mercancía contra la cuenta puente
    "Compras recibidas por facturar"; la factura la liquida y reconoce la deuda real con
    su ITBIS y sus retenciones. Solo se factura lo recibido y aún no facturado, y el
    precio no puede apartarse del de la OC más de la tolerancia sin autorización.
    """
    from datetime import timedelta
    ncf, tipo_ncf = _normalizar_ncf(data.ncf, TIPOS_NCF_COMPRA, "una factura de compra")
    dup = db.query(models.CuentaPorPagar).filter(
        models.CuentaPorPagar.proveedor_id == prov.id, models.CuentaPorPagar.ncf == ncf,
        models.CuentaPorPagar.estado != "anulada").first()
    if dup:
        raise HTTPException(400, f"El NCF {ncf} de {prov.nombre} ya está registrado en {dup.numero}")
    hoy = date.today()
    fecha = data.fecha_factura or hoy
    if fecha > hoy:
        raise HTTPException(400, "La fecha de la factura no puede ser futura")

    r_compra = _get_regla_cuentas(db, "compra", "factura_proveedor")
    r_puente = _get_regla_cuentas(db, "compra", "recepcion_por_facturar")
    if not r_compra or not r_puente:
        raise HTTPException(400, "Configure las reglas compra / factura_proveedor y compra / recepcion_por_facturar")

    por_id = {l.id: l for l in db.query(models.OrdenCompraLinea).filter(
        models.OrdenCompraLinea.oc_id == oc.oc_id).all()}
    pedidos = data.lineas or [FacturaLinea(linea_id=l.id, cantidad=_pendiente_facturar(l))
                              for l in por_id.values() if _pendiente_facturar(l) > _EPS]
    if not pedidos:
        raise HTTPException(400, f"La OC {oc.oc_id} no tiene mercancía recibida pendiente de facturar")

    sin_credito = tipo_ncf in NCF_SIN_CREDITO
    dims_oc = {"campo_id": oc.campo_id, "unidad_negocio_id": oc.unidad_negocio_id,
               "departamento_id": oc.departamento_id}
    puente = Decimal("0")
    diferencias: dict = {}     # (cuenta, un, depto) -> monto (+ cobra de más, - de menos)
    itbis_no_deducible: dict = {}
    subtotal = itbis = Decimal("0")
    lineas_cxp = []
    for item in pedidos:
        if item.cantidad <= 0:
            continue
        linea = por_id.get(item.linea_id)
        if not linea:
            raise HTTPException(400, f"La línea {item.linea_id} no pertenece a la OC {oc.oc_id}")
        prod = db.query(models.Producto).filter(models.Producto.id_prod == linea.producto_id).first()
        nombre = prod.producto if prod else linea.producto_id
        pend = _pendiente_facturar(linea)
        if item.cantidad > pend + _EPS:
            raise HTTPException(400, (
                f"{nombre}: hay {pend:g} recibidas sin facturar y la factura trae {item.cantidad:g}. "
                "Registre primero la recepción de lo que falta."))
        precio_oc = _precio_neto(linea)
        precio = precio_oc if item.precio_unitario is None else float(item.precio_unitario)
        if precio < 0:
            raise HTTPException(400, f"{nombre}: el precio no puede ser negativo")
        if precio_oc > 0 and abs(precio - precio_oc) / precio_oc > TOL_PRECIO + 1e-9:
            if not data.aceptar_diferencia:
                raise HTTPException(400, (
                    f"{nombre}: la factura cobra {precio:,.2f} por unidad y la OC dice {precio_oc:,.2f} "
                    f"(más de {TOL_PRECIO:.0%} de diferencia). Si es correcto, un administrador puede aceptarla."))
            if user.rol != "admin":
                raise HTTPException(403, "Solo un administrador puede aceptar una diferencia de precio fuera de tolerancia")

        recibido = Decimal(str(round(item.cantidad * precio_oc, 2)))
        sub = Decimal(str(round(item.cantidad * precio, 2)))
        itbis_l = _itbis_compra(sub, linea.impuesto, prov)
        cta_rec = _cuenta_debito_recepcion(linea, prod, r_compra[0])
        clave = (_cuenta_diferencia(linea, prod, cta_rec),
                 linea.unidad_negocio_id or oc.unidad_negocio_id,
                 linea.departamento_id or oc.departamento_id)
        puente += recibido
        if sub != recibido:
            diferencias[clave] = diferencias.get(clave, Decimal("0")) + (sub - recibido)
        if sin_credito and itbis_l > 0:
            itbis_no_deducible[clave] = itbis_no_deducible.get(clave, Decimal("0")) + itbis_l
        subtotal += sub
        itbis += itbis_l
        linea.cantidad_facturada = float(linea.cantidad_facturada or 0) + item.cantidad
        lineas_cxp.append((linea, item.cantidad, precio, sub, itbis_l))

    if not lineas_cxp:
        raise HTTPException(400, "Indique la cantidad facturada de al menos una línea")

    r_itbis = _get_regla_cuentas(db, "compra", "itbis_compra")
    if itbis > 0 and not sin_credito and not r_itbis:
        raise HTTPException(400, "Configure la regla compra / itbis_compra para registrar el crédito fiscal")
    ret_isr = (subtotal * Decimal(str(prov.retencion_isr_pct or 0)) / 100).quantize(Decimal("0.01"))
    ret_itbis = (itbis * Decimal(str(prov.retencion_itbis_pct or 0)) / 100).quantize(Decimal("0.01"))
    cta_ret_isr, cta_ret_itbis = _cuentas_retencion(db)
    if (ret_isr > 0 and not cta_ret_isr) or (ret_itbis > 0 and not cta_ret_itbis):
        raise HTTPException(400, "Configure las cuentas de retención ISR / ITBIS por pagar")
    total = subtotal + itbis - ret_isr - ret_itbis

    cxp = models.CuentaPorPagar(
        numero=get_next("CXP", db), proveedor_id=prov.id, oc_id=oc.oc_id,
        tipo_ncf=tipo_ncf, ncf=ncf, num_factura_proveedor=data.num_factura,
        fecha_factura=fecha,
        fecha_vencimiento=data.fecha_vencimiento or fecha + timedelta(days=prov.condicion_pago_dias or 30),
        subtotal=subtotal, itbis=itbis, retencion_isr=ret_isr, retencion_itbis=ret_itbis,
        total=total, saldo_pendiente=total,
        notas=data.notas or f"Factura de la OC {oc.oc_id}",
    )
    db.add(cxp)
    db.flush()
    for linea, cant, precio, sub, itbis_l in lineas_cxp:
        db.add(models.LineaCxP(
            cxp_id=cxp.id, producto_id=linea.producto_id, oc_linea_id=linea.id,
            cantidad=cant, precio_unitario=precio, descuento_pct=0,
            impuesto=linea.impuesto or "itbis_18", monto_itbis=itbis_l, subtotal=sub,
            cuenta_contable_id=linea.cuenta_contable_id,
        ))

    tercero = str(prov.id)
    lineas = [{"cuenta_id": r_puente[1], "debe": puente, "haber": 0, **dims_oc, "tercero_id": tercero,
               "descripcion_linea": f"Liquida recepción OC {oc.oc_id}"}]
    for (cta, un, dep), m in diferencias.items():
        lado = {"debe": m, "haber": 0} if m > 0 else {"debe": 0, "haber": -m}
        lineas.append({"cuenta_id": cta, **lado, "campo_id": oc.campo_id, "unidad_negocio_id": un,
                       "departamento_id": dep, "descripcion_linea": f"Diferencia de precio factura {ncf}"})
    if itbis > 0:
        if sin_credito:
            for (cta, un, dep), m in itbis_no_deducible.items():
                lineas.append({"cuenta_id": cta, "debe": m, "haber": 0, "campo_id": oc.campo_id,
                               "unidad_negocio_id": un, "departamento_id": dep,
                               "descripcion_linea": f"ITBIS no deducible ({tipo_ncf}) {ncf}"})
        else:
            lineas.append({"cuenta_id": r_itbis[0], "debe": itbis, "haber": 0,
                           "descripcion_linea": f"ITBIS crédito fiscal {ncf}"})
    lineas.append({"cuenta_id": r_compra[1], "debe": 0, "haber": total, **dims_oc, "tercero_id": tercero,
                   "descripcion_linea": f"CxP factura {ncf}"})
    if ret_isr > 0:
        lineas.append({"cuenta_id": cta_ret_isr, "debe": 0, "haber": ret_isr,
                       "descripcion_linea": f"Retención ISR {prov.nombre}"})
    if ret_itbis > 0:
        lineas.append({"cuenta_id": cta_ret_itbis, "debe": 0, "haber": ret_itbis,
                       "descripcion_linea": f"Retención ITBIS {prov.nombre}"})
    asiento = _crear_asiento_auto(db, fecha, "CXP", cxp.numero,
                                  f"Factura {ncf} — {prov.nombre} (OC {oc.oc_id})",
                                  lineas, user.nombre, requerido=True)
    cxp.asiento_id = asiento.id

    # La factura es el devengado: libera el compromiso de la OC por lo que cobra.
    devengado = _devengar_cxp_contra_compromisos(db, cxp, user)
    audit.log(db, user, "FACTURA", "OC", oc.oc_id,
              f"Factura {ncf} ({cxp.numero}) de {prov.nombre}: total RD$ {total:,.2f}",
              {"cxp": cxp.numero, "ncf": ncf, "subtotal": float(subtotal), "itbis": float(itbis),
               "ret_isr": float(ret_isr), "ret_itbis": float(ret_itbis),
               "diferencia_precio": float(sum(diferencias.values(), Decimal("0"))),
               "devengado": float(devengado)})
    return cxp


@router.post("/{oc_id}/factura")
def registrar_factura_oc(oc_id: str, data: FacturaPayload, db: Session = Depends(get_db),
                         current_user: models.Usuario = Depends(auth.require_supervisor)):
    """Registrar la factura del proveedor contra lo recibido de la OC."""
    oc = db.query(models.OrdenCompra).filter(models.OrdenCompra.oc_id == oc_id).first()
    if not oc:
        raise HTTPException(404, "Orden de compra no encontrada")
    if oc.estado not in ("Parcial", "Recibida", "Cerrada"):
        raise HTTPException(400, f"La OC {oc_id} no tiene recepciones que facturar (estado: {oc.estado})")
    prov = _proveedor_de(db, oc)
    if not prov:
        raise HTTPException(400, "La OC no tiene un proveedor registrado")
    import logging as _logging
    try:
        cxp = _registrar_factura(db, oc, prov, data, current_user)
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        _logging.getLogger(__name__).exception("Error registrando factura OC %s", oc_id)
        raise HTTPException(500, "Error al registrar la factura")
    return {"ok": True, "cxp": cxp.numero, "ncf": cxp.ncf, "subtotal": float(cxp.subtotal),
            "itbis": float(cxp.itbis), "retencion_isr": float(cxp.retencion_isr),
            "retencion_itbis": float(cxp.retencion_itbis), "total": float(cxp.total)}


class RecepcionLinea(PydanticBase):
    linea_id: int
    cantidad_recibida: float


class RecepcionPayload(PydanticBase):
    num_factura: Optional[str] = None     # referencia del conduce o factura que acompaña la mercancía
    fecha: Optional[date] = None          # None = hoy; permite registrar una recepción atrasada
    lineas: TList[RecepcionLinea] = []
    factura: Optional[FacturaDatos] = None   # "Recibir y facturar": la factura llegó con la mercancía


@router.post("/{oc_id}/recepcion")
def recibir_oc(oc_id: str, data: RecepcionPayload, db: Session = Depends(get_db),
               current_user: models.Usuario = Depends(auth.require_supervisor)):
    """Recibir mercancía de una OC: entra al inventario (o al gasto) contra la cuenta puente.

    La deuda con el proveedor nace con su factura, no con la recepción. Si la factura llegó
    con la mercancía, `factura` la registra en el mismo paso ("Recibir y facturar").
    """
    oc = db.query(models.OrdenCompra).filter(models.OrdenCompra.oc_id == oc_id).first()
    if not oc:
        raise HTTPException(status_code=404, detail="Orden de compra no encontrada")
    if oc.estado not in ("Aprobada", "Parcial"):
        raise HTTPException(400, f"Solo se puede recibir una OC Aprobada o Parcial (estado actual: {oc.estado})")

    from routers.inventario import _recalc_avg_cost
    import logging as _logging

    hoy = date.today()
    fecha_rec = data.fecha or hoy
    if fecha_rec > hoy:
        raise HTTPException(400, "La fecha de recepción no puede ser futura")
    momento = datetime.now() if fecha_rec == hoy else datetime.combine(fecha_rec, time(12))

    # Una recepción mueve inventario y una obligación con el proveedor: sin las reglas no hay
    # asiento, y sin proveedor la factura no podrá registrarse.
    r_compra = _get_regla_cuentas(db, "compra", "factura_proveedor")
    r_puente = _get_regla_cuentas(db, "compra", "recepcion_por_facturar")
    if not r_compra or not r_puente:
        raise HTTPException(400, "Configure las reglas compra / factura_proveedor y compra / recepcion_por_facturar antes de recibir")
    prov = _proveedor_de(db, oc)
    if not prov:
        raise HTTPException(400, "La OC no tiene un proveedor registrado: asígnelo antes de recibir")

    try:
        lineas_oc = db.query(models.OrdenCompraLinea).filter(models.OrdenCompraLinea.oc_id == oc_id).all()
        por_id = {l.id: l for l in lineas_oc}
        total_recibido_now = Decimal("0")
        received_lineas = []
        debitos: dict = {}   # (cuenta, unidad de negocio, departamento) -> monto
        for item in data.lineas:
            if item.cantidad_recibida <= 0:
                continue
            linea = por_id.get(item.linea_id)
            if not linea:
                raise HTTPException(400, f"La línea {item.linea_id} no pertenece a la OC {oc_id}")
            # Incluye productos desactivados después de hacer la OC: la mercancía llegó igual,
            # y saltarlos dejaba el asiento sin la entrada al inventario.
            prod = db.query(models.Producto).filter(models.Producto.id_prod == linea.producto_id).first()
            nombre = prod.producto if prod else linea.producto_id

            pendiente = float(linea.cantidad or 0) - float(linea.cantidad_recibida or 0)
            if item.cantidad_recibida > pendiente + _EPS:
                raise HTTPException(400, (
                    f"{nombre}: quedan {pendiente:g} por recibir y se intentó recibir "
                    f"{item.cantidad_recibida:g}. No se puede recibir más de lo pedido; "
                    "si llegó más, haga otra OC por la diferencia."))

            # El costo real es el neto: con 10% de descuento cada unidad cuesta 900, no 1.000.
            precio_neto = _precio_neto(linea)
            monto_l = Decimal(str(round(item.cantidad_recibida * precio_neto, 2)))
            linea.cantidad_recibida = float(linea.cantidad_recibida or 0) + item.cantidad_recibida
            total_recibido_now += monto_l
            received_lineas.append({"linea_id": linea.id, "cantidad_recibida": item.cantidad_recibida,
                                    "precio_neto": precio_neto, "monto": float(monto_l)})

            clave = (_cuenta_debito_recepcion(linea, prod, r_compra[0]),
                     linea.unidad_negocio_id or oc.unidad_negocio_id,
                     linea.departamento_id or oc.departamento_id)
            debitos[clave] = debitos.get(clave, Decimal("0")) + monto_l

            if prod and prod.es_inventariable:
                nuevo_costo = _recalc_avg_cost(prod, item.cantidad_recibida, precio_neto)
                nuevo_stock = float(prod.stock_actual or 0) + item.cantidad_recibida
                db.add(models.MovimientoInventario(
                    num_documento=get_next("GR", db),
                    producto_id=linea.producto_id,
                    tipo_doc="GR",
                    tipo="entrada",
                    motivo="Compra",
                    cantidad=item.cantidad_recibida,
                    costo_unitario=round(precio_neto, 4),
                    costo_promedio_post=round(nuevo_costo, 4),
                    stock_post=round(nuevo_stock, 4),
                    proveedor=oc.proveedor,
                    fecha=momento,
                    oc_referencia=oc_id,
                    usuario_id=current_user.id,
                ))
                prod.stock_actual = round(nuevo_stock, 4)
                prod.costo_promedio = round(nuevo_costo, 4)

        if not received_lineas:
            raise HTTPException(400, "Indique la cantidad recibida de al menos una línea")

        oc.total_recibido = float(oc.total_recibido or 0) + float(total_recibido_now)
        oc.fecha_recepcion = momento
        if data.num_factura:
            oc.num_factura = data.num_factura
        all_received = all(float(l.cantidad_recibida or 0) >= float(l.cantidad or 0) - _EPS for l in lineas_oc)
        oc.estado = "Recibida" if all_received else "Parcial"

        dim = {"campo_id": oc.campo_id, "unidad_negocio_id": oc.unidad_negocio_id,
               "departamento_id": oc.departamento_id}
        asiento = _crear_asiento_auto(
            db, fecha_rec, "GR", oc_id,
            f"Recepción OC {oc_id} — {prov.nombre}",
            [{"cuenta_id": cta, "debe": m, "haber": 0, "campo_id": oc.campo_id,
              "unidad_negocio_id": un, "departamento_id": dep,
              "descripcion_linea": f"Recepción OC {oc_id}"}
             for (cta, un, dep), m in debitos.items()] +
            [{"cuenta_id": r_puente[1], "debe": 0, "haber": total_recibido_now, **dim,
              "tercero_id": str(prov.id),
              "descripcion_linea": f"Recibido por facturar OC {oc_id}"}],
            current_user.nombre,
            requerido=True,
        )
        asiento_num = asiento.numero if asiento else None

        cxp = None
        if data.factura:
            db.flush()
            cxp = _registrar_factura(db, oc, prov, FacturaPayload(
                **data.factura.model_dump(),
                lineas=[FacturaLinea(linea_id=rl["linea_id"], cantidad=rl["cantidad_recibida"])
                        for rl in received_lineas]), current_user)

        audit.log(db, current_user, "RECEPCION", "OC", oc_id,
                  f"Recepción OC {oc_id}: {len(received_lineas)} líneas, monto={total_recibido_now:,.2f}" +
                  (f" — facturada ({cxp.numero})" if cxp else " — pendiente de facturar"),
                  {"lineas_recibidas": received_lineas, "total_recibido_now": float(total_recibido_now),
                   "fecha": str(fecha_rec), "num_factura": data.num_factura, "estado_nuevo": oc.estado,
                   "asiento": asiento_num, "cxp": cxp.numero if cxp else None})

        db.commit()
        db.refresh(oc)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        _logging.getLogger(__name__).exception("Error en recepción OC %s", oc_id)
        raise HTTPException(500, "Error al procesar la recepción")
    return {"ok": True, "estado": oc.estado, "total_recibido": oc.total_recibido,
            "num_factura": oc.num_factura, "asiento": asiento_num,
            "cxp": cxp.numero if cxp else None,
            "por_facturar": cxp is None}


@router.put("/{oc_id}")
def update_oc(oc_id: str, data: schemas.OrdenCompraCreate, db: Session = Depends(get_db),
              current_user: models.Usuario = Depends(auth.require_supervisor)):
    """Editar una OC en Borrador o Aprobada.

    Editar una aprobada la devuelve a Borrador y libera su compromiso: el monto nuevo tiene
    que pasar otra vez por el control presupuestario. Antes se podía aprobar por 10.000 y
    editar a 500.000 sin que el presupuesto se enterara. Con recepciones ya hay inventario,
    CxP y asientos que dependen de sus montos, así que ya no se edita.
    """
    oc = db.query(models.OrdenCompra).filter(models.OrdenCompra.oc_id == oc_id).first()
    if not oc:
        raise HTTPException(status_code=404, detail="Orden de compra no encontrada")
    if oc.estado not in ("Borrador", "Aprobada"):
        raise HTTPException(400, (
            f"Una OC {oc.estado} no se puede editar: sus recepciones ya generaron inventario, "
            "CxP y asientos. Para comprar más, duplíquela."))
    if not data.lineas:
        raise HTTPException(400, "La orden de compra debe tener al menos una línea")
    lineas_actuales = db.query(models.OrdenCompraLinea).filter(models.OrdenCompraLinea.oc_id == oc_id).all()
    if any(float(l.cantidad_recibida or 0) > 0 for l in lineas_actuales):
        raise HTTPException(400, "La OC ya tiene recepciones y no se puede editar")

    if data.proveedor_id:
        prov = db.query(models.Proveedor).get(data.proveedor_id)
        if not prov:
            raise HTTPException(400, f"Proveedor ID {data.proveedor_id} no existe")
        if not prov.activo:
            raise HTTPException(400, f"El proveedor '{prov.nombre}' está inactivo")

    try:
        reabierta = oc.estado == "Aprobada"
        if reabierta:
            _liberar_compromisos(db, oc_id, "edición", current_user)
            oc.estado = "Borrador"
            oc.aprobado_por = None
            oc.fecha_aprobacion = None

        oc.fecha = data.fecha or oc.fecha
        if data.proveedor_id:
            oc.proveedor_id = prov.id
            oc.proveedor = prov.nombre
        elif data.proveedor:
            oc.proveedor = data.proveedor
        oc.campo_id = data.campo_id
        oc.unidad_negocio_id = data.unidad_negocio_id
        oc.departamento_id = data.departamento_id
        oc.almacen_id = data.almacen_id
        oc.observaciones = data.observaciones

        antes = float(oc.total_estimado or 0)
        for l in lineas_actuales:
            db.delete(l)
        db.flush()
        oc.total_estimado = round(_agregar_lineas(db, oc_id, data.lineas), 2)

        audit.log(db, current_user, "MODIFICAR", "OC", oc_id,
                  f"OC {oc_id} editada: total {antes:,.2f} → {oc.total_estimado:,.2f}" +
                  (" — vuelve a Borrador, requiere nueva aprobación" if reabierta else ""),
                  {"proveedor": oc.proveedor, "campo_id": oc.campo_id, "total_anterior": antes,
                   "total_estimado": float(oc.total_estimado or 0), "reabierta": reabierta})

        db.commit()
        db.refresh(oc)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise HTTPException(500, "Error al actualizar la orden de compra")
    return {**schemas.OrdenCompraOut.model_validate(oc).model_dump(), "requiere_aprobacion": reabierta}


@router.delete("/{oc_id}")
def delete_oc(oc_id: str, db: Session = Depends(get_db),
              current_user: models.Usuario = Depends(auth.require_admin)):
    """Eliminar una OC en Borrador, o Cancelada sin recepciones.

    Borrar una OC aprobada dejaba su compromiso consumiendo presupuesto para siempre, y
    borrar una recibida dejaba el stock y el asiento vivos pero se llevaba la CxP: el mayor
    de proveedores quedaba con un saldo que ningún documento respaldaba.
    """
    oc = db.query(models.OrdenCompra).filter(models.OrdenCompra.oc_id == oc_id).first()
    if not oc:
        raise HTTPException(status_code=404, detail="Orden de compra no encontrada")
    if oc.estado not in ("Borrador", "Cancelada"):
        raise HTTPException(400, (
            f"Una OC {oc.estado} no se puede eliminar. Cancélela si no tiene recepciones, "
            "o ciérrela si ya las tiene."))
    lineas = db.query(models.OrdenCompraLinea).filter(models.OrdenCompraLinea.oc_id == oc_id).all()
    con_movimientos = db.query(models.MovimientoInventario).filter(
        models.MovimientoInventario.oc_referencia == oc_id).count()
    con_cxp = db.query(models.CuentaPorPagar).filter(models.CuentaPorPagar.oc_id == oc_id).count()
    if con_movimientos or con_cxp or any(float(l.cantidad_recibida or 0) > 0 for l in lineas):
        raise HTTPException(400, "La OC tiene recepciones, CxP o movimientos de inventario y no se puede eliminar")

    audit.log(db, current_user, "ELIMINAR", "OC", oc_id,
              f"OC {oc_id} eliminada: {oc.proveedor or 'Sin proveedor'} — Total era: RD$ {oc.total_estimado or 0:,.2f}",
              {"proveedor": oc.proveedor, "estado": oc.estado,
               "total_estimado": float(oc.total_estimado or 0)})

    # Una cancelada ya liberó su compromiso; sus movimientos presupuestarios se conservan
    # como historial (compromiso y liberación netean a cero).
    db.query(models.CompromisoPresupuestario).filter(
        models.CompromisoPresupuestario.origen_tipo == "OC",
        models.CompromisoPresupuestario.origen_id == oc_id,
    ).delete()
    for l in lineas:
        db.delete(l)
    db.delete(oc)
    db.commit()
    return {"ok": True}


@router.post("/{oc_id}/duplicar")
def duplicar_oc(oc_id: str, db: Session = Depends(get_db),
                current_user: models.Usuario = Depends(auth.require_supervisor)):
    """Clone an OC into a new Borrador."""
    oc = db.query(models.OrdenCompra).filter(models.OrdenCompra.oc_id == oc_id).first()
    if not oc:
        raise HTTPException(404, "Orden de compra no encontrada")
    lineas = db.query(models.OrdenCompraLinea).filter(models.OrdenCompraLinea.oc_id == oc_id).all()

    new_id = get_next("OC", db)
    try:
        new_oc = models.OrdenCompra(
            oc_id=new_id,
            fecha=datetime.now(),
            proveedor=oc.proveedor,
            proveedor_id=oc.proveedor_id,
            campo_id=oc.campo_id,
            unidad_negocio_id=oc.unidad_negocio_id,
            departamento_id=oc.departamento_id,
            almacen_id=oc.almacen_id,
            estado="Borrador",
            observaciones=f"Duplicada de {oc_id}",
        )
        db.add(new_oc)
        db.flush()

        total = 0.0
        for l in lineas:
            sub = float(l.subtotal or 0)
            total += sub
            db.add(models.OrdenCompraLinea(
                oc_id=new_id, producto_id=l.producto_id,
                cantidad=l.cantidad, cantidad_recibida=0,
                precio_unitario=l.precio_unitario,
                descuento_pct=float(l.descuento_pct or 0),
                impuesto=l.impuesto,
                subtotal=sub,
                cuenta_contable_id=l.cuenta_contable_id,
                unidad_negocio_id=l.unidad_negocio_id,
                departamento_id=l.departamento_id,
                almacen_id=l.almacen_id,
            ))
        new_oc.total_estimado = round(total, 2)

        audit.log(db, current_user, "DUPLICAR", "OC", new_id,
                  f"OC {new_id} duplicada de {oc_id} — Total: RD$ {total:,.2f}",
                  {"origen": oc_id, "total_estimado": total, "num_lineas": len(lineas)})

        db.commit()
        db.refresh(new_oc)
    except Exception:
        db.rollback()
        raise HTTPException(500, "Error al duplicar la orden de compra")
    return {"ok": True, "oc_id": new_id, "orden": schemas.OrdenCompraOut.model_validate(new_oc)}


# ─── Devolución a Proveedor ────────────────────────────────────────────────

class DevolucionLinea(PydanticBase):
    linea_id: int
    cantidad_devuelta: float


class DevolucionPayload(PydanticBase):
    motivo: str = "Devolución a proveedor"
    ncf: Optional[str] = None     # NCF de la nota de crédito del proveedor (B04/E34), si ya la envió
    lineas: TList[DevolucionLinea] = []


@router.post("/{oc_id}/devolucion")
def devolver_oc(oc_id: str, data: DevolucionPayload, db: Session = Depends(get_db),
                current_user: models.Usuario = Depends(auth.require_supervisor)):
    """Devolver mercancía recibida al proveedor.

    Lo que aún no estaba facturado solo revierte la recepción contra la cuenta puente. Lo
    ya facturado genera una nota de crédito que reduce la CxP, el ITBIS acreditado y las
    retenciones. Todo o nada, igual que la recepción: sin asiento no se mueve nada.
    """
    oc = db.query(models.OrdenCompra).filter(models.OrdenCompra.oc_id == oc_id).first()
    if not oc:
        raise HTTPException(404, "Orden de compra no encontrada")
    if oc.estado not in ("Parcial", "Recibida", "Cerrada"):
        raise HTTPException(400, f"Solo se puede devolver una OC Parcial/Recibida/Cerrada (estado: {oc.estado})")
    if not data.lineas:
        raise HTTPException(400, "Debe indicar al menos una línea a devolver")
    r_compra = _get_regla_cuentas(db, "compra", "factura_proveedor")
    r_puente = _get_regla_cuentas(db, "compra", "recepcion_por_facturar")
    if not r_compra or not r_puente:
        raise HTTPException(400, "Configure las reglas compra / factura_proveedor y compra / recepcion_por_facturar")
    prov = _proveedor_de(db, oc)
    if not prov:
        raise HTTPException(400, "La OC no tiene un proveedor registrado")
    ncf_nc = None
    if data.ncf:
        ncf_nc, _ = _normalizar_ncf(data.ncf, {"B04": "", "E34": ""}, "una nota de crédito")

    import logging as _logging
    hoy = date.today()

    try:
        por_id = {l.id: l for l in db.query(models.OrdenCompraLinea).filter(
            models.OrdenCompraLinea.oc_id == oc_id).all()}
        total_devuelto = Decimal("0")
        sin_facturar = Decimal("0")      # vuelve contra la cuenta puente
        facturado = []                   # (monto, impuesto, oc_linea_id): va en la nota de crédito
        lineas_devueltas = []
        creditos: dict = {}              # (cuenta, unidad de negocio, departamento) -> monto

        for item in data.lineas:
            if item.cantidad_devuelta <= 0:
                continue
            linea = por_id.get(item.linea_id)
            if not linea:
                raise HTTPException(400, f"Línea {item.linea_id} no encontrada en OC {oc_id}")
            prod = db.query(models.Producto).filter(models.Producto.id_prod == linea.producto_id).first()
            nombre = prod.producto if prod else linea.producto_id

            # cantidad_recibida ya descuenta las devoluciones anteriores (se reduce abajo).
            recibida = float(linea.cantidad_recibida or 0)
            qty = item.cantidad_devuelta
            if qty > recibida + _EPS:
                raise HTTPException(400, (
                    f"{nombre}: quedan {recibida:g} recibidas sin devolver y se intentó devolver {qty:g}"))

            precio = _precio_neto(linea)
            # Primero sale lo que aún no se facturó; el resto ya está en una factura.
            qty_a = min(qty, _pendiente_facturar(linea))
            qty_b = qty - qty_a
            monto_a = Decimal(str(round(qty_a * precio, 2)))
            monto_b = Decimal(str(round(qty_b * precio, 2)))
            monto_linea = monto_a + monto_b

            if prod and prod.es_inventariable:
                stock = float(prod.stock_actual or 0)
                if qty > stock + _EPS:
                    raise HTTPException(400, (
                        f"{nombre}: solo hay {stock:g} en inventario. Lo demás ya se consumió "
                        "y no se puede devolver al proveedor."))
                # Sale al precio al que entró, y el costo promedio de lo que queda se recalcula:
                # sin eso el valor del inventario y su cuenta en el mayor se separaban.
                nuevo_stock = stock - qty
                valor_restante = stock * float(prod.costo_promedio or 0) - float(monto_linea)
                nuevo_costo = (max(0.0, valor_restante) / nuevo_stock if nuevo_stock > _EPS
                               else float(prod.costo_promedio or 0))
                db.add(models.MovimientoInventario(
                    num_documento=get_next("DEV-GR", db),
                    producto_id=linea.producto_id,
                    tipo_doc="DEV-GR",
                    tipo="salida",
                    motivo=data.motivo,
                    cantidad=qty,
                    costo_unitario=round(precio, 4),
                    costo_promedio_post=round(nuevo_costo, 4),
                    stock_post=round(nuevo_stock, 4),
                    proveedor=oc.proveedor,
                    fecha=datetime.now(),
                    oc_referencia=oc_id,
                    usuario_id=current_user.id,
                    observacion=f"Devolución OC {oc_id} — {data.motivo}",
                ))
                prod.stock_actual = round(nuevo_stock, 4)
                prod.costo_promedio = round(nuevo_costo, 4)

            linea.cantidad_recibida = recibida - qty
            linea.cantidad_facturada = max(0.0, float(linea.cantidad_facturada or 0) - qty_b)
            total_devuelto += monto_linea
            sin_facturar += monto_a
            if monto_b > 0:
                facturado.append((monto_b, linea.impuesto, linea.id))
            clave = (_cuenta_debito_recepcion(linea, prod, r_compra[0]),
                     linea.unidad_negocio_id or oc.unidad_negocio_id,
                     linea.departamento_id or oc.departamento_id)
            creditos[clave] = creditos.get(clave, Decimal("0")) + monto_linea
            lineas_devueltas.append({
                "linea_id": linea.id,
                "producto_id": linea.producto_id,
                "cantidad_devuelta": qty,
                "cantidad_ya_facturada": qty_b,
                "monto": float(monto_linea),
            })

        if not lineas_devueltas:
            raise HTTPException(400, "No se procesaron líneas de devolución")

        oc.total_recibido = max(Decimal("0"), Decimal(str(oc.total_recibido or 0)) - total_devuelto)
        # Lo devuelto puede reponerse: la OC vuelve a quedar con cantidad pendiente.
        if oc.estado == "Recibida":
            oc.estado = "Parcial"

        dim = {"campo_id": oc.campo_id, "unidad_negocio_id": oc.unidad_negocio_id,
               "departamento_id": oc.departamento_id}
        tercero = str(prov.id)
        asiento_lineas = []
        if sin_facturar > 0:
            asiento_lineas.append({"cuenta_id": r_puente[1], "debe": sin_facturar, "haber": 0, **dim,
                                   "tercero_id": tercero,
                                   "descripcion_linea": f"Reverso de recepción sin facturar OC {oc_id}"})

        nc = cxp = None
        presup_reversado = Decimal("0")
        if facturado:
            sub_b = sum((m for m, _, _ in facturado), Decimal("0"))
            itbis_b = sum((_itbis_compra(m, imp, prov) for m, imp, _ in facturado), Decimal("0"))
            ret_isr_b = (sub_b * Decimal(str(prov.retencion_isr_pct or 0)) / 100).quantize(Decimal("0.01"))
            ret_itbis_b = (itbis_b * Decimal(str(prov.retencion_itbis_pct or 0)) / 100).quantize(Decimal("0.01"))
            neto_b = sub_b + itbis_b - ret_isr_b - ret_itbis_b

            # La NC va a una factura de la OC con saldo; mejor si es la que cobró esas líneas.
            ids_devueltos = {lid for _, _, lid in facturado}
            abiertas = db.query(models.CuentaPorPagar).filter(
                models.CuentaPorPagar.oc_id == oc_id,
                models.CuentaPorPagar.estado.in_(("pendiente", "parcial")),
            ).all()

            def _cobra_lineas(c):
                return db.query(models.LineaCxP).filter(models.LineaCxP.cxp_id == c.id,
                                                        models.LineaCxP.oc_linea_id.in_(ids_devueltos)).count()
            abiertas.sort(key=lambda c: (_cobra_lineas(c) == 0, -float(c.saldo_pendiente or 0)))
            cxp = abiertas[0] if abiertas else None
            saldo = Decimal(str(cxp.saldo_pendiente or 0)) if cxp else Decimal("0")
            if neto_b > saldo + Decimal("0.005"):
                raise HTTPException(400, (
                    f"Parte de lo devuelto ya estaba facturado: la nota de crédito reduce RD$ {neto_b:,.2f} "
                    f"y las facturas de esta OC solo tienen RD$ {saldo:,.2f} pendientes de pago. El exceso "
                    "sería un saldo a favor con el proveedor, que aún no se puede registrar: coordine el "
                    "reembolso y regístrelo como nota de crédito manual."))

            r_itbis = _get_regla_cuentas(db, "compra", "itbis_compra")
            if itbis_b > 0 and not r_itbis:
                raise HTTPException(400, "Configure la regla compra / itbis_compra")
            cta_ret_isr, cta_ret_itbis = _cuentas_retencion(db)
            if (ret_isr_b > 0 and not cta_ret_isr) or (ret_itbis_b > 0 and not cta_ret_itbis):
                raise HTTPException(400, "Configure las cuentas de retención ISR / ITBIS por pagar")

            nc = models.NotaCredito(
                numero=get_next("NC", db), tipo="proveedor", proveedor_id=prov.id,
                cxp_id=cxp.id, referencia_id=cxp.id, estado="activa", ncf=ncf_nc,
                fecha=hoy, motivo=data.motivo,
                subtotal=sub_b, itbis=itbis_b, total=sub_b + itbis_b,
            )
            db.add(nc)
            db.flush()
            cxp.saldo_pendiente = max(Decimal("0"), saldo - neto_b)
            if cxp.saldo_pendiente <= Decimal("0.005"):
                cxp.saldo_pendiente = Decimal("0")
                cxp.estado = "pagada"
            else:
                cxp.estado = "parcial"
            presup_reversado = _reversar_devengado_cxp(
                db, cxp, _monto_presupuestario(sub_b, itbis_b, prov, cxp.tipo_ncf), hoy,
                origen_tipo="DEV-GR", origen_id=nc.numero,
                notas=f"Reverso por devolución OC {oc_id} — NC {nc.numero}",
                user=current_user,
            )
            asiento_lineas.append({"cuenta_id": r_compra[1], "debe": neto_b, "haber": 0, **dim,
                                   "tercero_id": tercero,
                                   "descripcion_linea": f"NC {nc.numero} reduce CxP {cxp.numero}"})
            if ret_isr_b > 0:
                asiento_lineas.append({"cuenta_id": cta_ret_isr, "debe": ret_isr_b, "haber": 0,
                                       "descripcion_linea": f"Reverso retención ISR NC {nc.numero}"})
            if ret_itbis_b > 0:
                asiento_lineas.append({"cuenta_id": cta_ret_itbis, "debe": ret_itbis_b, "haber": 0,
                                       "descripcion_linea": f"Reverso retención ITBIS NC {nc.numero}"})
            if itbis_b > 0:
                asiento_lineas.append({"cuenta_id": r_itbis[0], "debe": 0, "haber": itbis_b,
                                       "descripcion_linea": f"Reverso ITBIS crédito fiscal NC {nc.numero}"})

        # Espejo de la recepción: sale por las mismas cuentas por las que entró.
        asiento_lineas += [{"cuenta_id": cta, "debe": 0, "haber": m, "campo_id": oc.campo_id,
                            "unidad_negocio_id": un, "departamento_id": dep,
                            "descripcion_linea": f"Devolución OC {oc_id}"}
                           for (cta, un, dep), m in creditos.items()]
        asiento = _crear_asiento_auto(
            db, hoy, "DEV-GR", oc_id, f"Devolución OC {oc_id} — {prov.nombre}",
            asiento_lineas, current_user.nombre, requerido=True,
        )
        if nc:
            nc.asiento_id = asiento.id

        audit.log(db, current_user, "DEVOLUCION", "OC", oc_id,
                  f"Devolución {len(lineas_devueltas)} líneas — Total: RD$ {total_devuelto:,.2f}" +
                  (f" — NC {nc.numero}" if nc else " — sin facturar, sin nota de crédito"),
                  {"lineas": lineas_devueltas, "nc": nc.numero if nc else None,
                   "cxp_ajustada": cxp.numero if cxp else None})

        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        _logging.getLogger(__name__).exception("Error en devolución OC %s", oc_id)
        raise HTTPException(500, "Error al procesar la devolución")

    return {
        "ok": True,
        "lineas_devueltas": lineas_devueltas,
        "total_devuelto": float(total_devuelto),
        "nc_numero": nc.numero if nc else None,
        "cxp_ajustada": cxp.numero if cxp else None,
        "presupuesto_reversado": float(presup_reversado),
        "asiento": asiento.numero if asiento else None,
    }


# ─── Reportes y ajustes de la factura separada ─────────────────────────────

@router.get("/reportes/por-facturar")
def reporte_por_facturar(db: Session = Depends(get_db), _=Depends(auth.get_current_user)):
    """Mercancía recibida que el proveedor aún no ha facturado, contra el saldo de la cuenta puente.

    Las dos cifras deben coincidir: es la conciliación de la cuenta "Compras recibidas por facturar".
    """
    filas = []
    for l, oc in db.query(models.OrdenCompraLinea, models.OrdenCompra).join(
            models.OrdenCompra, models.OrdenCompra.oc_id == models.OrdenCompraLinea.oc_id).all():
        pend = _pendiente_facturar(l)
        if pend <= _EPS:
            continue
        prod = db.query(models.Producto).filter(models.Producto.id_prod == l.producto_id).first()
        filas.append({
            "oc_id": oc.oc_id, "proveedor": oc.proveedor, "fecha_recepcion": oc.fecha_recepcion,
            "linea_id": l.id, "producto_id": l.producto_id, "producto": prod.producto if prod else l.producto_id,
            "cantidad": round(pend, 4), "precio_neto": round(_precio_neto(l), 4),
            "valor": round(pend * _precio_neto(l), 2),
        })
    r_puente = _get_regla_cuentas(db, "compra", "recepcion_por_facturar")
    saldo_mayor = None
    if r_puente:
        d, h = db.query(sqlfunc.coalesce(sqlfunc.sum(models.LineaAsiento.debe), 0),
                        sqlfunc.coalesce(sqlfunc.sum(models.LineaAsiento.haber), 0)).join(
            models.AsientoContable, models.AsientoContable.id == models.LineaAsiento.asiento_id).filter(
            models.LineaAsiento.cuenta_id == r_puente[1],
            models.AsientoContable.estado != "anulado").one()
        saldo_mayor = round(float(h or 0) - float(d or 0), 2)
    total = round(sum(f["valor"] for f in filas), 2)
    return {"total": total, "saldo_mayor": saldo_mayor,
            "diferencia": round(total - saldo_mayor, 2) if saldo_mayor is not None else None,
            "items": sorted(filas, key=lambda f: (str(f["fecha_recepcion"] or ""), f["oc_id"]))}


@router.post("/ajuste-asientos-recepciones")
def ajuste_asientos_recepciones(dry_run: bool = Query(True), fecha: Optional[date] = None,
                                db: Session = Depends(get_db),
                                current_user: models.Usuario = Depends(auth.require_admin)):
    """Completa el asiento de las facturas que creaba la recepción antes de la fase 2.

    Aquellas recepciones acreditaban la CxP solo por el subtotal: faltaba el ITBIS como
    crédito fiscal y las retenciones, y el mayor de proveedores quedaba descuadrado contra
    el auxiliar. Por cada una se crea un asiento complementario. En modo prueba solo lista.
    """
    r_compra = _get_regla_cuentas(db, "compra", "factura_proveedor")
    r_itbis = _get_regla_cuentas(db, "compra", "itbis_compra")
    cta_ret_isr, cta_ret_itbis = _cuentas_retencion(db)
    if not r_compra:
        raise HTTPException(400, "Configure la regla compra / factura_proveedor")
    fecha = fecha or date.today()

    ya = {a.referencia_id for a in db.query(models.AsientoContable).filter(
        models.AsientoContable.origen == "AJ-CXP", models.AsientoContable.estado != "anulado").all()}
    items, faltan_cuentas = [], []
    for cxp in db.query(models.CuentaPorPagar).filter(
            models.CuentaPorPagar.oc_id.isnot(None), models.CuentaPorPagar.asiento_id.isnot(None),
            models.CuentaPorPagar.estado != "anulada").order_by(models.CuentaPorPagar.id).all():
        a = db.query(models.AsientoContable).get(cxp.asiento_id)
        # Solo las que nacieron de una recepción (su asiento es el GR de la OC).
        if not a or a.origen != "GR" or cxp.numero in ya:
            continue
        itbis = Decimal(str(cxp.itbis or 0))
        ret_isr = Decimal(str(cxp.retencion_isr or 0))
        ret_itbis = Decimal(str(cxp.retencion_itbis or 0))
        if itbis <= 0 and ret_isr <= 0 and ret_itbis <= 0:
            continue
        neto_cxp = itbis - ret_isr - ret_itbis
        item = {"cxp": cxp.numero, "oc_id": cxp.oc_id, "fecha_factura": str(cxp.fecha_factura),
                "itbis": float(itbis), "retencion_isr": float(ret_isr), "retencion_itbis": float(ret_itbis),
                "ajuste_cxp": float(neto_cxp)}
        if (itbis > 0 and not r_itbis) or (ret_isr > 0 and not cta_ret_isr) or (ret_itbis > 0 and not cta_ret_itbis):
            faltan_cuentas.append(cxp.numero)
            continue
        items.append(item)
        if dry_run:
            continue
        lineas = []
        if itbis > 0:
            lineas.append({"cuenta_id": r_itbis[0], "debe": itbis, "haber": 0,
                           "descripcion_linea": f"ITBIS crédito fiscal no registrado — {cxp.numero}"})
        if neto_cxp != 0:
            lado = {"debe": 0, "haber": neto_cxp} if neto_cxp > 0 else {"debe": -neto_cxp, "haber": 0}
            lineas.append({"cuenta_id": r_compra[1], **lado, "tercero_id": str(cxp.proveedor_id),
                           "descripcion_linea": f"Complemento CxP {cxp.numero}"})
        if ret_isr > 0:
            lineas.append({"cuenta_id": cta_ret_isr, "debe": 0, "haber": ret_isr,
                           "descripcion_linea": f"Retención ISR — {cxp.numero}"})
        if ret_itbis > 0:
            lineas.append({"cuenta_id": cta_ret_itbis, "debe": 0, "haber": ret_itbis,
                           "descripcion_linea": f"Retención ITBIS — {cxp.numero}"})
        _crear_asiento_auto(db, fecha, "AJ-CXP", cxp.numero,
                            f"Complemento de ITBIS y retenciones de {cxp.numero} (OC {cxp.oc_id})",
                            lineas, current_user.nombre, requerido=True)

    resumen = {
        "dry_run": dry_run, "fecha": str(fecha), "facturas": len(items),
        "itbis": round(sum(i["itbis"] for i in items), 2),
        "retencion_isr": round(sum(i["retencion_isr"] for i in items), 2),
        "retencion_itbis": round(sum(i["retencion_itbis"] for i in items), 2),
        "ajuste_cxp": round(sum(i["ajuste_cxp"] for i in items), 2),
        "sin_cuentas_configuradas": faltan_cuentas, "items": items,
    }
    if not dry_run:
        audit.log(db, current_user, "AJUSTE", "CXP", "recepciones",
                  f"Complemento de ITBIS/retenciones en {len(items)} facturas de recepciones anteriores",
                  {k: v for k, v in resumen.items() if k != "items"})
        db.commit()
    return resumen
