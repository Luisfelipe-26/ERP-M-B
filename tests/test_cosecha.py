"""Registro de cosecha: kg por calibre, entrada a inventario, carencia y anulación."""
import calendar
import datetime as dt

import pytest
from fastapi import HTTPException

import models
import schemas
from conftest import ANIO, usuario
from routers.cosecha import (CalibreIn, CosechaIn, CosechaLineaIn, anular_cosecha, create_calibre,
                             create_cosecha, resumen_cosecha, verificar_carencia)
from routers.inventario import goods_issue

FECHA = dt.date(ANIO, 9, 10)


def _periodo(d):
    fin = dt.date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])
    return models.PeriodoContable(anio=d.year, mes=d.month, nombre=f"{d:%m-%Y}", estado="abierto",
                                  fecha_inicio=dt.date(d.year, d.month, 1), fecha_fin=fin)


@pytest.fixture
def finca(db, user):
    ctas = {}
    for cod, nom, nat in [("1.1.04", "Inventario fruta", "deudora"), ("4.2.01", "Producción agrícola", "acreedora"),
                          ("5.3.01", "Costo de ventas fruta", "deudora"), ("5.9.99", "Salidas varias", "deudora")]:
        c = models.CuentaContable(codigo=cod, nombre=nom, naturaleza=nat, tipo="x")
        db.add(c)
        ctas[cod] = c
    db.flush()
    db.add_all([
        models.ReglaContabilizacion(evento="cosecha", concepto="produccion", activo=True,
                                    cuenta_debe_id=ctas["1.1.04"].id, cuenta_haber_id=ctas["4.2.01"].id),
        models.ReglaContabilizacion(evento="inventario", concepto="salida", activo=True,
                                    cuenta_debe_id=ctas["5.9.99"].id, cuenta_haber_id=ctas["1.1.04"].id),
        _periodo(FECHA),
        models.Campo(id_campo="C01", nombre="Lote Norte", area_ha=5, variedad="Hass", activo=True),
        models.Campo(id_campo="C02", nombre="Lote Sur", area_ha=4, variedad="Hass", activo=True),
    ])
    if (dt.date.today().year, dt.date.today().month) != (FECHA.year, FECHA.month):
        db.add(_periodo(dt.date.today()))
    for pid, nom, costo in [("F18", "Hass calibre 18", 40), ("F22", "Hass calibre 22", 30)]:
        db.add(models.Producto(id_prod=pid, producto=nom, unidad="kg", costo_unitario=costo, costo_promedio=0,
                               stock_actual=0, es_inventariable=True, activo=True,
                               cuenta_inventario_id=ctas["1.1.04"].id, cuenta_costo_id=ctas["5.3.01"].id))
    db.commit()
    cals = {
        "18": create_calibre(CalibreIn(nombre="Cal 18", orden=1, producto_id="F18"), db=db, _=user),
        "22": create_calibre(CalibreIn(nombre="Cal 22", orden=2, producto_id="F22"), db=db, _=user),
        "rech": create_calibre(CalibreIn(nombre="Rechazo", orden=9), db=db, _=user),
    }
    return {"ctas": ctas, "cal": cals}


def _cosechar(db, user, finca, kg18=600, kg22=400, rech=50, campo="C01", fecha=FECHA, **extra):
    c = finca["cal"]
    return create_cosecha(CosechaIn(
        fecha=fecha, campo_id=campo,
        lineas=[CosechaLineaIn(calibre_id=c["18"].id, kg=kg18), CosechaLineaIn(calibre_id=c["22"].id, kg=kg22),
                CosechaLineaIn(calibre_id=c["rech"].id, kg=rech)], **extra),
        db=db, current_user=user)


def _prod(db, pid):
    return db.query(models.Producto).filter_by(id_prod=pid).one()


