"""
Órdenes de Compra (OC) — CRUD completo con líneas de producto.
H-16 FIX: Implementación del ciclo de compra vinculado a GR.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from database import get_db
import models, schemas, auth
from routers.sequences import get_next, peek_next
from routers.contabilidad import _crear_asiento_auto, _get_regla_cuentas, _verificar_presupuesto
from typing import List, Optional
from datetime import datetime
from decimal import Decimal
import audit

router = APIRouter(prefix="/api/ordenes-compra", tags=["ordenes-compra"])

ESTADOS_OC = ["Borrador", "Aprobada", "Parcial", "Recibida", "Cerrada", "Cancelada"]


@router.get("/preview/next-id")
def next_oc_id_preview(db: Session = Depends(get_db), _=Depends(auth.get_current_user)):
    return {"next_oc_id": peek_next("OC", db)}


@router.get("", response_model=List[schemas.OrdenCompraOut])
def list_ocs(
    estado: Optional[str] = None,
    proveedor: Optional[str] = None,
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
    return q.order_by(models.OrdenCompra.fecha.desc()).offset(skip).limit(limit).all()


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

        db.add(models.CompromisoPresupuestario(
            anio=fecha_oc.year if hasattr(fecha_oc, 'year') else datetime.now().year,
            mes=fecha_oc.month if hasattr(fecha_oc, 'month') else datetime.now().month,
            cuenta_id=r_compra[0],
            campo_id=oc.campo_id,
            unidad_negocio_id=oc.unidad_negocio_id,
            departamento_id=oc.departamento_id,
            monto=Decimal(str(round(total, 2))),
            origen_tipo="OC", origen_id=oc_id, estado="activo",
        ))

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

    db.query(models.CompromisoPresupuestario).filter(
        models.CompromisoPresupuestario.origen_tipo == "OC",
        models.CompromisoPresupuestario.origen_id == oc_id,
        models.CompromisoPresupuestario.estado == "activo",
    ).update({"estado": "cancelado"})

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
        db.query(models.CompromisoPresupuestario).filter(
            models.CompromisoPresupuestario.origen_tipo == "OC",
            models.CompromisoPresupuestario.origen_id == oc_id,
            models.CompromisoPresupuestario.estado == "activo",
        ).update({"estado": "cancelado"})
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

    if data.lineas:
        db.query(models.OrdenCompraLinea).filter(models.OrdenCompraLinea.oc_id == oc_id).delete()
        total = 0.0
        for linea in data.lineas:
            prod = db.query(models.Producto).filter(
                models.Producto.id_prod == linea.producto_id, models.Producto.activo == True
            ).first()
            if not prod:
                raise HTTPException(status_code=400, detail=f"Producto '{linea.producto_id}' no existe")
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
    return oc


@router.delete("/{oc_id}")
def delete_oc(oc_id: str, db: Session = Depends(get_db),
              current_user: models.Usuario = Depends(auth.require_admin)):
    oc = db.query(models.OrdenCompra).filter(models.OrdenCompra.oc_id == oc_id).first()
    if not oc:
        raise HTTPException(status_code=404, detail="Orden de compra no encontrada")

    audit.log(db, current_user, "ELIMINAR", "OC", oc_id,
              f"OC {oc_id} eliminada: {oc.proveedor or 'Sin proveedor'} — Total era: RD$ {oc.total_estimado or 0:,.2f}",
              {"proveedor": oc.proveedor, "estado": oc.estado,
               "total_estimado": float(oc.total_estimado or 0)})

    db.query(models.CompromisoPresupuestario).filter(
        models.CompromisoPresupuestario.origen_tipo == "OC",
        models.CompromisoPresupuestario.origen_id == oc_id,
    ).delete()
    db.query(models.OrdenCompraLinea).filter(models.OrdenCompraLinea.oc_id == oc_id).delete()
    db.delete(oc)
    db.commit()
    return {"ok": True}
