"""Integration tests against the Docker Postgres and Redis (CLAUDE.md §12, §10).

Skipped unless the services are configured:
  STATE_DATABASE_URL_TEST=postgresql://ua:...@127.0.0.1:5432/ua_test
  REDIS_URL_TEST=redis://:...@127.0.0.1:6379/15
Credentials come from docker/.env. Uses a dedicated test database and Redis DB 15.
"""

import os
import uuid

import pytest

from assistant.ragas_auditor import Scores

PG_URL = os.environ.get("STATE_DATABASE_URL_TEST", "")
REDIS_URL = os.environ.get("REDIS_URL_TEST", "")
RID = "3f2b6c1e-8a9d-4b7e-9c1a-2d4e6f8a0b1c"


@pytest.fixture()
def pg():
    if not PG_URL:
        pytest.skip("STATE_DATABASE_URL_TEST not set")
    from assistant.pgstore import PgStateDB

    db = PgStateDB(PG_URL)
    yield db
    db.close()


def _tenant():
    return f"tenant_{uuid.uuid4().hex[:12]}"  # unique per test, so tests do not see each other's rows


def test_pg_hitl_is_tenant_scoped(pg):
    from assistant.backends import hitl_for

    hitl = hitl_for(pg)
    a, b = _tenant(), _tenant()
    hitl.decide(a, RID, "APPROVE", reviewer_id="rev-1")
    assert hitl.is_approved(a, RID) is True
    assert hitl.is_approved(b, RID) is False
    hitl.decide(a, RID, "REJECT")
    assert hitl.is_approved(a, RID) is False


def test_pg_audit_chain_verifies_and_tamper_is_detected(pg):
    from assistant.backends import audit_for

    audit = audit_for(pg)
    t = _tenant()
    audit.append(t, "hitl_request", {"request_id": RID})
    audit.append(t, "hitl_decide", {"request_id": RID, "decision": "APPROVE"})
    assert audit.verify() is None
    assert [r[1] for r in audit.rows_for_tenant(t)] == ["hitl_request", "hitl_decide"]
    # Tamper with the stored payload, then the chain check must fail.
    with pg.connection() as conn:
        seq = conn.execute("SELECT seq FROM audit_log WHERE tenant_id = %s ORDER BY seq LIMIT 1", (t,)).fetchone()[0]
        conn.execute("UPDATE audit_log SET payload_json = '{\"tampered\": true}' WHERE seq = %s", (seq,))
    assert audit.verify() == seq
    with pg.connection() as conn:  # restore so later tests see an intact chain
        conn.execute("UPDATE audit_log SET payload_json = %s WHERE seq = %s",
                     ('{"request_id": "%s"}' % RID, seq))


def test_pg_session_facts_round_trip_and_isolation(pg):
    from assistant.backends import facts_for

    facts = facts_for(pg)
    t, other, sid = _tenant(), _tenant(), str(uuid.uuid4())
    facts.put(t, sid, "order_status", "shipped", "a" * 64)
    assert facts.get(t, sid, "order_status") == {"fact_value": "shipped", "source_chunk_hash": "a" * 64}
    assert facts.get(other, sid, "order_status") is None


def test_pg_ragas_scores_are_tenant_scoped(pg):
    from assistant.backends import ragas_for

    store = ragas_for(pg)
    t, other = _tenant(), _tenant()
    store.save(t, "r-1", Scores(1.0, 0.9, 0.8, []), "judge")
    assert [r[0] for r in store.rows(t)] == ["r-1"]
    assert store.rows(other) == []


@pytest.fixture()
def redis_cache():
    if not REDIS_URL:
        pytest.skip("REDIS_URL_TEST not set")
    from assistant.cachestore import RedisCache

    return RedisCache(REDIS_URL)


def test_redis_round_trip_and_ttl(redis_cache):
    key = f"test-{uuid.uuid4().hex}"
    redis_cache.set(key, {"answer": "30 days.", "citations": ["note_returns_policy-0"]})
    assert redis_cache.get(key) == {"answer": "30 days.", "citations": ["note_returns_policy-0"]}
    ttl = redis_cache._client.ttl("ua:cache:" + key)
    assert 255 <= ttl <= 345  # 300 s +/- 45 s jitter


def test_redis_fails_open_when_unreachable():
    from assistant.cachestore import RedisCache

    cache = RedisCache("redis://127.0.0.1:1/0")  # nothing listens here
    assert cache.get("anything") is None
    cache.set("anything", {"x": 1})  # must not raise
    assert cache.errors >= 2