def test_registra_kg_por_calibre_y_los_entra_a_inventario(db, user, finca):
    r = _cosechar(db, user, finca)

    assert r["total_kg"] == 1_050
    assert r["temporada"] == str(ANIO), "temporada por defecto = año de la fecha"
    assert {l["calibre"]: l["kg"] for l in r["lineas"]} == {"Cal 18": 600, "Cal 22": 400, "Rechazo": 50}

    assert float(_prod(db, "F18").stock_actual) == 600
    assert float(_prod(db, "F22").stock_actual) == 400
    movs = db.query(models.MovimientoInventario).filter_by(tipo_doc="COS").all()
    assert len(movs) == 2, "el rechazo no tiene producto: se registra pero no entra al stock"
    m18 = next(m for m in movs if m.producto_id == "F18")
    assert m18.tipo == "entrada" and float(m18.costo_unitario) == 40 and float(m18.stock_post) == 600
    assert m18.lote == f"C01/{FECHA:%Y%m%d}"


def test_entra_al_costo_estandar_con_asiento_balanceado(db, user, finca):
    r = _cosechar(db, user, finca)

    cos = db.query(models.Cosecha).get(r["id"])
    asiento = db.query(models.AsientoContable).get(cos.asiento_id)
    assert float(asiento.total_debe) == float(asiento.total_haber) == 36_000   # 600x40 + 400x30
    haber = {l.cuenta_id: float(l.haber or 0) for l in asiento.lineas if l.haber}
    assert haber[finca["ctas"]["4.2.01"].id] == 36_000
    assert float(_prod(db, "F18").costo_promedio) == 40


def test_la_carencia_vigente_bloquea_la_cosecha(db, user, finca):
    db.add(models.SprayLog(spray_code="APL-7", campo_id="C01", application_date=dt.datetime(ANIO, 9, 1),
                           phi_days=14, earliest_harvest=dt.datetime(ANIO, 9, 15),
                           products_json='[{"nombre": "Abamectina"}]'))
    db.commit()

    chk = verificar_carencia("C01", FECHA, db=db, _=user)
    assert chk["permitido"] is False
    assert chk["aplicaciones"][0]["productos"] == "Abamectina"
    assert chk["aplicaciones"][0]["dias_restantes"] == 5

    with pytest.raises(HTTPException) as e:
        _cosechar(db, user, finca)
    assert e.value.status_code == 400 and e.value.detail["requiere_override"] is True
    db.rollback()
    assert float(_prod(db, "F18").stock_actual) == 0, "un bloqueo no debe tocar el stock"


def test_la_carencia_no_bloquea_otro_campo_ni_el_dia_permitido(db, user, finca):
    db.add(models.SprayLog(spray_code="APL-8", campo_id="C01", application_date=dt.datetime(ANIO, 8, 27),
                           phi_days=14, earliest_harvest=dt.datetime(ANIO, 9, 10)))
    db.add(models.SprayLog(spray_code="APL-9", campo_id="C01", application_date=dt.datetime(ANIO, 9, 20),
                           phi_days=14, earliest_harvest=dt.datetime(ANIO, 10, 4)))
    db.commit()

    assert verificar_carencia("C01", FECHA, db=db, _=user)["permitido"] is True, \
        "vence ese mismo día, y la otra aplicación es posterior a la cosecha"
    assert verificar_carencia("C02", FECHA, db=db, _=user)["permitido"] is True


def test_forzar_la_carencia_exige_admin_y_justificacion(db, user, finca):
    db.add(models.SprayLog(spray_code="APL-7", campo_id="C01", application_date=dt.datetime(ANIO, 9, 1),
                           earliest_harvest=dt.datetime(ANIO, 9, 15)))
    db.commit()

    with pytest.raises(HTTPException) as e:
        _cosechar(db, usuario(db, "operador"), finca, forzar_carencia=True,
                  justificacion_carencia="Error de captura en la aplicación")
    assert e.value.status_code == 403
    db.rollback()

    with pytest.raises(HTTPException) as e:
        _cosechar(db, user, finca, forzar_carencia=True, justificacion_carencia="porque sí")
    assert e.value.status_code == 400
    db.rollback()

    r = _cosechar(db, user, finca, forzar_carencia=True,
                  justificacion_carencia="La aplicación APL-7 se registró en el campo equivocado")
    assert r["carencia_forzada"] is True and "equivocado" in r["justificacion_carencia"]


