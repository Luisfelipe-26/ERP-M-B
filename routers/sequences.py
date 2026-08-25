"""Sequence utility — atomic formatted document/entity numbering."""
import re
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func
from database import get_db
from auth import get_current_user, require_admin
import models

router = APIRouter(prefix="/api", tags=["secuencias"])

SEQUENCE_CONFIG = {
    'GR':    ('GR-', 4),
    'GI':    ('GI-', 4),
    'AJ':    ('AJ-', 4),
    'OT':    ('',    0),   # OT usa enteros puros, no formateados
    'OC':    ('OC-', 4),
    'CAMPO': ('C',   2),
    'TRAB':  ('T-',  3),
    'ACT':   ('A-',  3),
    'PROD':  ('P-',  3),
    'MON':   ('MON-', 4),
    'SPR':   ('SPR-', 4),
    'TR':    ('TR-',  3),
    'AC':    ('AC-',  4),
    'CXP':   ('CXP-', 4),
    'PAG':   ('PAG-', 4),
    'CXC':   ('CXC-', 4),
    'COB':   ('COB-', 4),
    'RP':    ('RP-',  4),
}

SEQUENCE_TABLE_MAP = {
    'PROD': (models.Producto, 'id_prod'),
    'CAMPO': (models.Campo, 'id_campo'),
    'TRAB': (models.Trabajador, 'id_trab'),
}


def _fmt(tipo: str, num: int) -> str:
    prefix, padding = SEQUENCE_CONFIG[tipo]
    if padding == 0:
        return str(num)  # OT: plain integer
    return f"{prefix}{str(num).zfill(padding)}"


def _max_from_table(tipo: str, db: Session) -> int:
    """Scan the actual table to find the highest existing number for this sequence type."""
    if tipo not in SEQUENCE_TABLE_MAP:
        return 0
    model, col_name = SEQUENCE_TABLE_MAP[tipo]
    prefix = SEQUENCE_CONFIG[tipo][0]
    col = getattr(model, col_name)
    rows = db.query(col).all()
    max_num = 0
    for (val,) in rows:
        if val and val.startswith(prefix):
            digits = val[len(prefix):]
            if digits.isdigit():
                max_num = max(max_num, int(digits))
    return max_num


def _ensure_synced(tipo: str, db: Session, seq) -> None:
    """If the sequence is behind the actual data, catch it up."""
    max_existing = _max_from_table(tipo, db)
    if max_existing > seq.ultimo_numero:
        seq.ultimo_numero = max_existing
        db.flush()


def get_next(tipo: str, db: Session) -> str:
    """Atomically increment and return the next formatted number."""
    seq = db.query(models.Sequence).filter(models.Sequence.tipo == tipo).with_for_update().first()
    if not seq:
        seq = models.Sequence(tipo=tipo, ultimo_numero=0)
        db.add(seq)
        db.flush()
    _ensure_synced(tipo, db, seq)
    seq.ultimo_numero += 1
    db.flush()
    return _fmt(tipo, seq.ultimo_numero)


def peek_next(tipo: str, db: Session) -> str:
    """Return the next number without consuming it (for pre-fill suggestions)."""
    seq = db.query(models.Sequence).filter(models.Sequence.tipo == tipo).first()
    seq_num = seq.ultimo_numero if seq else 0
    max_existing = _max_from_table(tipo, db)
    num = max(seq_num, max_existing) + 1
    return _fmt(tipo, num)


@router.get("/secuencias")
def listar_secuencias(db: Session = Depends(get_db), user=Depends(require_admin)):
    seqs = db.query(models.Sequence).order_by(models.Sequence.tipo).all()
    cfg = SEQUENCE_CONFIG
    return [
        {
            "tipo": s.tipo,
            "prefijo": cfg[s.tipo][0] if s.tipo in cfg else None,
            "ultimo_numero": s.ultimo_numero,
            "ultimo_generado": _fmt(s.tipo, s.ultimo_numero) if s.tipo in cfg else str(s.ultimo_numero),
        }
        for s in seqs
    ]
