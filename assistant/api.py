"""HTTP API (design §1.14, CLAUDE.md §14; endpoint from the Blueprint).

Endpoints:
  GET  /healthz
  POST /api/v1/agent/query     headers: X-Tenant-ID, X-User-ID (dev authenticator)
  POST /api/v1/hitl/decide     reviewer decision on a PENDING_APPROVAL request

Tenant comes from the authenticated principal. A body tenant_id that does not
match it is refused with E_TENANT_FORBIDDEN (design §6.8).

Error mapping: fail-closed guard failures return 503 with Retry-After. Other
PlatformErrors map to 4xx.

Security status: X-Tenant-ID / X-User-ID trust is for local use only. The
HITL decision endpoint requires the header X-Role: reviewer. That is a
development stub, not a real authorization system.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .auth import DevHeaderAuthenticator
from .contracts import HitlDecideInput
from .errors import ErrorCode, PlatformError
from .llm import from_env
from .orchestrator import Corpus, Orchestrator
from .backends import audit_for, hitl_for, open_state
from .cachestore import make_cache


def _auditor(llm):
    """Asynchronous RAGAS auditor (decision C21). Off unless RAGAS_ENABLED=1, because every
    answered retrieval response costs about 4-6 judge calls."""
    if os.environ.get("RAGAS_ENABLED", "").lower() not in ("1", "true", "yes"):
        return None
    from .backends import ragas_for
    from .pgstore import PgStateDB
    from .ragas_auditor import RagasAuditor

    url = os.environ.get("STATE_DATABASE_URL", "")
    target = PgStateDB(url) if url else str(ROOT / "state" / "ragas.db")
    return RagasAuditor(llm, ragas_for(target), judge_model=getattr(llm, "model", "unknown"))

ROOT = Path(__file__).resolve().parents[1]
STATUS_BY_CODE = {
    ErrorCode.INVALID_REQUEST: 400,
    ErrorCode.VALIDATION: 422,
    ErrorCode.TENANT_FORBIDDEN: 403,
    ErrorCode.RBAC_DENIED: 403,
    ErrorCode.TIMEOUT: 504,
    ErrorCode.BREAKER_OPEN: 409,
    ErrorCode.HITL_PENDING: 202,
    ErrorCode.IDEMPOTENT_REPLAY: 409,
    ErrorCode.DEGRADED: 503,
}


class QueryBody(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    tenant_id: str | None = None


@lru_cache(maxsize=1)
def get_orchestrator() -> Orchestrator:
    """Build the orchestrator. RETRIEVAL_MODE=bm25 (default) or hybrid.

    hybrid loads bge-large-en-v1.5 (dense) and bge-reranker-v2-m3 (cross-encoder)
    from MODELS_DIR. Loading is heavy (about 3.5 GB of weights), so it is opt-in.
    """
    corpus_dir = os.environ.get("CORPUS_DIR", str(ROOT / "knowledge_vault"))
    mode = os.environ.get("RETRIEVAL_MODE", "bm25").lower()
    if os.environ.get("MCP_MODE", "").lower() in ("1", "true", "yes"):
        # Retrieval, HITL state, and audit go through the MCP servers (CLAUDE.md §4, §5).
        # Start the state-store server first: python mcp_servers/state_store_server.py
        from .mcp_bridge import McpBridge

        bridge = McpBridge(
            rag_script=ROOT / "mcp_servers" / "rag_server.py",
            state_url=os.environ.get("STATE_STORE_URL", "http://localhost:8081/mcp"),
        )
        bridge.wait_ready("rag", timeout_s=float(os.environ.get("RAG_READY_TIMEOUT_S", "900")))
        llm = from_env()
        return Orchestrator(Corpus.from_dir(corpus_dir), llm=llm, mcp=bridge, auditor=_auditor(llm))

    state_db = open_state()  # Postgres when STATE_DATABASE_URL is set, else SQLite
    hitl = hitl_for(state_db)
    audit = audit_for(state_db)
    if mode == "bm25":
        llm = from_env()
        return Orchestrator(Corpus.from_dir(corpus_dir), llm=llm, hitl=hitl, audit=audit, cache=make_cache(),
                            auditor=_auditor(llm))
    if mode != "hybrid":
        raise RuntimeError(f"RETRIEVAL_MODE must be 'bm25' or 'hybrid', got {mode!r}")
    models = os.environ.get("MODELS_DIR")
    if not models:
        raise RuntimeError("RETRIEVAL_MODE=hybrid requires MODELS_DIR")
    from .retrieval.dense import BGEEncoder, DenseIndex
    from .retrieval.rerank import CrossEncoder

    encoder = BGEEncoder(Path(models) / "bge-large-en-v1.5")
    corpus = Corpus.from_dir(corpus_dir, tokenizer=encoder.tokenizer)
    dense = DenseIndex(encoder, corpus.chunks)
    reranker = CrossEncoder(Path(models) / "bge-reranker-v2-m3")
    llm = from_env()
    return Orchestrator(corpus, llm=llm, dense=dense, reranker=reranker, hitl=hitl, audit=audit,
                        cache=make_cache(), auditor=_auditor(llm))


@lru_cache(maxsize=1)
def get_authenticator() -> DevHeaderAuthenticator:
    return DevHeaderAuthenticator()


app = FastAPI(title="Unreliable Assistant API", version="0.1.0")


@app.on_event("startup")
def _warm_orchestrator() -> None:
    """Build the orchestrator (and in MCP mode wait for the RAG server) before serving requests."""
    from . import obs
    obs.setup_tracing()
    get_orchestrator()


@app.get("/metrics")
def metrics():
    """Prometheus exposition (CLAUDE.md §13)."""
    from fastapi.responses import Response
    from . import obs

    body, ctype = obs.metrics_payload()
    return Response(content=body, media_type=ctype)


@app.exception_handler(PlatformError)
async def platform_error_handler(_: Request, exc: PlatformError) -> JSONResponse:
    status = STATUS_BY_CODE.get(exc.code, 400)
    headers = {"Retry-After": "5"} if exc.code == ErrorCode.DEGRADED else None
    return JSONResponse(status_code=status, content={"error": exc.to_jsonrpc()}, headers=headers)


@app.get("/")
def root() -> dict:
    return {
        "service": "Unreliable Assistant API",
        "version": app.version,
        "docs": "/docs",
        "endpoints": {
            "health": "GET /healthz",
            "query": "POST /api/v1/agent/query (headers: X-Tenant-ID, X-User-ID)",
            "hitl_decide": "POST /api/v1/hitl/decide (header: X-Role: reviewer)",
        },
    }


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/v1/agent/query")
def query(body: QueryBody, request: Request) -> dict:
    principal = get_authenticator().authenticate(dict(request.headers))
    if body.tenant_id is not None and body.tenant_id != principal.tenant_id:
        raise PlatformError(ErrorCode.TENANT_FORBIDDEN, "body tenant_id does not match credentials")
    return get_orchestrator().answer(principal, body.query)


class HitlBody(HitlDecideInput):
    pass


@app.post("/api/v1/hitl/decide")
def hitl_decide(body: HitlBody, x_role: str | None = Header(default=None)) -> dict:
    if x_role != "reviewer":
        raise HTTPException(status_code=403, detail="reviewer role required")
    orch = get_orchestrator()
    orch.harness.hitl.decide(body.tenant_id, body.request_id, body.decision, body.reviewer_id)
    # In MCP mode the state-store server audits hitl_decide itself; do not append twice.
    if orch.audit is not None and not orch.uses_mcp:
        orch.audit.append(body.tenant_id, "hitl_decide",
                          {"request_id": body.request_id, "decision": body.decision})
    return {"request_id": body.request_id, "tenant_id": body.tenant_id, "decision": body.decision}
