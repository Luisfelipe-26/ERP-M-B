"""Importar el reporte de liquidaciones de la planta.

Una recepción de la planta trae fruta de varios campos y una factura agrupa varias
recepciones: por cada (recepción, campo) se crean cosecha a granel, despacho y liquidación,
y por cada número de factura una sola factura con sus liquidaciones.

Si existe `tests/data/liquidaciones_*.tsv` (un reporte real; no se sube al repositorio
porque trae datos comerciales), la misma prueba lo carga y compara contra su columna Importe.
"""
import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import HTTPException

import models
from conftest import periodo_abierto, saldo_cuenta
from routers.ventas import (FacturaVentaIn, ImportarIn, _nombre_calibre, anular_factura_venta,
                            anular_liquidacion, facturar_liquidaciones, importar_liquidaciones,
                            listar_liquidaciones)

COSTO_KG = 20
TASA = 62.5

# Mismo formato que el reporte de la planta (pegado desde Excel). 13.50 × 2.25 = 30.375 → 30.38
SINTETICO = """Fecha\tNumero Factura\tReferencia Interna\tCalibres\tCampo\tPrecio\tVolumen\t Importe
01/09/2026\t100\t5001\tAguacate Hass Calibre 10/12\t5\t2.25\t 13.50 \t 30.38
01/09/2026\t100\t5001\tAguacate Hass Calibre 14\t5\t2.25\t 1,035.10 \t 2,328.98
01/09/2026\t100\t5001\tAguacate Hass Industria\t5\t0.40\t 120.00 \t 48.00
01/09/2026\t100\t5001\tAguacate Hass Calibre 14\t17\t2.25\t 800.00 \t 1,800.00
03/09/2026\t100\t5002\tAguacate Hass Calibre 18\t17\t1.90\t 455.30 \t 865.07
08/09/2026\t101\t5003\tAguacate Hass Calibre 18\t5\t1.90\t 300.00 \t 570.00
08/09/2026\t101\t5003\tAguacate Hass Industria\t5\t0.40\t 40.25 \t 16.10
"""

REALES = sorted((Path(__file__).parent / "data").glob("liquidaciones_*.tsv"))


def _num(x):
    return float(x.strip().replace(",", ""))


def _leer(texto):
    filas = []
    for x in texto.splitlines()[1:]:
        c = x.split("\t")
        if len(c) < 8 or not c[0].strip():
            continue
        filas.append({"fecha": dt.datetime.strptime(c[0].strip(), "%d/%m/%Y").date(), "factura": c[1].strip(),
                      "referencia": c[2].strip(), "calibre": " ".join(c[3].split()), "campo": c[4].strip(),
                      "precio": _num(c[5]), "kg": _num(c[6]), "importe": _num(c[7])})
    return filas


def _esperado(filas):
    """Lo que dice el reporte: kg e importe por factura, kg por campo y recepciones."""
    fac, campos = {}, {}
    for f in filas:
        x = fac.setdefault(f["factura"], {"kg": Decimal(0), "total": Decimal(0)})
        x["kg"] += Decimal(str(f["kg"]))
        x["total"] += Decimal(str(f["importe"]))
        campos[f["campo"]] = campos.get(f["campo"], Decimal(0)) + Decimal(str(f["kg"]))
    return fac, campos, {(f["referencia"], f["campo"]) for f in filas}


def _cid(campo):
    return f"C{int(campo):02d}"


