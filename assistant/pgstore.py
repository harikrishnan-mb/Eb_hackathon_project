"""PostgreSQL backend for the state store and RAGAS scores (CLAUDE.md §12, §2 rule 4).

Same methods as the SQLite classes in assistant/store.py and assistant/ragas_auditor.py.
Every query filters on tenant_id. Each operation borrows its own pooled connection, so
concurrent API threads never share one connection. The audit chain is written under a
transaction-scoped advisory lock so concurrent writers cannot fork it.

Select with STATE_DATABASE_URL=postgresql://user:pass@host:port/db (factories in assistant/store.py).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from psycopg_pool import ConnectionPool

GENESIS = "0" * 64
_AUDIT_LOCK_KEY = 0x55415F4155444954  # fixed advisory-lock key for the audit chain

SCHEMA = """
CREATE TABLE IF NOT EXISTS hitl_decisions (
    tenant_id   TEXT NOT NULL,
    request_id  TEXT NOT NULL,
    decision    TEXT NOT NULL,
    decided_at  TIMESTAMPTZ NOT NULL,
    reviewer_id TEXT NOT NULL,
    PRIMARY KEY (tenant_id, request_id)
);
CREATE TABLE IF NOT EXISTS audit_log (
    seq          BIGSERIAL PRIMARY KEY,
    tenant_id    TEXT NOT NULL,
    event        TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    prev_hash    TEXT NOT NULL,
    hash         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_log_tenant ON audit_log (tenant_id, seq);
CREATE TABLE IF NOT EXISTS session_facts (
    tenant_id          TEXT NOT NULL,
    session_id         TEXT NOT NULL,
    fact_key           TEXT NOT NULL,
    fact_value         TEXT NOT NULL,
    source_chunk_hash  TEXT NOT NULL,
    PRIMARY KEY (tenant_id, session_id, fact_key)
);
CREATE TABLE IF NOT EXISTS ragas_scores (
    seq                BIGSERIAL PRIMARY KEY,
    tenant_id          TEXT NOT NULL,
    request_id         TEXT NOT NULL,
    faithfulness       DOUBLE PRECISION,
    answer_relevancy   DOUBLE PRECISION,
    context_precision  DOUBLE PRECISION,
    notes              TEXT NOT NULL,
    judge_model        TEXT NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS ragas_scores_tenant ON ragas_scores (tenant_id, seq);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PgStateDB:
    """Connection pool. `connection()` yields a connection that commits on success."""

    def __init__(self, url: str, max_size: int = 8) -> None:
        self.url = url
        self.pool = ConnectionPool(url, min_size=1, max_size=max_size, kwargs={"connect_timeout": 5}, open=True)
        with self.pool.connection() as conn:
            conn.execute(SCHEMA)

    def connection(self):
        return self.pool.connection()

    def close(self) -> None:
        self.pool.close()


class PgHitlStore:
    """Same interface as store.SqliteHitlStore."""

    def __init__(self, db: PgStateDB) -> None:
        self.db = db

    def decide(self, tenant_id: str, request_id: str, decision: str, reviewer_id: str = "reviewer") -> None:
        with self.db.connection() as conn:
            if decision == "APPROVE":
                conn.execute(
                    "INSERT INTO hitl_decisions (tenant_id, request_id, decision, decided_at, reviewer_id) "
                    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (tenant_id, request_id) DO UPDATE SET "
                    "decision = EXCLUDED.decision, decided_at = EXCLUDED.decided_at, "
                    "reviewer_id = EXCLUDED.reviewer_id",
                    (tenant_id, request_id, decision, _now(), reviewer_id),
                )
            else:
                conn.execute(
                    "DELETE FROM hitl_decisions WHERE tenant_id = %s AND request_id = %s",
                    (tenant_id, request_id),
                )

    def is_approved(self, tenant_id: str, request_id: str) -> bool:
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM hitl_decisions WHERE tenant_id = %s AND request_id = %s AND decision = 'APPROVE'",
                (tenant_id, request_id),
            ).fetchone()
        return row is not None


class PgAuditLog:
    """Same interface as store.AuditLog. The chain is global; reads filter by tenant."""

    def __init__(self, db: PgStateDB) -> None:
        self.db = db

    def append(self, tenant_id: str, event: str, payload: dict) -> str:
        body = json.dumps(payload, sort_keys=True, default=str)
        with self.db.connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_AUDIT_LOCK_KEY,))
            row = conn.execute("SELECT hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
            prev_hash = row[0] if row else GENESIS
            digest = _chain_hash(prev_hash, tenant_id, event, body)
            conn.execute(
                "INSERT INTO audit_log (tenant_id, event, payload_json, prev_hash, hash) "
                "VALUES (%s, %s, %s, %s, %s)",
                (tenant_id, event, body, prev_hash, digest),
            )
        return digest

    def verify(self) -> int | None:
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT seq, tenant_id, event, payload_json, prev_hash, hash FROM audit_log ORDER BY seq"
            ).fetchall()
        expected_prev = GENESIS
        for seq, tenant_id, event, body, prev_hash, digest in rows:
            if prev_hash != expected_prev or _chain_hash(prev_hash, tenant_id, event, body) != digest:
                return seq
            expected_prev = digest
        return None

    def rows_for_tenant(self, tenant_id: str) -> list[tuple]:
        with self.db.connection() as conn:
            return conn.execute(
                "SELECT seq, event, payload_json, hash FROM audit_log WHERE tenant_id = %s ORDER BY seq",
                (tenant_id,),
            ).fetchall()