def test_validaciones_de_entrada(db, user, finca):
    c = finca["cal"]
    for lineas, msg in [
        ([CosechaLineaIn(calibre_id=c["18"].id, kg=0)], "al menos un calibre"),
        ([CosechaLineaIn(calibre_id=c["18"].id, kg=5), CosechaLineaIn(calibre_id=c["18"].id, kg=5)], "dos veces"),
        ([CosechaLineaIn(calibre_id=999, kg=5)], "inexistente"),
    ]:
        with pytest.raises(HTTPException) as e:
            create_cosecha(CosechaIn(fecha=FECHA, campo_id="C01", lineas=lineas), db=db, current_user=user)
        assert e.value.status_code == 400 and msg in e.value.detail
        db.rollback()


def test_la_ot_debe_ser_del_mismo_campo(db, user, finca):
    db.add(models.Actividad(id_act="A1", actividad="Cosecha"))
    db.add(models.OrdenTrabajo(ot_id=77, campo_id="C02", actividad_id="A1", estado="Abierta"))
    db.commit()
    with pytest.raises(HTTPException) as e:
        _cosechar(db, user, finca, ot_id=77)
    assert "C02" in e.value.detail


def test_resumen_por_campo_y_calibre_con_rendimiento(db, user, finca):
    _cosechar(db, user, finca)
    _cosechar(db, user, finca, kg18=200, kg22=100, rech=0, campo="C02")

    r = resumen_cosecha(temporada=str(ANIO), fecha_desde=None, fecha_hasta=None, db=db, _=user)

    assert r["total_kg"] == 1_350
    c01 = next(c for c in r["campos"] if c["campo_id"] == "C01")
    assert c01["total_kg"] == 1_050 and c01["kg_por_ha"] == 210          # 1.050 kg / 5 ha
    assert [c["nombre"] for c in r["calibres"]] == ["Cal 18", "Cal 22", "Rechazo"], "ordenados por 'orden'"
    assert next(c for c in r["calibres"] if c["nombre"] == "Cal 18")["kg"] == 800


def test_anular_devuelve_stock_y_reversa_el_asiento(db, user, finca):
    r = _cosechar(db, user, finca)

    anular_cosecha(r["id"], motivo="Registro duplicado", db=db, current_user=user)

    assert float(_prod(db, "F18").stock_actual) == 0
    assert db.query(models.Cosecha).get(r["id"]).estado == "anulada"
    asientos = db.query(models.AsientoContable).filter(models.AsientoContable.origen == "COS").all()
    neto = sum(float(l.debe or 0) - float(l.haber or 0) for a in asientos for l in a.lineas
               if l.cuenta_id == finca["ctas"]["1.1.04"].id)
    assert neto == pytest.approx(0), "el reverso deja el inventario de fruta en cero"
    assert resumen_cosecha(temporada=str(ANIO), fecha_desde=None, fecha_hasta=None, db=db, _=user)["total_kg"] == 0


def test_no_anula_si_la_fruta_ya_salio(db, user, finca):
    r = _cosechar(db, user, finca)
    goods_issue(schemas.GICreate(producto_id="F18", cantidad=500, motivo="Venta"), db=db, current_user=user)

    with pytest.raises(HTTPException) as e:
        anular_cosecha(r["id"], motivo="Registro duplicado", db=db, current_user=user)
    assert "ya salió" in e.value.detail


def test_la_venta_va_a_costo_de_ventas_del_producto(db, user, finca):
    """Con regla genérica de salidas configurada, una venta igual debe ir a costo de ventas."""
    _cosechar(db, user, finca)

    goods_issue(schemas.GICreate(producto_id="F18", cantidad=100, motivo="Venta"), db=db, current_user=user)

    asiento = db.query(models.AsientoContable).filter_by(origen="GI").one()
    debe = {l.cuenta_id: float(l.debe or 0) for l in asiento.lineas if l.debe}
    assert debe == {finca["ctas"]["5.3.01"].id: 4_000}                   # 100 kg x 40
