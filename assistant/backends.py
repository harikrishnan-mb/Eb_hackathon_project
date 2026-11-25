"""Select the state-store backend from the environment (CLAUDE.md §12, C10).

STATE_DATABASE_URL=postgresql://...  -> PostgreSQL (assistant/pgstore.py)
otherwise                             -> SQLite file (STATE_DB, default state/platform.db)
"""

from __future__ import annotations

import os
from pathlib import Path

from .store import AuditLog, SessionFacts, SqliteHitlStore, StateDB

ROOT = Path(__file__).resolve().parents[1]


def open_state():
    url = os.environ.get("STATE_DATABASE_URL", "")
    if url:
        from .pgstore import PgStateDB

        return PgStateDB(url)
    return StateDB(os.environ.get("STATE_DB", str(ROOT / "state" / "platform.db")))


def hitl_for(db):
    if type(db).__name__ == "PgStateDB":
        from .pgstore import PgHitlStore

        return PgHitlStore(db)
    return SqliteHitlStore(db)


def audit_for(db):
    if type(db).__name__ == "PgStateDB":
        from .pgstore import PgAuditLog

        return PgAuditLog(db)
    return AuditLog(db)


def facts_for(db):
    if type(db).__name__ == "PgStateDB":
        from .pgstore import PgSessionFacts

        return PgSessionFacts(db)
    return SessionFacts(db)


def ragas_for(target):
    """target: PgStateDB, or a path for the SQLite RAGAS store."""
    if type(target).__name__ == "PgStateDB":
        from .pgstore import PgRagasStore

        return PgRagasStore(target)
    from .ragas_auditor import RagasStore

    return RagasStore(target)
