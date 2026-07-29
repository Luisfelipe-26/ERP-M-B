"""Background sync: Odoo Corvus → PostgreSQL proveedores (cada 10s, sin tokens)."""
import asyncio
import threading
import xmlrpc.client
from sqlalchemy.orm import Session
from database import SessionLocal
import models

import os as _os

ODOO_URL = _os.environ.get('ODOO_URL', 'https://corvus.marcoson.net')
ODOO_DB = _os.environ.get('ODOO_DB', 'corvus.marcoson.net')
ODOO_USER = _os.environ.get('ODOO_USER', 'finanzas@grupolb.com.do')
ODOO_PASS = _os.environ.get('ODOO_PASS', 'Mathias@0627')
SYNC_INTERVAL = int(_os.environ.get('ODOO_SYNC_INTERVAL', '10'))


def _sync_proveedores():
    """Sync suppliers from Odoo Corvus into local proveedores table."""
    try:
        common = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/common', allow_none=True)
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASS, {})
        if not uid:
            return

        odoo = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object', allow_none=True)

        # Get all active suppliers
        suppliers = odoo.execute_kw(ODOO_DB, uid, ODOO_PASS, 'res.partner', 'search_read',
            [[('supplier', '=', True), ('active', '=', True)]],
            {'fields': ['id', 'name', 'email', 'phone', 'mobile', 'vat'],
             'limit': 5000, 'order': 'name'})

        # Get only those with purchase activity
        pos = odoo.execute_kw(ODOO_DB, uid, ODOO_PASS, 'purchase.order', 'search_read',
            [[]], {'fields': ['partner_id'], 'limit': 5000})
        po_ids = set(p['partner_id'][0] for p in pos if p.get('partner_id'))

        supinfo = odoo.execute_kw(ODOO_DB, uid, ODOO_PASS, 'product.supplierinfo', 'search_read',
            [[]], {'fields': ['name'], 'limit': 5000})
        si_ids = set(s['name'][0] for s in supinfo if s.get('name'))

        active_ids = po_ids | si_ids
        active = [s for s in suppliers if s['id'] in active_ids]

        # Upsert into local DB
        db: Session = SessionLocal()
        try:
            added = 0
            updated = 0
            for s in active:
                existing = db.query(models.Proveedor).filter(
                    models.Proveedor.odoo_id == s['id']).first()

                nombre = s['name'].strip()
                rnc = s.get('vat') or None
                email = s.get('email') or None
                tel = s.get('phone') or s.get('mobile') or None

                if existing:
                    # Update if changed
                    changed = False
                    if existing.nombre != nombre:
                        existing.nombre = nombre; changed = True
                    if existing.rnc != rnc:
                        existing.rnc = rnc; changed = True
                    if existing.email != email:
                        existing.email = email; changed = True
                    if existing.telefono != tel:
                        existing.telefono = tel; changed = True
                    if not existing.activo:
                        existing.activo = True; changed = True
                    if changed:
                        updated += 1
                else:
                    # Check by name (might exist without odoo_id)
                    by_name = db.query(models.Proveedor).filter(
                        models.Proveedor.nombre == nombre).first()
                    if by_name:
                        by_name.odoo_id = s['id']
                        by_name.rnc = rnc or by_name.rnc
                        by_name.email = email or by_name.email
                        by_name.telefono = tel or by_name.telefono
                        updated += 1
                    else:
                        prov = models.Proveedor(
                            nombre=nombre, rnc=rnc, email=email,
                            telefono=tel, odoo_id=s['id'])
                        db.add(prov)
                        added += 1

            if added or updated:
                db.commit()
        finally:
            db.close()

    except Exception:
        pass  # Silent — no tokens, no noise


def _sync_loop():
    """Blocking loop that runs in a background thread."""
    import time
    while True:
        _sync_proveedores()
        time.sleep(SYNC_INTERVAL)


def start_sync():
    """Start the background sync thread (call once at app startup)."""
    t = threading.Thread(target=_sync_loop, daemon=True, name="odoo-sync")
    t.start()
