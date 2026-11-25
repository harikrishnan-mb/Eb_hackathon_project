"""MCP server: state-store over Streamable HTTP (CLAUDE.md §5; mcp_config.json "state-store").

Endpoint: http://localhost:8081/mcp (STATE_STORE_PORT overrides the port).
Tools: session_facts_put, session_facts_get, hitl_request, hitl_decide, hitl_status, audit_append.
Tier ceiling: TIER_3_MUTATE, harness-approved calls only [DD]. Every write is
validated against assistant.contracts models and every query filters on tenant_id.

Backed by SQLite (assistant/store.py), the local scope proposed in C10.
Reviewer identity is taken from the request body (reviewer_id). This is not an
authorization check. CLAUDE.md §6.8 leaves production identity open.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from assistant.contracts import HitlDecideInput, SessionFactsPutInput, Tier3Request  # noqa: E402
from assistant.backends import audit_for, facts_for, hitl_for, open_state  # noqa: E402

PORT = int(os.environ.get("STATE_STORE_PORT", "8081"))
mcp = FastMCP("state-store", host="127.0.0.1", port=PORT)

_TENANT = re.compile(r"^tenant_[a-z0-9_-]{1,56}$")
_db: StateDB | None = None


def _state() -> tuple[SessionFacts, SqliteHitlStore, AuditLog]:
    global _db
    if _db is None:
        _db = open_state()
    return facts_for(_db), hitl_for(_db), audit_for(_db)


def _invalid(exc: ValidationError) -> dict:
    return {"error": {"code": -32602, "name": "E_VALIDATION", "detail": exc.errors(include_input=False)}}


def session_facts_put(request_id: str, tenant_id: str, session_id: str, fact_key: str,
                      fact_value: str, source_chunk_hash: str) -> dict:
    """Store a verified fact for a session. Writes only verified facts (Gate 5)."""
    try:
        m = SessionFactsPutInput(request_id=request_id, tenant_id=tenant_id, session_id=session_id,
                                 fact_key=fact_key, fact_value=fact_value, source_chunk_hash=source_chunk_hash)
    except ValidationError as exc:
        return _invalid(exc)
    facts, _, _ = _state()
    facts.put(m.tenant_id, m.session_id, m.fact_key, m.fact_value, m.source_chunk_hash)
    return {"stored": True}


def session_facts_get(tenant_id: str, session_id: str, fact_key: str) -> dict:
    """Read one fact. Returns found=false when absent. Always scoped to tenant_id."""
    if not _TENANT.match(tenant_id):
        return {"error": {"code": -32602, "name": "E_VALIDATION", "detail": "tenant_id pattern"}}
    facts, _, _ = _state()
    row = facts.get(tenant_id, session_id, fact_key)
    return {"found": row is not None, **(row or {})}


def hitl_request(request_id: str, idempotency_key: str, tenant_id: str, action: str,
                 args: dict | None = None, risk_tier: str = "TIER_3_MUTATE") -> dict:
    """Register a Tier 3 request. It stays PENDING_APPROVAL until a reviewer decides."""
    try:
        m = Tier3Request(request_id=request_id, idempotency_key=idempotency_key, tenant_id=tenant_id,
                         action=action, args=args or {}, risk_tier=risk_tier)
    except ValidationError as exc:
        return _invalid(exc)
    _, _, audit = _state()
    audit.append(m.tenant_id, "hitl_request", {"request_id": m.request_id, "action": m.action})
    return {"status": "PENDING_APPROVAL", "request_id": m.request_id, "tenant_id": m.tenant_id}


def hitl_decide(request_id: str, tenant_id: str, reviewer_id: str, decision: str) -> dict:
    """Record a reviewer decision (APPROVE or REJECT) and audit it."""
    try:
        m = HitlDecideInput(request_id=request_id, tenant_id=tenant_id, reviewer_id=reviewer_id, decision=decision)
    except ValidationError as exc:
        return _invalid(exc)
    _, hitl, audit = _state()
    hitl.decide(m.tenant_id, m.request_id, m.decision, m.reviewer_id)
    audit.append(m.tenant_id, "hitl_decide", {"request_id": m.request_id, "decision": m.decision,
                                              "reviewer_id": m.reviewer_id})
    return {"request_id": m.request_id, "tenant_id": m.tenant_id, "decision": m.decision}


def hitl_status(tenant_id: str, request_id: str) -> dict:
    """True only if a reviewer approved this request (the harness Tier 3 gate reads this)."""
    if not _TENANT.match(tenant_id):
        return {"error": {"code": -32602, "name": "E_VALIDATION", "detail": "tenant_id pattern"}}
    _, hitl, _ = _state()
    return {"approved": hitl.is_approved(tenant_id, request_id)}


def audit_append(tenant_id: str, event: str, payload: dict | None = None) -> dict:
    """Append one event to the hash-chained audit log."""
    if not _TENANT.match(tenant_id):
        return {"error": {"code": -32602, "name": "E_VALIDATION", "detail": "tenant_id pattern"}}
    if not re.match(r"^[a-z_]{3,48}$", event):
        return {"error": {"code": -32602, "name": "E_VALIDATION", "detail": "event pattern"}}
    _, _, audit = _state()
    digest = audit.append(tenant_id, event, payload or {})
    return {"hash": digest}


for _fn in (session_facts_put, session_facts_get, hitl_request, hitl_decide, hitl_status, audit_append):
    mcp.tool(name=_fn.__name__)(_fn)

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