class PgSessionFacts:
    """Same interface as store.SessionFacts."""

    def __init__(self, db: PgStateDB) -> None:
        self.db = db

    def put(self, tenant_id: str, session_id: str, fact_key: str, fact_value: str, source_chunk_hash: str) -> None:
        with self.db.connection() as conn:
            conn.execute(
                "INSERT INTO session_facts (tenant_id, session_id, fact_key, fact_value, source_chunk_hash) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (tenant_id, session_id, fact_key) DO UPDATE SET "
                "fact_value = EXCLUDED.fact_value, source_chunk_hash = EXCLUDED.source_chunk_hash",
                (tenant_id, session_id, fact_key, fact_value, source_chunk_hash),
            )

    def get(self, tenant_id: str, session_id: str, fact_key: str) -> dict | None:
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT fact_value, source_chunk_hash FROM session_facts "
                "WHERE tenant_id = %s AND session_id = %s AND fact_key = %s",
                (tenant_id, session_id, fact_key),
            ).fetchone()
        return None if row is None else {"fact_value": row[0], "source_chunk_hash": row[1]}


class PgRagasStore:
    """Same interface as ragas_auditor.RagasStore (save and rows)."""

    def __init__(self, db: PgStateDB) -> None:
        self.db = db

    def save(self, tenant_id: str, request_id: str, scores: Any, judge_model: str) -> None:
        with self.db.connection() as conn:
            conn.execute(
                "INSERT INTO ragas_scores (tenant_id, request_id, faithfulness, answer_relevancy, "
                "context_precision, notes, judge_model, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (tenant_id, request_id, scores.faithfulness, scores.answer_relevancy,
                 scores.context_precision, json.dumps(scores.notes), judge_model, _now()),
            )

    def rows(self, tenant_id: str) -> list[tuple]:
        with self.db.connection() as conn:
            return conn.execute(
                "SELECT request_id, faithfulness, answer_relevancy, context_precision FROM ragas_scores "
                "WHERE tenant_id = %s ORDER BY seq",
                (tenant_id,),
            ).fetchall()


def _chain_hash(prev_hash: str, tenant_id: str, event: str, body: str) -> str:
    return hashlib.sha256(f"{prev_hash}|{tenant_id}|{event}|{body}".encode("utf-8")).hexdigest()
