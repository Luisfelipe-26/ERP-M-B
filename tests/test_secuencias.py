"""Numeración de documentos: cada tipo usado en el código debe estar registrado."""
import datetime as dt
import pathlib
import re

import models
import schemas
from conftest import ANIO
from routers.contabilidad import crear_nota_credito
from routers.sequences import SEQUENCE_CONFIG, get_next

RAIZ = pathlib.Path(__file__).resolve().parents[1]


def test_todo_tipo_de_secuencia_usado_esta_registrado():
    """NC, DEV y DEV-GR se usaban sin registrar: cada nota de crédito y cada
    devolución fallaba con KeyError en producción. Este test lo detecta al escribirlo."""
    faltan = {}
    for f in list((RAIZ / "routers").glob("*.py")) + [RAIZ / "main.py"]:
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            for k in re.findall(r"""(?:get_next|peek_next)\(\s*["']([^"']+)["']""", line):
                if k not in SEQUENCE_CONFIG:
                    faltan.setdefault(k, []).append(f"{f.name}:{n}")
    assert not faltan, f"tipos de secuencia sin registrar en SEQUENCE_CONFIG: {faltan}"


def test_un_tipo_desconocido_no_revienta(db):
    assert get_next("XYZ", db) == "XYZ-0001"
    assert get_next("XYZ", db) == "XYZ-0002"


def test_nota_de_credito_se_crea(db, user, proveedor):
    """Antes fallaba siempre: get_next('NC') lanzaba KeyError."""
    r = crear_nota_credito(schemas.NotaCreditoCreate(
        proveedor_id=proveedor.id, fecha=dt.date(ANIO, 3, 1), motivo="Descuento por volumen",
        subtotal=1_000, itbis=180), db=db, user=user)

    assert r["ok"] is True
    assert r["numero"] == "NC-0001"
    nc = db.query(models.NotaCredito).get(r["id"])
    assert float(nc.total) == 1_180
