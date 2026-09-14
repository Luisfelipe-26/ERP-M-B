"""Ciclo compromiso -> devengado: cómo una OC reserva presupuesto y cómo la factura lo consume."""
import datetime as dt
from decimal import Decimal

import models
from conftest import ANIO, presupuestar
from routers.compras import _entradas_presupuestarias_oc
from routers.contabilidad import (_devengar_cxp_contra_compromisos,
                                  _reversar_devengado_cxp)


def _oc_dos_lineas(db, proveedor, cuenta_a, cuenta_b):
    """OC con dos líneas que van a cuentas y departamentos distintos."""
    oc = models.OrdenCompra(oc_id="OC-001", fecha=dt.datetime(ANIO, 1, 10),
                            proveedor=proveedor.nombre, proveedor_id=proveedor.id,
                            estado="Borrador", total_estimado=80_000)
    db.add(oc)
    db.flush()
    l1 = models.OrdenCompraLinea(oc_id="OC-001", producto_id="P1", cantidad=10,
                                 precio_unitario=5_000, subtotal=50_000,
                                 impuesto="itbis_18", cuenta_contable_id=cuenta_a.id,
                                 departamento_id=7)
    l2 = models.OrdenCompraLinea(oc_id="OC-001", producto_id="P2", cantidad=10,
                                 precio_unitario=3_000, subtotal=30_000,
                                 impuesto="itbis_18", cuenta_contable_id=cuenta_b.id,
                                 departamento_id=9)
    db.add_all([l1, l2])
    db.commit()
    return oc, l1, l2


def _comprometer(db, entradas):
    """Replica lo que hace aprobar_oc: el compromiso y su movimiento en el ledger."""
    comps = []
    for e in entradas:
        c = models.CompromisoPresupuestario(
            anio=ANIO, mes=1, cuenta_id=e["cuenta_id"],
            departamento_id=e["departamento_id"], monto=e["monto"],
            monto_ejecutado=Decimal("0"), oc_linea_id=e["oc_linea_id"],
            origen_tipo="OC", origen_id="OC-001", estado="activo")
        db.add(c)
        db.add(models.MovimientoPresupuestario(
            fecha=dt.date(ANIO, 1, 10), tipo="COMPROMISO", anio=ANIO, mes=1,
            cuenta_id=e["cuenta_id"], departamento_id=e["departamento_id"],
            monto=e["monto"], origen_tipo="OC", origen_id="OC-001"))
        comps.append(c)
    db.commit()
    return comps


def _facturar(db, proveedor, numero, oc_linea, subtotal, itbis, mes=1):
    cxp = models.CuentaPorPagar(numero=numero, proveedor_id=proveedor.id, oc_id="OC-001",
                                fecha_factura=dt.date(ANIO, mes, 20), subtotal=subtotal,
                                itbis=itbis, total=subtotal + itbis,
                                saldo_pendiente=subtotal + itbis, estado="pendiente")
    db.add(cxp)
    db.flush()
    db.add(models.LineaCxP(cxp_id=cxp.id, producto_id=oc_linea.producto_id,
                           oc_linea_id=oc_linea.id, cantidad=oc_linea.cantidad,
                           precio_unitario=oc_linea.precio_unitario,
                           impuesto="itbis_18", monto_itbis=itbis, subtotal=subtotal))
    db.commit()
    return cxp


def test_cada_linea_de_oc_genera_su_propia_linea_presupuestaria(db, proveedor, cuenta):
    """Antes toda la OC se imputaba a una cuenta genérica y al centro de costo del encabezado."""
    otra = models.CuentaContable(codigo="6.1.02", nombre="Combustible",
                                 naturaleza="deudora", tipo="gasto")
    db.add(otra)
    db.commit()
    oc, _, _ = _oc_dos_lineas(db, proveedor, cuenta, otra)

    entradas = _entradas_presupuestarias_oc(db, oc, proveedor, cuenta.id)

    assert len(entradas) == 2
    assert {e["cuenta_id"] for e in entradas} == {cuenta.id, otra.id}
    assert {e["departamento_id"] for e in entradas} == {7, 9}
    assert {e["monto"] for e in entradas} == {Decimal("50000.00"), Decimal("30000.00")}


def test_un_informal_compromete_solo_el_subtotal(db, proveedor, cuenta):
    """El informal no cobra ITBIS, así que la OC no reserva presupuesto por él."""
    oc, _, _ = _oc_dos_lineas(db, proveedor, cuenta, cuenta)
    informal = models.Proveedor(nombre="Vivero", tipo_contribuyente="informal")
    db.add(informal)
    db.commit()

    entradas = _entradas_presupuestarias_oc(db, oc, informal, cuenta.id)

    assert entradas[0]["monto"] == Decimal("50000.00")


