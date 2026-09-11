"""
Órdenes de Compra (OC) — CRUD completo con líneas de producto.
H-16 FIX: Implementación del ciclo de compra vinculado a GR.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from database import get_db
import models, schemas, auth
from routers.sequences import get_next, peek_next
from routers.contabilidad import _crear_asiento_auto, _get_regla_cuentas, _verificar_presupuesto, _registrar_mov_pres
from typing import List, Optional
from datetime import datetime
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
    dimension: str = Query("campo", regex="^(campo|unidad_negocio|departamento)$"),
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


@router.post("")
def create_oc(data: schemas.OrdenCompraCreate, db: Session = Depends(get_db),
              current_user: models.Usuario = Depends(auth.require_supervisor)):
    if not data.lineas:
        raise HTTPException(status_code=400, detail="La orden de compra debe tener al menos una línea")

    oc_id = get_next("OC", db)
    total = 0.0

    nombre_proveedor = data.proveedor
    prov_id = data.proveedor_id
    if prov_id and not nombre_proveedor:
        prov = db.query(models.Proveedor).filter(models.Proveedor.id == prov_id).first()
        if prov:
            nombre_proveedor = prov.nombre

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

        for linea in data.lineas:
            prod = db.query(models.Producto).filter(
                models.Producto.id_prod == linea.producto_id, models.Producto.activo == True
            ).first()
            if not prod:
                raise HTTPException(status_code=400, detail=f"Producto '{linea.producto_id}' no existe")
            if linea.cantidad <= 0:
                raise HTTPException(status_code=400, detail=f"Cantidad debe ser mayor a 0 para '{prod.producto}'")
            if linea.precio_unitario < 0:
                raise HTTPException(status_code=400, detail=f"Precio no puede ser negativo para '{prod.producto}'")

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
                cuenta_contable_id=linea.cuenta_contable_id,
                unidad_negocio_id=linea.unidad_negocio_id,
                departamento_id=linea.departamento_id,
                almacen_id=linea.almacen_id,
            ))

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
        "numero": c.numero, "fecha": str(c.fecha_factura),
        "total": float(c.total or 0), "saldo": float(c.saldo_pendiente or 0),
        "estado": c.estado, "num_factura": c.num_factura_proveedor,
    } for c in cxp_list]

    return {
        "orden": schemas.OrdenCompraOut.model_validate(oc),
        "lineas": lineas_out,
        "asiento_contable": asiento_info,
        "compromiso": compromiso_info,
        "cuentas_por_pagar": cxp_out,
    }


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

    if r_compra and total > 0:
        fecha_oc = oc.fecha or datetime.now()
        fecha_check = fecha_oc.date() if hasattr(fecha_oc, 'date') else fecha_oc
        ver = _verificar_presupuesto(db, [{
            "cuenta_id": r_compra[0], "debe": total, "haber": 0,
            "campo_id": oc.campo_id,
            "unidad_negocio_id": oc.unidad_negocio_id,
            "departamento_id": oc.departamento_id,
        }], fecha_check)

        if ver.get("bloqueado") and not override:
            raise HTTPException(400, {
                "detail": "Presupuesto insuficiente — aprobación bloqueada",
                "alertas": ver.get("alertas", []),
                "requiere_override": True,
            })
        if ver.get("bloqueado") and override and current_user.rol != "admin":
            raise HTTPException(403, "Solo un administrador puede autorizar sobregiro presupuestario")

        comp_anio = fecha_oc.year if hasattr(fecha_oc, 'year') else datetime.now().year
        comp_mes = fecha_oc.month if hasattr(fecha_oc, 'month') else datetime.now().month
        comp_monto = Decimal(str(round(total, 2)))
        db.add(models.CompromisoPresupuestario(
            anio=comp_anio, mes=comp_mes,
            cuenta_id=r_compra[0],
            campo_id=oc.campo_id,
            unidad_negocio_id=oc.unidad_negocio_id,
            departamento_id=oc.departamento_id,
            monto=comp_monto,
            origen_tipo="OC", origen_id=oc_id, estado="activo",
        ))
        _registrar_mov_pres(
            db, tipo="COMPROMISO", fecha=fecha_check,
            cuenta_id=r_compra[0], monto=comp_monto,
            anio=comp_anio, mes=comp_mes,
            campo_id=oc.campo_id, unidad_negocio_id=oc.unidad_negocio_id,
            departamento_id=oc.departamento_id,
            origen_tipo="OC", origen_id=oc_id,
            notas=f"Compromiso OC {oc_id}",
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
    alertas = ver.get("alertas", []) if r_compra and total > 0 else []
    return {"ok": True, "estado": "Aprobada", "alertas_presupuesto": alertas}


@router.post("/{oc_id}/cerrar")
def cerrar_oc(oc_id: str, db: Session = Depends(get_db),
              current_user: models.Usuario = Depends(auth.require_supervisor)):
    """Cerrar OC: libera compromiso presupuestario remanente."""
    oc = db.query(models.OrdenCompra).filter(models.OrdenCompra.oc_id == oc_id).first()
    if not oc:
        raise HTTPException(404, "Orden de compra no encontrada")
    if oc.estado not in ("Aprobada", "Parcial", "Recibida"):
        raise HTTPException(400, f"Solo se puede cerrar una OC Aprobada, Parcial o Recibida (estado actual: {oc.estado})")

    comps_cerrar = db.query(models.CompromisoPresupuestario).filter(
        models.CompromisoPresupuestario.origen_tipo == "OC",
        models.CompromisoPresupuestario.origen_id == oc_id,
        models.CompromisoPresupuestario.estado == "activo",
    ).all()
    for comp in comps_cerrar:
        comp.estado = "cancelado"
        _registrar_mov_pres(
            db, tipo="LIBERACION", fecha=datetime.now().date(),
            cuenta_id=comp.cuenta_id, monto=-comp.monto,
            anio=comp.anio, mes=comp.mes,
            campo_id=comp.campo_id, unidad_negocio_id=comp.unidad_negocio_id,
            departamento_id=comp.departamento_id,
            origen_tipo="OC", origen_id=oc_id,
            notas=f"Liberación por cierre OC {oc_id}",
            usuario_id=current_user.id,
        )

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
        comps_cancel = db.query(models.CompromisoPresupuestario).filter(
            models.CompromisoPresupuestario.origen_tipo == "OC",
            models.CompromisoPresupuestario.origen_id == oc_id,
            models.CompromisoPresupuestario.estado == "activo",
        ).all()
        for comp in comps_cancel:
            comp.estado = "cancelado"
            _registrar_mov_pres(
                db, tipo="LIBERACION", fecha=datetime.now().date(),
                cuenta_id=comp.cuenta_id, monto=-comp.monto,
                anio=comp.anio, mes=comp.mes,
                campo_id=comp.campo_id, unidad_negocio_id=comp.unidad_negocio_id,
                departamento_id=comp.departamento_id,
                origen_tipo="OC", origen_id=oc_id,
                notas=f"Liberación por cancelación OC {oc_id}",
                usuario_id=current_user.id,
            )
        oc.estado = "Cancelada"
        audit.log(db, current_user, "CANCELAR", "OC", oc_id,
                  f"OC {oc_id} cancelada por {current_user.nombre}",
                  {"estado_anterior": oc.estado})
        db.commit()
        return {"ok": True, "estado": "Cancelada"}

    raise HTTPException(400, "Use /aprobar para aprobar o /cerrar para cerrar. Solo se permite cancelar vía este endpoint.")


from pydantic import BaseModel as PydanticBase
from typing import List as TList

class RecepcionLinea(PydanticBase):
    linea_id: int
    cantidad_recibida: float

class RecepcionPayload(PydanticBase):
    num_factura: Optional[str] = None
    lineas: TList[RecepcionLinea] = []


@router.post("/{oc_id}/recepcion")
def recibir_oc(oc_id: str, data: RecepcionPayload, db: Session = Depends(get_db),
               current_user: models.Usuario = Depends(auth.require_supervisor)):
    """Receive items against an OC — updates quantities, generates GR for inventariables."""
    oc = db.query(models.OrdenCompra).filter(models.OrdenCompra.oc_id == oc_id).first()
    if not oc:
        raise HTTPException(status_code=404, detail="Orden de compra no encontrada")
    if oc.estado not in ("Aprobada", "Parcial"):
        raise HTTPException(400, f"Solo se puede recibir una OC Aprobada o Parcial (estado actual: {oc.estado})")

    from routers.inventario import _recalc_avg_cost
    from routers.sequences import get_next
    import logging as _logging

    try:
        total_recibido_now = 0.0
        received_lineas = []
        for item in data.lineas:
            if item.cantidad_recibida <= 0:
                continue
            linea = db.query(models.OrdenCompraLinea).filter(
                models.OrdenCompraLinea.id == item.linea_id,
                models.OrdenCompraLinea.oc_id == oc_id
            ).first()
            if not linea:
                continue

            linea.cantidad_recibida = (linea.cantidad_recibida or 0) + item.cantidad_recibida
            total_recibido_now += item.cantidad_recibida * linea.precio_unitario
            received_lineas.append({"linea_id": linea.id, "cantidad_recibida": item.cantidad_recibida})

            prod = db.query(models.Producto).filter(
                models.Producto.id_prod == linea.producto_id, models.Producto.activo == True
            ).first()
            if prod and prod.es_inventariable:
                nuevo_costo = _recalc_avg_cost(prod, item.cantidad_recibida, linea.precio_unitario)
                nuevo_stock = (prod.stock_actual or 0) + item.cantidad_recibida
                num_doc = get_next("GR", db)

                mov = models.MovimientoInventario(
                    num_documento=num_doc,
                    producto_id=linea.producto_id,
                    tipo_doc="GR",
                    tipo="entrada",
                    motivo="Compra",
                    cantidad=item.cantidad_recibida,
                    costo_unitario=linea.precio_unitario,
                    costo_promedio_post=round(nuevo_costo, 4),
                    stock_post=round(nuevo_stock, 4),
                    proveedor=oc.proveedor,
                    fecha=datetime.now(),
                    oc_referencia=oc_id,
                    usuario_id=current_user.id,
                )
                db.add(mov)
                prod.stock_actual = round(nuevo_stock, 4)
                prod.costo_promedio = round(nuevo_costo, 4)

        oc.total_recibido = (oc.total_recibido or 0) + total_recibido_now
        oc.fecha_recepcion = datetime.now()
        if data.num_factura:
            oc.num_factura = data.num_factura

        lineas_oc = db.query(models.OrdenCompraLinea).filter(models.OrdenCompraLinea.oc_id == oc_id).all()
        all_received = all((l.cantidad_recibida or 0) >= l.cantidad for l in lineas_oc)
        any_received = any((l.cantidad_recibida or 0) > 0 for l in lineas_oc)

        if all_received:
            oc.estado = "Recibida"
        elif any_received:
            oc.estado = "Parcial"

        asiento_num = None
        cxp_numero = None
        if total_recibido_now > 0:
            monto = Decimal(str(round(total_recibido_now, 2)))
            r_compra = _get_regla_cuentas(db, "compra", "factura_proveedor")
            if r_compra:
                dim = {"campo_id": oc.campo_id, "unidad_negocio_id": oc.unidad_negocio_id, "departamento_id": oc.departamento_id}
                asiento = _crear_asiento_auto(
                    db, datetime.now().date(), "GR", oc_id,
                    f"Recepción OC {oc_id} — {oc.proveedor or 'Proveedor'}",
                    [
                        {"cuenta_id": r_compra[0], "debe": monto, "haber": 0,
                         **dim,
                         "descripcion_linea": f"Entrada inventario OC {oc_id}"},
                        {"cuenta_id": r_compra[1], "debe": 0, "haber": monto,
                         **dim,
                         "descripcion_linea": f"CxP recepción OC {oc_id}"},
                    ],
                    current_user.nombre
                )
                if asiento:
                    asiento_num = asiento.numero

            prov = None
            if oc.proveedor_id:
                prov = db.query(models.Proveedor).get(oc.proveedor_id)
            if not prov and oc.proveedor:
                prov = db.query(models.Proveedor).filter(
                    models.Proveedor.nombre == oc.proveedor,
                    models.Proveedor.activo == True,
                ).first()

            if prov:
                from datetime import timedelta
                cxp_num = get_next("CXP", db)
                fecha_hoy = datetime.now().date()
                vencimiento = fecha_hoy + timedelta(days=prov.condicion_pago_dias or 30)

                itbis_pct = Decimal("0.18")
                itbis_monto = round(monto * itbis_pct, 2)
                isr_pct = Decimal(str(prov.retencion_isr_pct or 0))
                itbis_ret_pct = Decimal(str(prov.retencion_itbis_pct or 0))
                ret_isr = round(monto * isr_pct / 100, 2)
                ret_itbis = round(itbis_monto * itbis_ret_pct / 100, 2)
                total_cxp = monto + itbis_monto - ret_isr - ret_itbis

                cxp = models.CuentaPorPagar(
                    numero=cxp_num,
                    proveedor_id=prov.id,
                    oc_id=oc_id,
                    tipo_ncf=prov.tipo_ncf_default or "E41",
                    num_factura_proveedor=data.num_factura,
                    fecha_factura=fecha_hoy,
                    fecha_vencimiento=vencimiento,
                    subtotal=monto,
                    itbis=itbis_monto,
                    retencion_isr=ret_isr,
                    retencion_itbis=ret_itbis,
                    total=total_cxp,
                    saldo_pendiente=total_cxp,
                    asiento_id=asiento.id if asiento else None,
                    notas=f"Generada automáticamente desde recepción OC {oc_id}",
                )
                db.add(cxp)
                db.flush()

                for rl in received_lineas:
                    oc_l = next((o for o in lineas_oc if o.id == rl["linea_id"]), None)
                    if oc_l:
                        sub_l = Decimal(str(rl["cantidad_recibida"])) * Decimal(str(oc_l.precio_unitario or 0))
                        imp = oc_l.impuesto or "itbis_18"
                        rate = Decimal("0.18") if imp == "itbis_18" else Decimal("0")
                        db.add(models.LineaCxP(
                            cxp_id=cxp.id,
                            producto_id=oc_l.producto_id,
                            oc_linea_id=oc_l.id,
                            cantidad=rl["cantidad_recibida"],
                            precio_unitario=float(oc_l.precio_unitario or 0),
                            descuento_pct=float(oc_l.descuento_pct or 0),
                            impuesto=imp,
                            monto_itbis=round(sub_l * rate, 2),
                            subtotal=round(sub_l, 2),
                        ))

                cxp_numero = cxp_num

        audit.log(db, current_user, "RECEPCION", "OC", oc_id,
                  f"Recepción OC {oc_id}: {len(received_lineas)} líneas, monto={total_recibido_now:,.2f}",
                  {"lineas_recibidas": received_lineas, "total_recibido_now": total_recibido_now,
                   "num_factura": data.num_factura, "estado_nuevo": oc.estado,
                   "asiento": asiento_num, "cxp": cxp_numero})

        db.commit()
        db.refresh(oc)
    except Exception:
        db.rollback()
        _logging.getLogger(__name__).exception("Error en recepción OC %s", oc_id)
        raise HTTPException(500, "Error al procesar la recepción")
    return {"ok": True, "estado": oc.estado, "total_recibido": oc.total_recibido,
            "num_factura": oc.num_factura, "asiento": asiento_num, "cxp": cxp_numero}


@router.put("/{oc_id}")
def update_oc(oc_id: str, data: schemas.OrdenCompraCreate, db: Session = Depends(get_db),
              current_user: models.Usuario = Depends(auth.get_current_user)):
    """Edit OC — admin can edit even closed OCs."""
    oc = db.query(models.OrdenCompra).filter(models.OrdenCompra.oc_id == oc_id).first()
    if not oc:
        raise HTTPException(status_code=404, detail="Orden de compra no encontrada")

    if oc.estado not in ("Borrador", "Aprobada") and current_user.rol != "admin":
        raise HTTPException(403, f"Solo un administrador puede editar OCs en estado {oc.estado}")

    oc.fecha = data.fecha or oc.fecha
    if data.proveedor_id:
        prov = db.query(models.Proveedor).filter(models.Proveedor.id == data.proveedor_id).first()
        if prov:
            oc.proveedor_id = data.proveedor_id
            oc.proveedor = prov.nombre
    elif data.proveedor:
        oc.proveedor = data.proveedor
    oc.campo_id = data.campo_id
    oc.unidad_negocio_id = data.unidad_negocio_id
    oc.departamento_id = data.departamento_id
    oc.almacen_id = data.almacen_id
    oc.observaciones = data.observaciones

    try:
        if data.lineas:
            has_receptions = any(
                (l.cantidad_recibida or 0) > 0
                for l in db.query(models.OrdenCompraLinea).filter(
                    models.OrdenCompraLinea.oc_id == oc_id
                ).all()
            )
            if has_receptions and oc.estado != "Borrador":
                existing = {l.producto_id: l for l in db.query(models.OrdenCompraLinea).filter(
                    models.OrdenCompraLinea.oc_id == oc_id).all()}
                total = 0.0
                for linea in data.lineas:
                    prod = db.query(models.Producto).filter(
                        models.Producto.id_prod == linea.producto_id, models.Producto.activo == True
                    ).first()
                    if not prod:
                        raise HTTPException(400, f"Producto '{linea.producto_id}' no existe")
                    desc = float(linea.descuento_pct or 0)
                    subtotal = round(linea.cantidad * linea.precio_unitario * (1 - desc / 100), 2)
                    total += subtotal
                    if linea.producto_id in existing:
                        ex = existing[linea.producto_id]
                        ex.cantidad = linea.cantidad
                        ex.precio_unitario = linea.precio_unitario
                        ex.descuento_pct = desc
                        ex.impuesto = linea.impuesto or prod.impuesto_compra or "itbis_18"
                        ex.subtotal = subtotal
                        ex.cuenta_contable_id = linea.cuenta_contable_id
                        ex.unidad_negocio_id = linea.unidad_negocio_id
                        ex.departamento_id = linea.departamento_id
                        ex.almacen_id = linea.almacen_id
                oc.total_estimado = round(total, 2)
            else:
                db.query(models.OrdenCompraLinea).filter(models.OrdenCompraLinea.oc_id == oc_id).delete()
                total = 0.0
                for linea in data.lineas:
                    prod = db.query(models.Producto).filter(
                        models.Producto.id_prod == linea.producto_id, models.Producto.activo == True
                    ).first()
                    if not prod:
                        raise HTTPException(400, f"Producto '{linea.producto_id}' no existe")
                    desc = float(linea.descuento_pct or 0)
                    subtotal = round(linea.cantidad * linea.precio_unitario * (1 - desc / 100), 2)
                    total += subtotal
                    db.add(models.OrdenCompraLinea(
                        oc_id=oc_id, producto_id=linea.producto_id,
                        cantidad=linea.cantidad, cantidad_recibida=0,
                        precio_unitario=linea.precio_unitario,
                        descuento_pct=desc,
                        impuesto=linea.impuesto or prod.impuesto_compra or "itbis_18",
                        subtotal=subtotal,
                        cuenta_contable_id=linea.cuenta_contable_id,
                        unidad_negocio_id=linea.unidad_negocio_id,
                        departamento_id=linea.departamento_id,
                        almacen_id=linea.almacen_id,
                    ))
                oc.total_estimado = round(total, 2)

            comp = db.query(models.CompromisoPresupuestario).filter(
                models.CompromisoPresupuestario.origen_tipo == "OC",
                models.CompromisoPresupuestario.origen_id == oc_id,
                models.CompromisoPresupuestario.estado == "activo",
            ).first()
            if comp:
                comp.monto = Decimal(str(round(total, 2)))
                comp.campo_id = data.campo_id
                comp.unidad_negocio_id = data.unidad_negocio_id
                comp.departamento_id = data.departamento_id

        audit.log(db, current_user, "MODIFICAR", "OC", oc_id,
                  f"OC {oc_id} editada: proveedor={oc.proveedor}, campo={oc.campo_id}",
                  {"proveedor": oc.proveedor, "campo_id": oc.campo_id,
                   "total_estimado": float(oc.total_estimado or 0)})

        db.commit()
        db.refresh(oc)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise HTTPException(500, "Error al actualizar la orden de compra")
    return oc


@router.delete("/{oc_id}")
def delete_oc(oc_id: str, db: Session = Depends(get_db),
              current_user: models.Usuario = Depends(auth.require_admin)):
    oc = db.query(models.OrdenCompra).filter(models.OrdenCompra.oc_id == oc_id).first()
    if not oc:
        raise HTTPException(status_code=404, detail="Orden de compra no encontrada")

    cxp_list = db.query(models.CuentaPorPagar).filter(
        models.CuentaPorPagar.oc_id == oc_id).all()
    cxp_con_pagos = [c for c in cxp_list if db.query(models.Pago).filter(
        models.Pago.cxp_id == c.id).count() > 0]
    if cxp_con_pagos:
        nums = ", ".join(c.numero for c in cxp_con_pagos)
        raise HTTPException(400, f"No se puede eliminar: existen CxP con pagos registrados ({nums})")

    for cxp in cxp_list:
        db.query(models.LineaCxP).filter(models.LineaCxP.cxp_id == cxp.id).delete()
        db.delete(cxp)

    audit.log(db, current_user, "ELIMINAR", "OC", oc_id,
              f"OC {oc_id} eliminada: {oc.proveedor or 'Sin proveedor'} — Total era: RD$ {oc.total_estimado or 0:,.2f}",
              {"proveedor": oc.proveedor, "estado": oc.estado,
               "total_estimado": float(oc.total_estimado or 0),
               "cxp_eliminadas": len(cxp_list)})

    db.query(models.CompromisoPresupuestario).filter(
        models.CompromisoPresupuestario.origen_tipo == "OC",
        models.CompromisoPresupuestario.origen_id == oc_id,
    ).delete()
    db.query(models.OrdenCompraLinea).filter(models.OrdenCompraLinea.oc_id == oc_id).delete()
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
    lineas: TList[DevolucionLinea] = []


@router.post("/{oc_id}/devolucion")
def devolver_oc(oc_id: str, data: DevolucionPayload, db: Session = Depends(get_db),
                current_user: models.Usuario = Depends(auth.require_supervisor)):
    """Return received items to supplier — adjusts inventory, generates NC, reduces CxP."""
    oc = db.query(models.OrdenCompra).filter(models.OrdenCompra.oc_id == oc_id).first()
    if not oc:
        raise HTTPException(404, "Orden de compra no encontrada")
    if oc.estado not in ("Parcial", "Recibida", "Cerrada"):
        raise HTTPException(400, f"Solo se puede devolver una OC Parcial/Recibida/Cerrada (estado: {oc.estado})")
    if not data.lineas:
        raise HTTPException(400, "Debe indicar al menos una línea a devolver")

    from routers.inventario import _recalc_avg_cost
    import logging as _logging
    _log = _logging.getLogger(__name__)

    try:
        total_devuelto = Decimal("0")
        lineas_devueltas = []

        for item in data.lineas:
            if item.cantidad_devuelta <= 0:
                continue
            linea = db.query(models.OrdenCompraLinea).filter(
                models.OrdenCompraLinea.id == item.linea_id,
                models.OrdenCompraLinea.oc_id == oc_id
            ).first()
            if not linea:
                raise HTTPException(400, f"Línea {item.linea_id} no encontrada en OC {oc_id}")

            recibida = float(linea.cantidad_recibida or 0)
            prev_devuelto = db.query(sqlfunc.coalesce(sqlfunc.sum(models.MovimientoInventario.cantidad), 0)).filter(
                models.MovimientoInventario.oc_referencia == oc_id,
                models.MovimientoInventario.tipo_doc == "DEV-GR",
                models.MovimientoInventario.producto_id == linea.producto_id,
            ).scalar() or 0
            disponible = recibida - float(prev_devuelto)
            if item.cantidad_devuelta > disponible:
                raise HTTPException(400,
                    f"Producto {linea.producto_id}: disponible para devolver={disponible}, solicitado={item.cantidad_devuelta}")

            monto_linea = Decimal(str(round(item.cantidad_devuelta * float(linea.precio_unitario), 4)))
            total_devuelto += monto_linea

            prod = db.query(models.Producto).filter(
                models.Producto.id_prod == linea.producto_id, models.Producto.activo == True
            ).first()
            if prod and prod.es_inventariable:
                nuevo_stock = max(0, (prod.stock_actual or 0) - item.cantidad_devuelta)
                num_doc = get_next("DEV-GR", db)
                mov = models.MovimientoInventario(
                    num_documento=num_doc,
                    producto_id=linea.producto_id,
                    tipo_doc="DEV-GR",
                    tipo="salida",
                    motivo=data.motivo,
                    cantidad=item.cantidad_devuelta,
                    costo_unitario=float(linea.precio_unitario),
                    stock_post=round(nuevo_stock, 4),
                    proveedor=oc.proveedor,
                    fecha=datetime.now(),
                    oc_referencia=oc_id,
                    usuario_id=current_user.id,
                    observacion=f"Devolución OC {oc_id} — {data.motivo}",
                )
                db.add(mov)
                prod.stock_actual = round(nuevo_stock, 4)

            linea.cantidad_recibida = max(0, recibida - item.cantidad_devuelta)
            lineas_devueltas.append({
                "linea_id": linea.id,
                "producto_id": linea.producto_id,
                "cantidad_devuelta": item.cantidad_devuelta,
                "monto": float(monto_linea),
            })

        if not lineas_devueltas:
            raise HTTPException(400, "No se procesaron líneas de devolución")

        oc.total_recibido = max(Decimal("0"), Decimal(str(oc.total_recibido or 0)) - total_devuelto)

        nc_numero = None
        cxp_ajustada = None
        prov = None
        if oc.proveedor_id:
            prov = db.query(models.Proveedor).get(oc.proveedor_id)
        if not prov and oc.proveedor:
            prov = db.query(models.Proveedor).filter(
                models.Proveedor.nombre == oc.proveedor, models.Proveedor.activo == True
            ).first()

        if prov and total_devuelto > 0:
            itbis_monto = round(total_devuelto * Decimal("0.18"), 2)
            nc_total = total_devuelto + itbis_monto
            nc_numero = get_next("NC", db)
            cxp = db.query(models.CuentaPorPagar).filter(
                models.CuentaPorPagar.oc_id == oc_id
            ).first()

            nc = models.NotaCredito(
                numero=nc_numero,
                tipo="proveedor",
                proveedor_id=prov.id,
                cxp_id=cxp.id if cxp else None,
                estado="activa",
                referencia_id=cxp.id if cxp else None,
                fecha=datetime.now().date(),
                motivo=data.motivo,
                subtotal=total_devuelto,
                itbis=itbis_monto,
                total=nc_total,
            )
            db.add(nc)
            db.flush()

            if cxp:
                cxp.saldo_pendiente = max(Decimal("0"), (cxp.saldo_pendiente or Decimal("0")) - nc_total)
                if cxp.saldo_pendiente <= 0:
                    cxp.estado = "pagada"
                elif cxp.saldo_pendiente < cxp.total:
                    cxp.estado = "parcial"
                cxp_ajustada = cxp.numero

            r_compra = _get_regla_cuentas(db, "compra", "factura_proveedor")
            if r_compra:
                dim = {"campo_id": oc.campo_id, "unidad_negocio_id": oc.unidad_negocio_id,
                       "departamento_id": oc.departamento_id}
                try:
                    _crear_asiento_auto(
                        db, datetime.now().date(), "DEV-GR", oc_id,
                        f"Devolución OC {oc_id} — {prov.nombre}",
                        [
                            {"cuenta_id": r_compra[1], "debe": total_devuelto, "haber": 0,
                             **dim, "descripcion_linea": f"Reverso CxP devolución OC {oc_id}"},
                            {"cuenta_id": r_compra[0], "debe": 0, "haber": total_devuelto,
                             **dim, "descripcion_linea": f"Salida inventario devolución OC {oc_id}"},
                        ],
                        current_user.nombre
                    )
                except Exception:
                    _log.exception("Error asiento devolución OC %s", oc_id)

        audit.log(db, current_user, "DEVOLUCION", "OC", oc_id,
                  f"Devolución {len(lineas_devueltas)} líneas — Total: RD$ {total_devuelto:,.2f}" +
                  (f" — NC {nc_numero}" if nc_numero else ""),
                  {"lineas": lineas_devueltas, "nc": nc_numero, "cxp_ajustada": cxp_ajustada})

        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise HTTPException(500, "Error al procesar la devolución")

    return {
        "ok": True,
        "lineas_devueltas": lineas_devueltas,
        "total_devuelto": float(total_devuelto),
        "nc_numero": nc_numero,
        "cxp_ajustada": cxp_ajustada,
    }


