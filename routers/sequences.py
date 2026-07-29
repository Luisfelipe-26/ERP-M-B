"""Sequence utility — atomic formatted document/entity numbering."""
from sqlalchemy.orm import Session
import models

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
}


def _fmt(tipo: str, num: int) -> str:
    prefix, padding = SEQUENCE_CONFIG[tipo]
    if padding == 0:
        return str(num)  # OT: plain integer
    return f"{prefix}{str(num).zfill(padding)}"


def get_next(tipo: str, db: Session) -> str:
    """Atomically increment and return the next formatted number."""
    seq = db.query(models.Sequence).filter(models.Sequence.tipo == tipo).with_for_update().first()
    if not seq:
        seq = models.Sequence(tipo=tipo, ultimo_numero=0)
        db.add(seq)
    seq.ultimo_numero += 1
    db.flush()
    return _fmt(tipo, seq.ultimo_numero)


def peek_next(tipo: str, db: Session) -> str:
    """Return the next number without consuming it (for pre-fill suggestions)."""
    seq = db.query(models.Sequence).filter(models.Sequence.tipo == tipo).first()
    num = (seq.ultimo_numero if seq else 0) + 1
    return _fmt(tipo, num)
