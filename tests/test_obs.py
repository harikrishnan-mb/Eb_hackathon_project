import threading

import pytest

from assistant import obs
from assistant.errors import ErrorCode, PlatformError


def test_bulkhead_admits_up_to_size_then_fails_fast():
    bh = obs.Bulkhead("test", 2)
    with bh.slot():
        with bh.slot():
            with pytest.raises(PlatformError) as exc:
                with bh.slot():
                    pass
            assert exc.value.code == ErrorCode.DEGRADED


def test_bulkhead_releases_slot_after_exception():
    bh = obs.Bulkhead("test2", 1)
    with pytest.raises(ValueError):
        with bh.slot():
            raise ValueError("boom")
    with bh.slot():  # slot was released, so this must not fail
        pass


def test_bulkhead_is_thread_safe_under_contention():
    bh = obs.Bulkhead("test3", 4)
    refused = []

    def work():
        try:
            with bh.slot():
                threading.Event().wait(0.05)
        except PlatformError:
            refused.append(1)

    threads = [threading.Thread(target=work) for _ in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(refused) >= 1  # more callers than slots: some must be refused fast


def test_gate9_pool_sizes_default():
    assert obs.POOLS["rag"].size == 64 and obs.POOLS["llm"].size == 256 and obs.POOLS["db"].size == 128


def test_metrics_payload_contains_named_series():
    obs.REQUESTS.labels(route="HYBRID_RAG", status="answered").inc()
    body, ctype = obs.metrics_payload()
    assert b"ua_requests_total" in body and b"ua_request_seconds" in body
    assert "text/plain" in ctype or "openmetrics" in ctype
