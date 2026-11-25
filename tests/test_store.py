import sqlite3

from assistant.store import AuditLog, SqliteHitlStore, StateDB


def _db(tmp_path):
    return StateDB(tmp_path / "s.db")


def test_hitl_decisions_are_tenant_scoped(tmp_path):
    hitl = SqliteHitlStore(_db(tmp_path))
    rid = "3f2b6c1e-8a9d-4b7e-9c1a-2d4e6f8a0b1c"
    hitl.decide("tenant_a", rid, "APPROVE")
    assert hitl.is_approved("tenant_a", rid)
    assert not hitl.is_approved("tenant_b", rid)  # no cross-tenant approval


def test_reject_removes_approval(tmp_path):
    hitl = SqliteHitlStore(_db(tmp_path))
    rid = "3f2b6c1e-8a9d-4b7e-9c1a-2d4e6f8a0b1c"
    hitl.decide("tenant_a", rid, "APPROVE")
    hitl.decide("tenant_a", rid, "REJECT")
    assert not hitl.is_approved("tenant_a", rid)


def test_decisions_survive_reopen(tmp_path):
    rid = "3f2b6c1e-8a9d-4b7e-9c1a-2d4e6f8a0b1c"
    SqliteHitlStore(_db(tmp_path)).decide("tenant_a", rid, "APPROVE")
    assert SqliteHitlStore(_db(tmp_path)).is_approved("tenant_a", rid)


def test_audit_chain_verifies_and_detects_tampering(tmp_path):
    db = _db(tmp_path)
    log = AuditLog(db)
    log.append("tenant_a", "hitl_request", {"request_id": "r1"})
    log.append("tenant_b", "hitl_decide", {"request_id": "r2", "decision": "APPROVE"})
    log.append("tenant_a", "hitl_decide", {"request_id": "r1", "decision": "APPROVE"})
    assert log.verify() is None
    conn = sqlite3.connect(str(tmp_path / "s.db"))
    conn.execute("UPDATE audit_log SET payload_json = '{\"request_id\": \"r1\", \"decision\": \"REJECT\"}' WHERE seq = 1")
    conn.commit()
    assert AuditLog(StateDB(tmp_path / "s.db")).verify() == 1


def test_audit_rows_are_per_tenant(tmp_path):
    log = AuditLog(_db(tmp_path))
    log.append("tenant_a", "hitl_request", {"request_id": "r1"})
    log.append("tenant_b", "hitl_request", {"request_id": "r2"})
    rows = log.rows_for_tenant("tenant_a")
    assert len(rows) == 1 and "r1" in rows[0][2]
