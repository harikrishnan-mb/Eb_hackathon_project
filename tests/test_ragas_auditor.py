import json
import threading

from assistant.auth import Principal
from assistant.orchestrator import Corpus, Orchestrator
from assistant.ragas_auditor import AuditSample, RagasAuditor, RagasJudge, RagasStore, Scores
from pathlib import Path

KB = Path(__file__).resolve().parents[1] / "knowledge_vault"


class FakeJudge:
    """Answers the three judge prompts. claims_supported is the number of supported claims out of 4."""

    def __init__(self, claims_supported=3, relevancy=0.9, relevant_contexts=True):
        self.claims_supported = claims_supported
        self.relevancy = relevancy
        self.relevant_contexts = relevant_contexts

    def generate(self, prompt, *, max_tokens, timeout_s):
        if "atomic factual claims" in prompt:
            claims = [{"claim": f"c{i}", "supported": i < self.claims_supported} for i in range(4)]
            return json.dumps({"claims": claims})
        if "Rate from 0 to 1" in prompt:
            return json.dumps({"score": self.relevancy})
        if "useful for answering" in prompt:
            return json.dumps({"relevant": self.relevant_contexts})
        return "not json"


SAMPLE = AuditSample("tenant_a", "r-1", "What is the return window?", "30 days.", ["ctx one", "ctx two"])


def test_faithfulness_is_share_of_supported_claims():
    j = RagasJudge(FakeJudge(claims_supported=3))
    assert j.faithfulness("ans", ["ctx"]) == (0.75, None)


def test_unparsable_judge_output_gives_none_with_note():
    j = RagasJudge(FakeJudge())
    j.judge = type("Bad", (), {"generate": lambda self, p, **k: "no json here"})()
    scores = j.score(SAMPLE)
    assert scores.faithfulness is None and scores.answer_relevancy is None
    assert len(scores.notes) == 3


def test_relevancy_is_clamped_to_unit_interval():
    assert RagasJudge(FakeJudge(relevancy=7)).answer_relevancy("q", "a") == (1.0, None)


def test_worker_saves_scores_per_tenant(tmp_path):
    store = RagasStore(tmp_path / "r.db")
    aud = RagasAuditor(FakeJudge(), store, judge_model="fake")
    aud.submit(SAMPLE)
    aud.submit(AuditSample("tenant_b", "r-2", "q", "a", ["c"]))
    aud.drain()
    assert [r[0] for r in store.rows("tenant_a")] == ["r-1"]
    assert [r[0] for r in store.rows("tenant_b")] == ["r-2"]


def test_full_queue_drops_instead_of_blocking(tmp_path):
    gate = threading.Event()

    class Blocking(FakeJudge):
        def generate(self, prompt, **kw):
            gate.wait(5)
            return super().generate(prompt, **kw)

    aud = RagasAuditor(Blocking(), RagasStore(tmp_path / "r.db"), judge_model="fake", max_queue=1)
    results = [aud.submit(SAMPLE) for _ in range(4)]
    gate.set()
    aud.drain()
    assert aud.dropped >= 1 and results.count(False) >= 1


def test_sample_rate_keeps_fraction(tmp_path):
    aud = RagasAuditor(FakeJudge(), RagasStore(tmp_path / "r.db"), judge_model="fake", sample_rate=0.5)
    kept = sum(aud.submit(SAMPLE) for _ in range(10))
    aud.drain()
    assert kept == 5


def test_orchestrator_enqueues_answered_rag_responses(tmp_path):
    store = RagasStore(tmp_path / "r.db")
    aud = RagasAuditor(FakeJudge(), store, judge_model="fake")
    orch = Orchestrator(Corpus.from_dir(KB), auditor=aud)
    r = orch.answer(Principal("tenant_a", "u1"), "What is the returns policy?")
    assert r["route"] == "HYBRID_RAG"
    aud.drain()
    rows = store.rows("tenant_a")
    assert len(rows) == 1 and rows[0][1] == 0.75


class VerdictJudge:
    def __init__(self, verdicts):
        self.verdicts = list(verdicts)

    def generate(self, prompt, **kw):
        return json.dumps({"relevant": self.verdicts.pop(0)})


def test_context_precision_is_rank_weighted_average_precision():
    # relevant at ranks 1 and 3 of 3 contexts: (1/1 + 2/3) / 2
    j = RagasJudge(VerdictJudge([True, False, True]))
    assert j.context_precision("q", ["a", "b", "c"]) == (0.8333, None)


def test_single_relevant_context_at_rank_one_scores_one():
    j = RagasJudge(VerdictJudge([True, False, False, False]))
    assert j.context_precision("q", ["a", "b", "c", "d"]) == (1.0, None)


def test_no_relevant_context_scores_zero():
    j = RagasJudge(VerdictJudge([False, False]))
    assert j.context_precision("q", ["a", "b"]) == (0.0, None)


def test_cache_hits_are_audited_too(tmp_path):
    store = RagasStore(tmp_path / "r.db")
    aud = RagasAuditor(FakeJudge(), store, judge_model="fake")
    orch = Orchestrator(Corpus.from_dir(KB), auditor=aud)
    p = Principal("tenant_a", "u1")
    first = orch.answer(p, "What is the returns policy?")
    second = orch.answer(p, "What is the returns policy?")
    assert second["cache_hit"] is True and first["cache_hit"] is False
    aud.drain()
    assert len(store.rows("tenant_a")) == 2
