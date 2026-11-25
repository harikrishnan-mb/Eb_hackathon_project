"""Request pipeline (design §1.3, §1.9; CLAUDE.md §4).

Order for one query:
  1. Pre-execution guards: PII redaction and injection check (fail closed).
  2. Per-user cache lookup (exact key, tenant-scoped).
  3. Triage, then the safety guard on routing.
  4. Route:
     REJECT_OOS       → fixed refusal, no LLM.
     DIRECT_ANSWER    → LLM answer, no retrieval.
     TRANSACTION_EXEC → Tier 3 request is registered and returns E_HITL_PENDING.
     HYBRID_RAG       → bounded ReAct loop: retrieve through the harness, then
                        generate from retrieved chunks, or abstain when nothing is found.

Retrieval here is BM25 only. Dense and reranker stages are not yet wired in
(see reports/retrieval). The RAGAS auditor is not yet wired in.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from . import guard, obs, verifier
from .context import compress_observation
from .auth import Principal
from .cache import cache_key
from .errors import ErrorCode, PlatformError
from .harness import ToolExecutionHarness
from .llm import LLMClient, MockLLM
from .loop import ReActLoop, State
from .repair import auto_repair
from .retrieval.bm25 import BM25Index
from .retrieval.dense import Chunk, DenseIndex, chunk_document
from .retrieval.pipeline import HybridRetriever
from .triage import classify, route_safely

if TYPE_CHECKING:
    from .retrieval.rerank import CrossEncoder

# Per-call timeout for the rag MCP server. CLAUDE.md §5 says 150 ms; the CPU reranker
# needs about 15 s, so this is a local-run setting (deviation C25, approved by the user).
MCP_RAG_TIMEOUT_S = float(os.environ.get("MCP_RAG_TIMEOUT_S", "60"))

ABSTAIN_MESSAGE = "I could not find this in the approved knowledge base, so I cannot answer it."
REJECT_MESSAGE = "This request is outside the scope of this assistant."
HITL_MESSAGE = "This action needs human approval. It has been queued for review."


@dataclass
class Corpus:
    chunks: list[Chunk]            # one chunk per note unless a tokenizer splits long notes

    texts: dict[str, str] = field(init=False)     # chunk_id -> text
    chunk_doc: dict[str, str] = field(init=False)  # chunk_id -> note id
    index: BM25Index = field(init=False)

    def __post_init__(self) -> None:
        self.texts = {c.chunk_id: c.text for c in self.chunks}
        self.chunk_doc = {c.chunk_id: c.doc_id for c in self.chunks}
        self.index = BM25Index(self.texts)

    @classmethod
    def from_dir(cls, path: str | Path, tokenizer: Any | None = None) -> "Corpus":
        chunks: list[Chunk] = []
        for p in sorted(Path(path).glob("*.md")):
            body = p.read_text(encoding="utf-8").split("---", 2)[-1]
            if tokenizer is not None:
                chunks.extend(chunk_document(p.stem, body, tokenizer))
            else:
                chunks.append(Chunk(chunk_id=f"{p.stem}-0", doc_id=p.stem, text=body.strip()))
        return cls(chunks)


class Orchestrator:
    def __init__(
        self,
        corpus: Corpus,
        llm: LLMClient | None = None,
        harness: ToolExecutionHarness | None = None,
        dense: DenseIndex | None = None,
        reranker: "CrossEncoder | None" = None,
        loop_budget_ms: float | None = None,
        hitl=None,
        audit=None,
        mcp=None,
        auditor=None,
        cache=None,
    ) -> None:
        self.corpus = corpus
        self.llm: LLMClient = llm or MockLLM()
        from .cachestore import DictCache
        self.cache = cache or DictCache()  # get/set; Redis in production (REDIS_URL)
        # CLAUDE.md §8 default is 380 ms. LOOP_BUDGET_MS overrides it; 0 or 'none' disables the budget.
        if loop_budget_ms is None:
            raw = os.environ.get("LOOP_BUDGET_MS", "")
            loop_budget_ms = float("inf") if raw.strip().lower() in ("0", "none") else (float(raw) if raw else 380.0)
        self.loop_budget_ms = loop_budget_ms
        self.retriever = HybridRetriever(
            texts=corpus.texts, chunk_doc=corpus.chunk_doc, bm25=corpus.index,
            dense=dense, reranker=reranker,
        )
        # MCP mode: retrieval, HITL state, and audit go to the MCP servers (CLAUDE.md §5).
        self.mcp = mcp
        self.uses_mcp = mcp is not None
        if mcp is not None:
            from .mcp_bridge import McpAudit, McpHitlStore
            hitl = hitl or McpHitlStore(mcp)
            audit = audit or McpAudit(mcp)
        self.audit = audit
        self.auditor = auditor  # RagasAuditor: post-execution, async (CLAUDE.md §4)
        self.harness = harness or ToolExecutionHarness(self._tool_handlers(), hitl=hitl)

    # ---- tool handlers (called only through the harness) -----------------
    def _tool_handlers(self):
        def hybrid_retrieve(m: BaseModel) -> dict[str, Any]:
            if self.mcp is not None:
                args = {"request_id": m.request_id, "tenant_id": m.tenant_id, "query": m.query,
                        "top_k": m.top_k, "rerank_k": m.rerank_k, "threshold": m.threshold}
                return self.mcp.call("rag", "hybrid_retrieve", args, timeout_s=MCP_RAG_TIMEOUT_S)
            r = self.retriever.retrieve(m.query, top_k=m.top_k, rerank_k=m.rerank_k, threshold=m.threshold)
            return {"abstain": r.abstain, "chunks": r.chunks, "stage_ms": r.stage_ms, "flags": r.flags}

        def hitl_request(m: BaseModel) -> dict[str, Any]:
            if self.mcp is not None:
                return self.mcp.call("state", "hitl_request", {
                    "request_id": m.request_id, "idempotency_key": m.idempotency_key,
                    "tenant_id": m.tenant_id, "action": m.action, "args": m.args,
                    "risk_tier": m.risk_tier,
                }, timeout_s=5.0)
            return {"status": "PENDING_APPROVAL", "action": m.action}

        return {"hybrid_retrieve": hybrid_retrieve, "hitl_request": hitl_request}

    # ---- public entry point ---------------------------------------------
    def answer(self, principal: Principal, query: str, *, request_id: str | None = None) -> dict[str, Any]:
        """Metrics wrapper around _answer. Spans and counters follow CLAUDE.md §13."""
        started = time.perf_counter()
        result = self._answer(principal, query, request_id=request_id)
        obs.REQUEST_SECONDS.observe(time.perf_counter() - started)
        obs.REQUESTS.labels(route=str(result.get("route")), status=str(result.get("status"))).inc()
        return result

    def _answer(self, principal: Principal, query: str, *, request_id: str | None = None) -> dict[str, Any]:
        request_id = request_id or str(uuid.uuid4())
        trace: list[str] = []

        # 1. Guards (fail closed; PlatformError propagates to the API as 503).
        with obs.span("hook.pre"):
            redacted, pii_kinds = guard.redact(query)
            if pii_kinds:
                trace.append(f"pii_redacted:{','.join(pii_kinds)}")
            category = guard.threat_category(redacted)
        if category is not None:
            obs.GUARD_BLOCKS.labels(category=category).inc()
            return self._result(principal, request_id, "REJECT_OOS", REJECT_MESSAGE, [], trace + ["injection_blocked"])

        # 2. Cache (per user, tenant-scoped key).
        key = cache_key(principal.tenant_id, principal.user_id, redacted)
        with obs.span("cache.lookup"):
            hit = self.cache.get(key)
        obs.CACHE.labels(result="hit" if hit is not None else "miss").inc()
        if hit is not None:
            cached = dict(hit)
            cached["cache_hit"] = True
            cached["request_id"] = request_id
            # Every served answer is audited, including cache hits (RAGAS auditor, CLAUDE.md §9.1).
            self._audit_answer(principal, request_id, redacted, cached)
            return cached

        # 3. Triage and safety guard.
        with obs.span("triage.classify"):
            route = route_safely(classify(redacted))
        trace.append(f"route:{route}")

        if route == "REJECT_OOS":
            result = self._result(principal, request_id, route, REJECT_MESSAGE, [], trace)
        elif route == "DIRECT_ANSWER":
            result = self._result(principal, request_id, route, self._generate(redacted), [], trace)
        elif route == "TRANSACTION_EXEC":
            result = self._transaction(principal, request_id, redacted, trace)
        else:
            result = self._rag(principal, request_id, redacted, trace)

        if route in ("DIRECT_ANSWER", "HYBRID_RAG") and result.get("status") == "answered":
            self.cache.set(key, result)
        self._audit_answer(principal, request_id, redacted, result)
        return result

    def _audit_answer(self, principal: Principal, request_id: str, question: str, result: dict[str, Any]) -> None:
        """Enqueue an answered retrieval response for the RAGAS auditor. Never blocks."""
        if not (self.auditor and result.get("route") == "HYBRID_RAG"
                and result.get("status") == "answered" and result.get("citations")):
            return
        from .ragas_auditor import AuditSample
        contexts = [self.corpus.texts[c] for c in result["citations"] if c in self.corpus.texts]
        self.auditor.submit(AuditSample(principal.tenant_id, request_id, question, result["answer"], contexts))

    # ---- routes ----------------------------------------------------------
    def _rag(self, principal: Principal, request_id: str, query: str, trace: list[str]) -> dict[str, Any]:
        loop = ReActLoop(total_budget_ms=self.loop_budget_ms)
        loop.start()
        loop.transition(State.TRIAGE)
        loop.transition(State.REASONING)
        args = {"request_id": request_id, "tenant_id": principal.tenant_id, "query": query}
        args_hash = hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()
        if not loop.begin_tool_call("hybrid_retrieve", args_hash):
            # Halted before the tool ran (budget, step limit, or breaker). Degraded, never cached.
            return self._result(principal, request_id, "HYBRID_RAG", ABSTAIN_MESSAGE, [],
                                trace + [loop.state.value], status="degraded")
        try:
            with obs.POOLS["rag"].slot(), obs.span("mcp.rag.hybrid_retrieve"):
                result = self.harness.call(tool="hybrid_retrieve", subagent="retrieval", raw=args)
        except PlatformError:  # rag bulkhead full: degrade, do not queue
            return self._result(principal, request_id, "HYBRID_RAG", ABSTAIN_MESSAGE, [],
                                trace + ["E_DEGRADED", "rag_bulkhead_full"], status="degraded")
        loop.finish_tool_call(tool="hybrid_retrieve", args_hash=args_hash, step_ms=0.0, ok=result.ok)
        trace.append("tool:hybrid_retrieve")
        trace.extend(result.value.get("flags", []) if result.ok else [])
        if loop.state == State.HALTED_BUDGET:
            # Loop budget spent (CLAUDE.md §8). Return a degraded abstention, not an error.
            return self._result(principal, request_id, "HYBRID_RAG", ABSTAIN_MESSAGE, [],
                                trace + ["halted_budget"], status="degraded")
        if not result.ok:
            return self._result(principal, request_id, "HYBRID_RAG", ABSTAIN_MESSAGE,
                                [], trace + [result.error.name])
        chunks = result.value["chunks"]
        if not chunks:
            loop.transition(State.VERIFY)
            loop.transition(State.DONE)
            return self._result(principal, request_id, "HYBRID_RAG", ABSTAIN_MESSAGE, [], trace + ["abstain"])

        loop.transition(State.REASONING)
        prompt = self._prompt(query, chunks)
        answer = self._generate(prompt)
        loop.transition(State.VERIFY)
        # Verification (Gate 7): remove sentences the cited chunks do not support.
        kept, removed = verifier.verify(answer, [c["text"] for c in chunks])
        if removed:
            obs.UNSUPPORTED_CLAIMS.inc(len(removed))
            trace = trace + [f"unsupported_claims:{len(removed)}"]
        loop.transition(State.DONE)
        citations = [c["chunk_id"] for c in chunks]
        if not kept:  # nothing survived verification: abstain rather than answer
            return self._result(principal, request_id, "HYBRID_RAG", ABSTAIN_MESSAGE, [], trace + ["verified_out"])
        return self._result(principal, request_id, "HYBRID_RAG", kept, citations, trace + ["generated"])

    def _transaction(self, principal: Principal, request_id: str, query: str, trace: list[str]) -> dict[str, Any]:
        raw = {
            "request_id": request_id,
            "idempotency_key": str(uuid.uuid4()),
            "tenant_id": principal.tenant_id,
            "action": "workflow_change",
            "args": {"query": query},
            "risk_tier": "TIER_3_MUTATE",
        }
        result = self.harness.call(tool="hitl_request", subagent="execution", raw=raw)
        if self.audit is not None:
            self.audit.append(principal.tenant_id, "hitl_request",
                              {"request_id": request_id, "ok": result.ok})
        if not result.ok and result.error.code == ErrorCode.HITL_PENDING:
            return self._result(principal, request_id, "TRANSACTION_EXEC", HITL_MESSAGE, [],
                                trace + ["E_HITL_PENDING"], status="pending_approval")
        if result.ok:
            return self._result(principal, request_id, "TRANSACTION_EXEC", HITL_MESSAGE, [],
                                trace + ["hitl_request"], status="pending_approval")
        raise result.error

    # ---- helpers ---------------------------------------------------------
    def _generate(self, prompt: str) -> str:
        with obs.POOLS["llm"].slot(), obs.span("llm.generate"):  # full pool: PlatformError -> 503
            return self.llm.generate(prompt, max_tokens=512, timeout_s=30.0)

    @staticmethod
    def _prompt(query: str, chunks: list[dict[str, Any]]) -> str:
        # GoalAnchorBlock first (Gate 5), then the observations.
        sources = "\n\n".join(f"[{c['chunk_id']}] {compress_observation(c['text'])}" for c in chunks)
        return (
            f"GoalAnchorBlock: answer only from the sources. Question: {query}\n"
            f"Sources:\n{sources}\n"
            "Answer with citations in square brackets."
        )

    @staticmethod
    def _result(principal, request_id, route, answer, citations, trace, status: str = "answered"):
        return {
            "request_id": request_id,
            "tenant_id": principal.tenant_id,
            "route": route,
            "status": status,
            "answer": answer,
            "citations": citations,
            "trace": trace,
            "cache_hit": False,
        }


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