def test_factura_parcial_deja_el_resto_comprometido(db, config, proveedor, cuenta, user):
    """Con recepciones parciales cada factura toma su parte; el resto sigue reservado."""
    otra = models.CuentaContable(codigo="6.1.02", nombre="Combustible",
                                 naturaleza="deudora", tipo="gasto")
    db.add(otra)
    db.commit()
    oc, l1, l2 = _oc_dos_lineas(db, proveedor, cuenta, otra)
    comp1, comp2 = _comprometer(db, _entradas_presupuestarias_oc(db, oc, proveedor, cuenta.id))

    cxp1 = _facturar(db, proveedor, "CXP-001", l1, 50_000, 9_000)
    devengado = _devengar_cxp_contra_compromisos(db, cxp1, user)
    db.commit()

    assert devengado == Decimal("50000.00")
    assert comp1.estado == "ejecutado"
    assert comp2.estado == "activo"
    assert Decimal(str(comp2.monto_ejecutado or 0)) == 0


def test_segunda_factura_cierra_el_compromiso_restante(db, config, proveedor, cuenta, user):
    otra = models.CuentaContable(codigo="6.1.02", nombre="Combustible",
                                 naturaleza="deudora", tipo="gasto")
    db.add(otra)
    db.commit()
    oc, l1, l2 = _oc_dos_lineas(db, proveedor, cuenta, otra)
    _, comp2 = _comprometer(db, _entradas_presupuestarias_oc(db, oc, proveedor, cuenta.id))

    _devengar_cxp_contra_compromisos(db, _facturar(db, proveedor, "CXP-001", l1, 50_000, 9_000), user)
    db.commit()
    _devengar_cxp_contra_compromisos(db, _facturar(db, proveedor, "CXP-002", l2, 30_000, 5_400, mes=2), user)
    db.commit()

    assert comp2.estado == "ejecutado"

    movs = db.query(models.MovimientoPresupuestario).all()
    neto = sum(float(m.monto) for m in movs if m.tipo in ("COMPROMISO", "LIBERACION"))
    devengado = sum(float(m.monto) for m in movs if m.tipo == "DEVENGADO")
    assert abs(neto) < 0.01, "el compromiso debe quedar neteado"
    assert devengado == 80_000


def test_la_nota_de_credito_reversa_la_cuenta_que_devengo(db, config, proveedor, cuenta, user):
    """Reversar contra una cuenta genérica dejaría un centro de costo sobregirado."""
    otra = models.CuentaContable(codigo="6.1.02", nombre="Combustible",
                                 naturaleza="deudora", tipo="gasto")
    db.add(otra)
    db.commit()
    oc, l1, l2 = _oc_dos_lineas(db, proveedor, cuenta, otra)
    _comprometer(db, _entradas_presupuestarias_oc(db, oc, proveedor, cuenta.id))

    cxp1 = _facturar(db, proveedor, "CXP-001", l1, 50_000, 9_000)
    _devengar_cxp_contra_compromisos(db, cxp1, user)
    _devengar_cxp_contra_compromisos(db, _facturar(db, proveedor, "CXP-002", l2, 30_000, 5_400, mes=2), user)
    db.commit()

    reversado = _reversar_devengado_cxp(db, cxp1, Decimal("10000"), dt.date(ANIO, 2, 10),
                                        origen_tipo="NC", origen_id="NC-001",
                                        notas="devolución", user=user)
    db.commit()

    def devengado_de(cta):
        return sum(float(m.monto) for m in db.query(models.MovimientoPresupuestario)
                   .filter_by(cuenta_id=cta.id, tipo="DEVENGADO").all())

    assert reversado == Decimal("10000.00")
    assert devengado_de(cuenta) == 40_000
    assert devengado_de(otra) == 30_000, "no debe tocar la cuenta que no facturó"


def test_no_reversa_una_factura_que_nunca_se_devengo(db, proveedor, user):
    cxp = models.CuentaPorPagar(numero="CXP-999", proveedor_id=proveedor.id,
                                fecha_factura=dt.date(ANIO, 1, 5), subtotal=1_000,
                                itbis=180, total=1_180, saldo_pendiente=1_180)
    db.add(cxp)
    db.commit()

    reversado = _reversar_devengado_cxp(db, cxp, Decimal("500"), dt.date(ANIO, 1, 10),
                                        origen_tipo="NC", origen_id="NC-9",
                                        notas="x", user=user)

    assert reversado == Decimal("0")
