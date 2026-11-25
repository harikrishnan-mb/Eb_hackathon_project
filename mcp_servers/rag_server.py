"""MCP server: hybrid-rag (CLAUDE.md §5, mcp_config.json "hybrid-rag").

Transport: stdio (long-lived process, one per host). Tool: hybrid_retrieve.
Arguments are validated against assistant.contracts.HybridRetrieveInput (strict,
extra fields forbidden, UUIDv4 request_id, tenant pattern). Tier: TIER_1_READ.

Environment:
  CORPUS_DIR   knowledge base directory (default knowledge_vault/)
  RETRIEVAL_MODE  bm25 (default) or hybrid (loads dense and reranker from MODELS_DIR)
  MODELS_DIR   directory with bge-large-en-v1.5 and bge-reranker-v2-m3 (hybrid only)
  LOOP_BUDGET_MS is not used here; the host enforces its own budget.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from assistant.contracts import HybridRetrieveInput  # noqa: E402
from assistant.retrieval.pipeline import HybridRetriever  # noqa: E402

mcp = FastMCP("hybrid-rag")
_retriever: HybridRetriever | None = None


def _build_retriever() -> HybridRetriever:
    """Same pipeline as the API (assistant/orchestrator.py), without the LLM."""
    from assistant.orchestrator import Corpus

    corpus_dir = os.environ.get("CORPUS_DIR", str(ROOT / "knowledge_vault"))
    mode = os.environ.get("RETRIEVAL_MODE", "bm25").lower()
    if mode != "hybrid":
        corpus = Corpus.from_dir(corpus_dir)
        return HybridRetriever(corpus.texts, corpus.chunk_doc, corpus.index)

    from assistant.retrieval.dense import BGEEncoder, DenseIndex
    from assistant.retrieval.rerank import CrossEncoder

    models = Path(os.environ["MODELS_DIR"])
    encoder = BGEEncoder(models / "bge-large-en-v1.5")
    corpus = Corpus.from_dir(corpus_dir, tokenizer=encoder.tokenizer)
    dense = DenseIndex(encoder, corpus.chunks)
    reranker = CrossEncoder(models / "bge-reranker-v2-m3")
    return HybridRetriever(corpus.texts, corpus.chunk_doc, corpus.index, dense=dense, reranker=reranker)


_load_lock = threading.Lock()


def _get_retriever() -> HybridRetriever:
    """Load once. The lock makes a concurrent first call wait for the warm-up instead of loading twice."""
    global _retriever
    with _load_lock:
        if _retriever is None:
            _retriever = _build_retriever()
    return _retriever


def ready() -> dict:
    """True once the retriever (and in hybrid mode, the models) is loaded."""
    return {"ready": _retriever is not None}


def _warm_up() -> None:
    try:
        _get_retriever()
    except Exception as exc:  # noqa: BLE001 - reported through ready() staying false
        print(f"rag warm-up failed: {exc}", file=sys.stderr, flush=True)


def hybrid_retrieve(
    request_id: str,
    tenant_id: str,
    query: str,
    top_k: int = 50,
    rerank_k: int = 4,
    threshold: float = 0.42,
) -> dict:
    """Run the 3-stage hybrid retrieval. Returns abstain, chunks, stage_ms, flags."""
    try:
        args = HybridRetrieveInput(
            request_id=request_id, tenant_id=tenant_id, query=query,
            top_k=top_k, rerank_k=rerank_k, threshold=threshold,
        )
    except ValidationError as exc:
        return {"error": {"code": -32602, "name": "E_VALIDATION", "detail": exc.errors(include_input=False)}}
    r = _get_retriever().retrieve(args.query, top_k=args.top_k, rerank_k=args.rerank_k, threshold=args.threshold)
    return {"abstain": r.abstain, "chunks": r.chunks, "stage_ms": r.stage_ms, "flags": r.flags}


mcp.tool(name="hybrid_retrieve")(hybrid_retrieve)

mcp.tool(name="ready")(ready)

if __name__ == "__main__":
    # Load models in the background so the MCP handshake is immediate and the first query is fast.
    threading.Thread(target=_warm_up, name="rag-warm-up", daemon=True).start()
    mcp.run()