@pytest.fixture
def f(db):
    c = {}
    for cod, nom, nat, tipo in [
        ("1.1.02.01", "CxC clientes", "deudora", "activo"),
        ("1.1.03.03", "Cosecha terminada", "deudora", "activo"),
        ("1.1.03.08", "Fruta despachada por liquidar", "deudora", "activo"),
        ("4.1.01", "Venta de aguacate", "acreedora", "ingreso"),
        ("5.1.06", "Producción agrícola", "acreedora", "costo"),
        ("5.1.11", "Costo de venta aguacate", "deudora", "costo"),
        ("5.2.03", "Merma y faltantes", "deudora", "costo"),
    ]:
        c[cod] = models.CuentaContable(codigo=cod, nombre=nom, naturaleza=nat, tipo=tipo)
        db.add(c[cod])
    db.flush()
    for ev, con, debe, haber in [
        ("cosecha", "produccion", "1.1.03.03", "5.1.06"),
        ("venta", "factura_cliente", "1.1.02.01", "4.1.01"),
        ("venta", "despacho_por_liquidar", "1.1.03.08", "1.1.03.03"),
        ("inventario", "ajuste", "5.2.03", "1.1.03.03"),
    ]:
        db.add(models.ReglaContabilizacion(evento=ev, concepto=con, activo=True,
                                           cuenta_debe_id=c[debe].id, cuenta_haber_id=c[haber].id))
    db.add(models.Producto(id_prod="AGR", producto="Aguacate Hass a granel", unidad="kg",
                           costo_unitario=COSTO_KG, costo_promedio=0, stock_actual=0,
                           es_inventariable=True, activo=True,
                           cuenta_inventario_id=c["1.1.03.03"].id, cuenta_costo_id=c["5.1.11"].id))
    db.add(models.Cliente(id_cliente="PL1", nombre="Planta empacadora", activo=True, condicion_pago_dias=30))
    db.flush()
    c["granel"] = models.Calibre(nombre="Fruta de campo", orden=0, producto_id="AGR", es_granel=True)
    db.add(c["granel"])
    c["cliente"] = db.query(models.Cliente).one()
    periodo_abierto(db, dt.date(2026, 9, 1))
    db.commit()
    return c


def _preparar(db, f, filas, **kw):
    """Crea los campos del reporte y arma la importación."""
    for campo in {x["campo"] for x in filas}:
        if not db.query(models.Campo).filter_by(id_campo=_cid(campo)).first():
            db.add(models.Campo(id_campo=_cid(campo), nombre=f"Campo {campo}", area_ha=5, variedad="Hass", activo=True))
    db.commit()
    datos = dict(
        cliente_id=f["cliente"].id, moneda="USD", temporada="2026",
        campos={x["campo"]: _cid(x["campo"]) for x in filas}, calibres={},
        facturas={x["factura"]: {"ncf": f"E31{int(x['factura']):010d}", "tasa_cambio": TASA} for x in filas},
        filas=[{k: v for k, v in x.items() if k != "importe"} for x in filas])
    datos.update(kw)
    return ImportarIn(**datos)


@pytest.mark.parametrize("fuente", ["sintetico"] + [p.name for p in REALES])
def test_importa_el_reporte_y_cuadra_con_la_planta(db, user, f, fuente):
    texto = SINTETICO if fuente == "sintetico" else (Path(__file__).parent / "data" / fuente).read_text(encoding="utf-8")
    filas = _leer(texto)
    fac_esp, campos_esp, recepciones = _esperado(filas)
    payload = _preparar(db, f, filas)

    prueba = importar_liquidaciones(payload, dry_run=True, db=db, current_user=user)
    assert prueba["recepciones"] == prueba["liquidaciones"] == len(recepciones)
    assert db.query(models.DespachoFruta).count() == 0, "el modo prueba no deja nada"
    assert db.query(models.Calibre).count() == 1

    r = importar_liquidaciones(payload, dry_run=False, db=db, current_user=user)
    # Cada factura cuadra al centavo con la columna Importe del reporte
    for x in r["facturas"]:
        assert Decimal(str(x["total"])) == fac_esp[x["factura"]]["total"], x["factura"]
        assert Decimal(str(x["kg"])) == fac_esp[x["factura"]]["kg"], x["factura"]
    assert len(r["facturas"]) == len(fac_esp)
    assert r["cosechas"] == r["despachos"] == len(recepciones)
    assert set(r["calibres_creados"]) == {_nombre_calibre(x["calibre"]) for x in filas}

    cxcs = db.query(models.CuentaPorCobrar).all()
    assert len(cxcs) == len(fac_esp)
    total_usd = sum(v["total"] for v in fac_esp.values())
    assert -saldo_cuenta(db, f["4.1.01"]) == pytest.approx(float(total_usd) * TASA, abs=0.05)

    # Lo cosechado se despachó y se liquidó completo: nada queda en inventario ni por liquidar
    kg = float(sum(v["kg"] for v in fac_esp.values()))
    assert float(db.query(models.Producto).filter_by(id_prod="AGR").one().stock_actual) == pytest.approx(0, abs=0.001)
    assert saldo_cuenta(db, f["1.1.03.03"]) == pytest.approx(0, abs=0.05)
    assert saldo_cuenta(db, f["1.1.03.08"]) == pytest.approx(0, abs=0.05)
    assert saldo_cuenta(db, f["5.1.11"]) == pytest.approx(kg * COSTO_KG, abs=0.05)

    por_campo = {}
    for l in listar_liquidaciones(db=db, _=user):
        por_campo[l["campo_id"]] = por_campo.get(l["campo_id"], Decimal(0)) + Decimal(str(l["kg_liquidados"]))
    assert por_campo == {_cid(k): v for k, v in campos_esp.items()}

    with pytest.raises(HTTPException) as e:
        importar_liquidaciones(payload, dry_run=True, db=db, current_user=user)
    assert "ya está cargada" in e.value.detail


