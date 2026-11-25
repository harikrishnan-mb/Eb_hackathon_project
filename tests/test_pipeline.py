"""Hybrid pipeline with stub dense and reranker (no model weights loaded)."""

from dataclasses import dataclass

from assistant.retrieval.bm25 import BM25Index
from assistant.retrieval.pipeline import HybridRetriever

TEXTS = {
    "note_returns-0": "Returns are accepted within 30 days for SKU-001 Ceramic Mug.",
    "note_freight-0": "Carrier SLA covers freight delivery windows for pallets.",
    "note_lot-0": "LOT-00044 was recalled after a seal failure.",
}
CHUNK_DOC = {c: c.rsplit("-", 1)[0] for c in TEXTS}


@dataclass
class _Hit:
    chunk_id: str
    doc_id: str
    cosine: float
    rank: int


class StubDense:
    def __init__(self, order):
        self.order = order

    def search(self, query, top_k=50):
        return [_Hit(c, CHUNK_DOC[c], 0.9 - i * 0.1, i + 1) for i, c in enumerate(self.order)]


class StubReranker:
    def __init__(self, scores):
        self.scores = scores  # chunk_id -> score

    def score(self, query, passages):
        return [self.scores[next(c for c, t in TEXTS.items() if t == p)] for p in passages]


def _retriever(dense=None, reranker=None):
    return HybridRetriever(TEXTS, CHUNK_DOC, BM25Index(TEXTS), dense=dense, reranker=reranker)


def test_bm25_only_flags_dense_and_rerank_unavailable():
    r = _retriever().retrieve("returns policy for SKU-001", rerank_k=4)
    assert "dense_unavailable" in r.flags and "rerank_unavailable" in r.flags
    assert r.chunks[0]["chunk_id"] == "note_returns-0"
    assert r.abstain is False


def test_dense_is_fused_and_reranker_orders_output():
    dense = StubDense(["note_freight-0", "note_returns-0", "note_lot-0"])
    rr = StubReranker({"note_returns-0": 0.95, "note_freight-0": 0.1, "note_lot-0": 0.2})
    r = _retriever(dense, rr).retrieve("returns policy for SKU-001")
    assert r.flags == []
    assert r.chunks[0]["chunk_id"] == "note_returns-0"
    assert set(r.stage_ms) >= {"bm25", "biencoder", "rrf", "rerank"}


def test_reranker_below_threshold_abstains():
    dense = StubDense(["note_freight-0", "note_returns-0"])
    rr = StubReranker({"note_returns-0": 0.2, "note_freight-0": 0.3, "note_lot-0": 0.0})
    r = _retriever(dense, rr).retrieve("returns policy", threshold=0.42)
    assert r.abstain is True and r.chunks == []


def test_no_match_abstains_without_models():
    r = _retriever().retrieve("zzzz qqqq unknown tokens")
    assert r.abstain is True


def test_budget_halt_returns_degraded_answer_not_error(monkeypatch, tmp_path):
    import itertools

    from assistant import orchestrator as orch_mod
    from assistant.auth import Principal
    from assistant.loop import ReActLoop

    # Every clock read advances one second, so the 380 ms budget is spent at the first tool call.
    ticks = itertools.count(0, 1.0)

    class SlowLoop(ReActLoop):
        def __init__(self, **kwargs):
            super().__init__(clock=lambda: next(ticks), **kwargs)

    monkeypatch.setattr(orch_mod, "ReActLoop", SlowLoop)
    (tmp_path / "note_a.md").write_text("---\nid: note_a\n---\nReturns are accepted within 30 days.")
    orch = orch_mod.Orchestrator(orch_mod.Corpus.from_dir(tmp_path))
    r = orch.answer(Principal(tenant_id="tenant_a", user_id="u1"), "What is the returns window?")
    assert r["status"] == "degraded"
    assert "HALTED_BUDGET" in r["trace"]
