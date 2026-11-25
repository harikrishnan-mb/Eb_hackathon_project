"""Observability and bulkheads (CLAUDE.md §4 bulkheads, §13 spans and metrics, Gate 9-11).

- Spans use the Gate 10 canonical names (hook.pre, cache.lookup, triage.classify,
  mcp.rag.bm25, ..., llm.generate). Without an exporter they are no-ops.
  OTEL_TRACES_EXPORTER=console prints them; OTEL_EXPORTER_OTLP_ENDPOINT is not used here
  because no collector is running.
- Prometheus metrics are exposed at GET /metrics by the API.
- Bulkheads cap concurrent calls per dependency (rag 64, llm 256, db 128 from Gate 9).
  When a pool is full the call fails fast with E_DEGRADED instead of queuing.
"""

from __future__ import annotations

import contextlib
import os
import threading
from typing import Iterator

from opentelemetry import trace
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

from .errors import ErrorCode, PlatformError

# ---- metrics ---------------------------------------------------------------
REQUESTS = Counter("ua_requests_total", "Agent requests by route and status", ["route", "status"])
REQUEST_SECONDS = Histogram(
    "ua_request_seconds", "End-to-end request latency in seconds",
    buckets=(0.005, 0.01, 0.025, 0.045, 0.1, 0.2, 0.25, 0.45, 0.59, 1, 2.5, 5, 10, 20, 60),
)
GUARD_BLOCKS = Counter("ua_guard_blocks_total", "Requests stopped by a guard", ["category"])
CACHE = Counter("ua_cache_total", "Response cache lookups", ["result"])
BULKHEAD_REJECTED = Counter("ua_bulkhead_rejected_total", "Calls refused by a full bulkhead", ["pool"])
UNSUPPORTED_CLAIMS = Counter("ua_unsupported_claims_total", "Answer sentences without support in the cited chunks")
RAGAS_DROPPED = Counter("ua_ragas_dropped_total", "RAGAS samples dropped because the queue was full")

# ---- tracing ---------------------------------------------------------------
_TRACER = trace.get_tracer("unreliable-assistant")


@contextlib.contextmanager
def span(name: str, **attrs) -> Iterator[None]:
    """Gate 10 span. Records the exception type on failure and re-raises."""
    with _TRACER.start_as_current_span(name) as s:
        for k, v in attrs.items():
            s.set_attribute(k, str(v))
        yield


def setup_tracing() -> None:
    """Install a console exporter when OTEL_TRACES_EXPORTER=console. Otherwise spans stay no-ops."""
    if os.environ.get("OTEL_TRACES_EXPORTER", "").lower() != "console":
        return
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

    provider = TracerProvider()
    provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)


def metrics_payload() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST


# ---- bulkheads -------------------------------------------------------------
class Bulkhead:
    """Fixed-size pool of concurrent slots. acquire() fails fast when all slots are busy."""

    def __init__(self, name: str, size: int) -> None:
        self.name = name
        self.size = size
        self._sem = threading.BoundedSemaphore(size)

    @contextlib.contextmanager
    def slot(self) -> Iterator[None]:
        if not self._sem.acquire(blocking=False):
            BULKHEAD_REJECTED.labels(pool=self.name).inc()
            raise PlatformError(ErrorCode.DEGRADED, f"{self.name} bulkhead full")
        try:
            yield
        finally:
            self._sem.release()


POOLS = {
    "rag": Bulkhead("rag", int(os.environ.get("RAG_POOL", "64"))),
    "llm": Bulkhead("llm", int(os.environ.get("LLM_POOL", "256"))),
    "db": Bulkhead("db", int(os.environ.get("DB_POOL", "128"))),
}