def test_el_redondeo_es_el_de_la_planta(db, user, f):
    filas = _leer(SINTETICO)[:1]                                   # 13.50 kg × 2.25 = 30.375
    r = importar_liquidaciones(_preparar(db, f, filas), dry_run=True, db=db, current_user=user)
    assert r["facturas"][0]["total"] == 30.38


def test_faltan_datos_se_avisan_todos_juntos(db, user, f):
    filas = _leer(SINTETICO)
    with pytest.raises(HTTPException) as e:
        importar_liquidaciones(_preparar(db, f, filas, campos={"5": "C05"},
                                         facturas={"100": {"ncf": "E310000000100"}}),
                               dry_run=True, db=db, current_user=user)
    detalle = e.value.detail
    assert "campo 17 del archivo" in detalle and "NCF de la factura 101" in detalle
    assert "tasa de cambio de la factura 100" in detalle


def test_una_factura_agrupa_liquidaciones_y_se_puede_rehacer(db, user, f):
    filas = _leer(SINTETICO)
    importar_liquidaciones(_preparar(db, f, filas), dry_run=False, db=db, current_user=user)
    cxc = db.query(models.CuentaPorCobrar).filter_by(ncf="E310000000100").one()
    liqs = db.query(models.LiquidacionVenta).filter_by(cxc_id=cxc.id).all()
    assert len(liqs) == 3                                          # 5001/campo 5, 5001/campo 17, 5002/campo 17

    with pytest.raises(HTTPException) as e:
        anular_liquidacion(liqs[0].id, motivo="Probar bloqueo", db=db, current_user=user)
    assert "anule primero la factura" in e.value.detail
    db.rollback()

    venta = -saldo_cuenta(db, f["4.1.01"])
    total = float(cxc.total)
    anular_factura_venta(cxc.id, motivo="NCF equivocado", db=db, current_user=user)
    assert -saldo_cuenta(db, f["4.1.01"]) == pytest.approx(venta - total * TASA, abs=0.01)
    assert saldo_cuenta(db, f["1.1.03.08"]) > 0, "sus despachos vuelven a estar por liquidar"
    assert all(l.cxc_id is None and l.estado == "activa" for l in liqs)

    nueva = facturar_liquidaciones(FacturaVentaIn(liquidacion_ids=[l.id for l in liqs], ncf="E310000000102",
                                                  tasa_cambio=TASA), db=db, current_user=user)
    assert nueva["total"] == total
    assert saldo_cuenta(db, f["1.1.03.08"]) == pytest.approx(0, abs=0.05)
    assert -saldo_cuenta(db, f["4.1.01"]) == pytest.approx(venta, abs=0.05)
