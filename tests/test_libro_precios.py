"""Libro de precios por cliente y calibre, con vigencias e historial."""
import datetime as dt

import pytest
from fastapi import HTTPException

import models
from conftest import ANIO
from routers.cosecha import (ListaPreciosIn, PrecioLineaIn, PrecioUpdate, anular_precio,
                             list_precios, matriz_precios, precio_vigente, registrar_lista_precios,
                             update_precio)

D = lambda m, d: dt.date(ANIO, m, d)


@pytest.fixture
def base(db):
    c18 = models.Calibre(nombre="Cal 18", orden=1)
    c22 = models.Calibre(nombre="Cal 22", orden=2)
    mission = models.Cliente(id_cliente="CL1", nombre="Mission Produce", activo=True)
    local = models.Cliente(id_cliente="CL2", nombre="Mercado Local", activo=True)
    db.add_all([c18, c22, mission, local])
    db.commit()
    return {"c18": c18, "c22": c22, "mission": mission, "local": local}


def _lista(db, user, cliente=None, desde=D(9, 1), hasta=None, moneda="DOP", **precios):
    """precios: c18=95.0, c22=80.0"""
    return registrar_lista_precios(ListaPreciosIn(
        cliente_id=cliente.id if cliente else None, moneda=moneda, fecha_desde=desde, fecha_hasta=hasta,
        lineas=[PrecioLineaIn(calibre_id=precios["_cal"][k].id, precio=v)
                for k, v in precios.items() if k != "_cal"]),
        db=db, current_user=user)


def test_registra_la_lista_del_cliente_y_rige_desde_su_fecha(db, user, base):
    r = _lista(db, user, base["mission"], _cal=base, c18=95.0, c22=80.0)

    assert r["creados"] == 2 and r["cerrados"] == 0
    p = precio_vigente(db, base["c18"].id, D(9, 10), base["mission"].id, "DOP")
    assert float(p.precio) == 95 and p.cliente_id == base["mission"].id
    assert precio_vigente(db, base["c18"].id, D(8, 31), base["mission"].id, "DOP") is None, \
        "antes de su fecha de vigencia no rige"


def test_una_lista_nueva_cierra_la_anterior_y_conserva_el_historial(db, user, base):
    _lista(db, user, base["mission"], desde=D(9, 1), _cal=base, c18=95.0)
    r = _lista(db, user, base["mission"], desde=D(9, 15), _cal=base, c18=102.5)

    assert r["cerrados"] == 1
    hist = list_precios(cliente_id=base["mission"].id, calibre_id=base["c18"].id, db=db, _=user)
    assert [(h["precio"], h["fecha_desde"], h["fecha_hasta"]) for h in hist] == [
        (102.5, D(9, 15), None), (95.0, D(9, 1), D(9, 14))]
    assert float(precio_vigente(db, base["c18"].id, D(9, 10), base["mission"].id, "DOP").precio) == 95
    assert float(precio_vigente(db, base["c18"].id, D(9, 20), base["mission"].id, "DOP").precio) == 102.5


def test_no_permite_una_lista_que_choque_con_una_posterior(db, user, base):
    _lista(db, user, base["mission"], desde=D(9, 15), _cal=base, c18=102.5)
    with pytest.raises(HTTPException) as e:
        _lista(db, user, base["mission"], desde=D(9, 15), _cal=base, c18=99.0)
    assert e.value.status_code == 400 and "15/09" in e.value.detail


def test_la_lista_entra_entera_o_no_entra(db, user, base):
    """Si un calibre choca, no se registra ninguno ni se cierra nada."""
    _lista(db, user, base["mission"], desde=D(9, 1), _cal=base, c18=95.0)
    _lista(db, user, base["mission"], desde=D(9, 20), _cal=base, c22=70.0)

    with pytest.raises(HTTPException):
        _lista(db, user, base["mission"], desde=D(9, 10), _cal=base, c18=100.0, c22=75.0)
    db.rollback()

    c18 = list_precios(cliente_id=base["mission"].id, calibre_id=base["c18"].id, db=db, _=user)
    assert len(c18) == 1 and c18[0]["fecha_hasta"] is None, "el precio del 18 no debe haberse cerrado"


def test_sin_precio_propio_aplica_el_base(db, user, base):
    _lista(db, user, None, _cal=base, c18=90.0, c22=75.0)
    _lista(db, user, base["mission"], _cal=base, c18=95.0)

    m = matriz_precios(fecha=D(9, 10), moneda="DOP", db=db, _=user)
    filas = {f["cliente_nombre"]: f["precios"] for f in m["filas"]}
    assert filas["Precio base"][str(base["c22"].id)]["precio"] == 75
    assert filas["Mission Produce"][str(base["c18"].id)] == {
        **filas["Mission Produce"][str(base["c18"].id)], "precio": 95.0, "origen": "propio"}
    assert filas["Mission Produce"][str(base["c22"].id)]["origen"] == "base"
    assert filas["Mission Produce"][str(base["c22"].id)]["precio"] == 75
    assert "Mercado Local" not in filas, "sin precios propios no aparece como fila"


def test_las_monedas_llevan_vigencias_separadas(db, user, base):
    _lista(db, user, base["mission"], desde=D(9, 1), _cal=base, c18=95.0)
    r = _lista(db, user, base["mission"], desde=D(9, 5), moneda="USD", _cal=base, c18=1.60)

    assert r["cerrados"] == 0, "una lista en USD no cierra la de DOP"
    assert float(precio_vigente(db, base["c18"].id, D(9, 10), base["mission"].id, "DOP").precio) == 95
    assert float(precio_vigente(db, base["c18"].id, D(9, 10), base["mission"].id, "USD").precio) == 1.60


def test_anular_la_ultima_lista_devuelve_la_vigencia_a_la_anterior(db, user, base):
    _lista(db, user, base["mission"], desde=D(9, 1), _cal=base, c18=95.0)
    nueva = _lista(db, user, base["mission"], desde=D(9, 15), _cal=base, c18=102.5)["precios"][0]

    r = anular_precio(nueva["id"], db=db, current_user=user)

    assert r["reabierto"] is not None
    vig = precio_vigente(db, base["c18"].id, D(9, 20), base["mission"].id, "DOP")
    assert float(vig.precio) == 95 and vig.fecha_hasta is None


def test_editar_no_puede_solapar_la_vigencia_siguiente(db, user, base):
    primero = _lista(db, user, base["mission"], desde=D(9, 1), hasta=D(9, 14), _cal=base, c18=95.0)["precios"][0]
    _lista(db, user, base["mission"], desde=D(9, 15), _cal=base, c18=102.5)

    with pytest.raises(HTTPException) as e:
        update_precio(primero["id"], PrecioUpdate(fecha_hasta=D(9, 30)), db=db, current_user=user)
    assert "15/09" in e.value.detail

    r = update_precio(primero["id"], PrecioUpdate(precio=96.0), db=db, current_user=user)
    assert r["precio"] == 96 and r["fecha_hasta"] == D(9, 14), "corregir el precio no toca la vigencia"


@pytest.mark.parametrize("kw,msg", [
    ({"c18": 0.0}, "mayor a 0"),
    ({"c18": 95.0, "moneda": "EUR"}, "Moneda"),
    ({"c18": 95.0, "hasta": dt.date(ANIO, 8, 1)}, "anterior"),
])
def test_validaciones(db, user, base, kw, msg):
    with pytest.raises(HTTPException) as e:
        _lista(db, user, base["mission"], _cal=base, **kw)
    assert e.value.status_code == 400 and msg in e.value.detail
