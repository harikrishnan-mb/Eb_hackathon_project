import uuid

from assistant.errors import ErrorCode
from assistant.harness import ToolExecutionHarness


def _hybrid_args(**over):
    base = {"request_id": str(uuid.uuid4()), "tenant_id": "tenant_a", "query": "policy for SKU99"}
    base.update(over)
    return base


def _handlers():
    def retrieve(m):
        return {"abstain": False, "chunks": [], "stage_ms": {"bm25": 1.0}}

    def transition(m):
        return {"status": "applied", "action": m.action}

    return {"hybrid_retrieve": retrieve, "workflow_transition": transition}


def test_read_call_succeeds_and_reports_repair():
    h = ToolExecutionHarness(_handlers())
    r = h.call(tool="hybrid_retrieve", subagent="retrieval", raw=_hybrid_args())
    assert r.ok
    assert r.repairs and r.repairs[0].repaired.endswith("SKU-099")


def test_schema_error_is_validation_not_crash():
    h = ToolExecutionHarness(_handlers())
    r = h.call(tool="hybrid_retrieve", subagent="retrieval", raw=_hybrid_args(extra="x"))
    assert not r.ok and r.error.code == ErrorCode.VALIDATION


def test_retrieval_subagent_cannot_mutate():
    h = ToolExecutionHarness(_handlers())
    raw = {
        "request_id": str(uuid.uuid4()), "idempotency_key": str(uuid.uuid4()),
        "tenant_id": "tenant_a", "action": "close_order", "risk_tier": "TIER_3_MUTATE",
    }
    r = h.call(tool="workflow_transition", subagent="retrieval", raw=raw)
    assert not r.ok and r.error.code == ErrorCode.RBAC_DENIED


def test_tier3_blocked_until_hitl_approved():
    h = ToolExecutionHarness(_handlers())
    rid = str(uuid.uuid4())
    raw = {
        "request_id": rid, "idempotency_key": str(uuid.uuid4()),
        "tenant_id": "tenant_a", "action": "close_order", "risk_tier": "TIER_3_MUTATE",
    }
    r = h.call(tool="workflow_transition", subagent="execution", raw=raw)
    assert not r.ok and r.error.code == ErrorCode.HITL_PENDING

    h.hitl.decide("tenant_a", rid, "APPROVE")
    r = h.call(tool="workflow_transition", subagent="execution", raw=raw)
    assert r.ok and r.value["status"] == "applied"


def test_idempotent_replay_returns_stored_result_and_conflict_is_denied():
    h = ToolExecutionHarness(_handlers())
    rid = str(uuid.uuid4())
    key = str(uuid.uuid4())
    raw = {
        "request_id": rid, "idempotency_key": key,
        "tenant_id": "tenant_a", "action": "close_order", "risk_tier": "TIER_3_MUTATE",
    }
    h.hitl.decide("tenant_a", rid, "APPROVE")
    assert h.call(tool="workflow_transition", subagent="execution", raw=raw).ok
    replay = h.call(tool="workflow_transition", subagent="execution", raw=raw)
    assert replay.ok and replay.value["status"] == "applied"

    changed = dict(raw, action="reopen_order")
    conflict = h.call(tool="workflow_transition", subagent="execution", raw=changed)
    assert not conflict.ok and conflict.error.code == ErrorCode.IDEMPOTENT_REPLAY


def test_unknown_tool_rejected():
    h = ToolExecutionHarness(_handlers())
    r = h.call(tool="drop_table", subagent="execution", raw={})
    assert not r.ok and r.error.code == ErrorCode.INVALID_REQUEST


def test_handler_crash_becomes_degraded_not_exception():
    def boom(m):
        raise RuntimeError("db down")

    h = ToolExecutionHarness({"hybrid_retrieve": boom})
    r = h.call(tool="hybrid_retrieve", subagent="retrieval", raw=_hybrid_args())
    assert not r.ok and r.error.code == ErrorCode.DEGRADED
