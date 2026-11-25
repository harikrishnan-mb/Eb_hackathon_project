"""Hybrid retrieval pipeline: BM25 -> dense -> RRF -> cross-encoder (CLAUDE.md §9).

Stages run in order. Dense and reranker are optional. When one is missing the
pipeline degrades and flags it (CLAUDE.md §9 degradations):
  no dense     -> BM25 only, flag "dense_unavailable"
  no reranker  -> RRF top rerank_k, flag "rerank_unavailable", no abstention short-circuit
  reranker top score < threshold -> abstain
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from .bm25 import BM25Index
from .fusion import rrf


class Encoder(Protocol):
    def search(self, query: str, top_k: int = 50) -> list[Any]: ...


class Scorer(Protocol):
    def score(self, query: str, passages: list[str]) -> list[float]: ...


@dataclass
class RetrievalResult:
    abstain: bool
    chunks: list[dict[str, Any]]
    stage_ms: dict[str, float]
    flags: list[str] = field(default_factory=list)


class HybridRetriever:
    def __init__(
        self,
        texts: dict[str, str],          # chunk_id -> chunk text
        chunk_doc: dict[str, str],      # chunk_id -> doc_id (note id)
        bm25: BM25Index,
        dense: Encoder | None = None,
        reranker: Scorer | None = None,
        rrf_pool: int = 40,
    ) -> None:
        self.texts = texts
        self.chunk_doc = chunk_doc
        self.bm25 = bm25
        self.dense = dense
        self.reranker = reranker
        self.rrf_pool = rrf_pool

    def retrieve(self, query: str, top_k: int = 50, rerank_k: int = 4, threshold: float = 0.42) -> RetrievalResult:
        stage_ms: dict[str, float] = {}
        flags: list[str] = []

        t = time.perf_counter()
        bm_ids = [h.doc_id for h in self.bm25.search(query, top_k=top_k)]
        stage_ms["bm25"] = round((time.perf_counter() - t) * 1e3, 3)

        if self.dense is None:
            flags.append("dense_unavailable")
            ranked_ids = bm_ids[: self.rrf_pool]
            cosine: dict[str, float] = {}
        else:
            t = time.perf_counter()
            hits = self.dense.search(query, top_k=top_k)
            stage_ms["biencoder"] = round((time.perf_counter() - t) * 1e3, 3)
            dense_ids = [h.chunk_id for h in hits]
            cosine = {h.chunk_id: h.cosine for h in hits}
            t = time.perf_counter()
            ranked_ids = [cid for cid, _ in rrf([bm_ids, dense_ids], pool=self.rrf_pool)]
            stage_ms["rrf"] = round((time.perf_counter() - t) * 1e3, 3)

        if not ranked_ids:
            return RetrievalResult(abstain=True, chunks=[], stage_ms=stage_ms, flags=flags)

        bm25_scores = self.bm25.score(query)
        if self.reranker is None:
            flags.append("rerank_unavailable")
            order = ranked_ids[:rerank_k]
            rerank_scores = {cid: 0.0 for cid in order}
        else:
            t = time.perf_counter()
            scores = self.reranker.score(query, [self.texts[c] for c in ranked_ids])
            stage_ms["rerank"] = round((time.perf_counter() - t) * 1e3, 3)
            ordered = sorted(zip(ranked_ids, scores), key=lambda x: (-x[1], x[0]))
            if not ordered or ordered[0][1] < threshold:
                return RetrievalResult(abstain=True, chunks=[], stage_ms=stage_ms, flags=flags)
            order = [cid for cid, _ in ordered[:rerank_k]]
            rerank_scores = dict(ordered)

        chunks = []
        for rank, cid in enumerate(order, start=1):
            text = self.texts[cid]
            chunks.append({
                "chunk_id": cid,
                "chunk_hash": _sha(text),
                "text": text.strip()[:4096],
                "bm25": round(bm25_scores.get(cid, 0.0), 6),
                "cosine": round(cosine.get(cid, 0.0), 6),
                "rrf_rank": rank,
                "rerank_score": round(rerank_scores.get(cid, 0.0), 6),
            })
        return RetrievalResult(abstain=not chunks, chunks=chunks, stage_ms=stage_ms, flags=flags)


def _sha(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
