"""Local persistence: HITL decisions and a hash-chained audit log (CLAUDE.md §12, §2 rule 4).

SQLite is used for local, test, and workbench runs. C10 proposes Postgres for
production; that is not built yet. Every query filters on tenant_id.

Audit chain: each row stores prev_hash and hash = SHA-256(prev_hash + canonical payload).
verify() recomputes the whole chain and reports the first broken sequence number.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

GENESIS = "0" * 64


class StateDB:
    def __init__(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS hitl_decisions (
                tenant_id   TEXT NOT NULL,
                request_id  TEXT NOT NULL,
                decision    TEXT NOT NULL,
                decided_at  TEXT NOT NULL,
                PRIMARY KEY (tenant_id, request_id)
            );
            CREATE TABLE IF NOT EXISTS session_facts (
                tenant_id          TEXT NOT NULL,
                session_id         TEXT NOT NULL,
                fact_key           TEXT NOT NULL,
                fact_value         TEXT NOT NULL,
                source_chunk_hash  TEXT NOT NULL,
                PRIMARY KEY (tenant_id, session_id, fact_key)
            );
            CREATE TABLE IF NOT EXISTS audit_log (
                seq          INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id    TEXT NOT NULL,
                event        TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                prev_hash    TEXT NOT NULL,
                hash         TEXT NOT NULL
            );
            """
        )
        self._conn.commit()


class SqliteHitlStore:
    """Same interface as harness.HitlStore, backed by StateDB."""

    def __init__(self, db: StateDB) -> None:
        self.db = db

    def decide(self, tenant_id: str, request_id: str, decision: str, reviewer_id: str = "reviewer") -> None:
        with self.db._lock:
            if decision == "APPROVE":
                self.db._conn.execute(
                    "INSERT OR REPLACE INTO hitl_decisions (tenant_id, request_id, decision, decided_at) "
                    "VALUES (?, ?, ?, ?)",
                    (tenant_id, request_id, decision, _now()),
                )
            else:
                self.db._conn.execute(
                    "DELETE FROM hitl_decisions WHERE tenant_id = ? AND request_id = ?",
                    (tenant_id, request_id),
                )
            self.db._conn.commit()

    def is_approved(self, tenant_id: str, request_id: str) -> bool:
        with self.db._lock:
            row = self.db._conn.execute(
                "SELECT 1 FROM hitl_decisions WHERE tenant_id = ? AND request_id = ? AND decision = 'APPROVE'",
                (tenant_id, request_id),
            ).fetchone()
        return row is not None


class SessionFacts:
    """Verified facts per tenant and session (CLAUDE.md §8, §6.3)."""

    def __init__(self, db: StateDB) -> None:
        self.db = db

    def put(self, tenant_id: str, session_id: str, fact_key: str, fact_value: str, source_chunk_hash: str) -> None:
        with self.db._lock:
            self.db._conn.execute(
                "INSERT OR REPLACE INTO session_facts (tenant_id, session_id, fact_key, fact_value, source_chunk_hash) "
                "VALUES (?, ?, ?, ?, ?)",
                (tenant_id, session_id, fact_key, fact_value, source_chunk_hash),
            )
            self.db._conn.commit()

    def get(self, tenant_id: str, session_id: str, fact_key: str) -> dict | None:
        with self.db._lock:
            row = self.db._conn.execute(
                "SELECT fact_value, source_chunk_hash FROM session_facts "
                "WHERE tenant_id = ? AND session_id = ? AND fact_key = ?",
                (tenant_id, session_id, fact_key),
            ).fetchone()
        return None if row is None else {"fact_value": row[0], "source_chunk_hash": row[1]}


class AuditLog:
    def __init__(self, db: StateDB) -> None:
        self.db = db

    def append(self, tenant_id: str, event: str, payload: dict) -> str:
        body = json.dumps(payload, sort_keys=True, default=str)
        with self.db._lock:
            prev = self.db._conn.execute(
                "SELECT hash FROM audit_log ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            prev_hash = prev[0] if prev else GENESIS
            digest = _chain_hash(prev_hash, tenant_id, event, body)
            self.db._conn.execute(
                "INSERT INTO audit_log (tenant_id, event, payload_json, prev_hash, hash) VALUES (?, ?, ?, ?, ?)",
                (tenant_id, event, body, prev_hash, digest),
            )
            self.db._conn.commit()
        return digest

    def verify(self) -> int | None:
        """Return None if the whole chain is intact, else the first broken seq."""
        with self.db._lock:
            rows = self.db._conn.execute(
                "SELECT seq, tenant_id, event, payload_json, prev_hash, hash FROM audit_log ORDER BY seq"
            ).fetchall()
        expected_prev = GENESIS
        for seq, tenant_id, event, body, prev_hash, digest in rows:
            if prev_hash != expected_prev or _chain_hash(prev_hash, tenant_id, event, body) != digest:
                return seq
            expected_prev = digest
        return None

    def rows_for_tenant(self, tenant_id: str) -> list[tuple]:
        with self.db._lock:
            return self.db._conn.execute(
                "SELECT seq, event, payload_json, hash FROM audit_log WHERE tenant_id = ? ORDER BY seq",
                (tenant_id,),
            ).fetchall()


def _chain_hash(prev_hash: str, tenant_id: str, event: str, body: str) -> str:
    return hashlib.sha256(f"{prev_hash}|{tenant_id}|{event}|{body}".encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
