"""RAGAS-style post-execution auditor (CLAUDE.md §4, §9.1; mcp_config.json "ragas_auditor").

What this is: an LLM-judge implementation of three RAGAS-style metrics. It is not the
ragas library. The library is not installed (disk space), and its dependency tree is large.
The metric definitions follow RAGAS:
  faithfulness       share of answer claims supported by the retrieved contexts (KPI-20)
  answer_relevancy   judge score, 0-1, how directly the answer addresses the question (KPI-21)
  context_precision  share of retrieved contexts the judge marks relevant (KPI-11 proxy)
Context entity recall (KPI-22) and context recall (§9.1) are not implemented yet.
RAGAS uses embedding-based relevancy; this judge-based version is a proxy, so its numbers
are not comparable to published RAGAS values.

Async (C21, mcp_config.json "async": true): the request path only enqueues. A background
thread judges and writes scores to SQLite (table ragas_scores). When the queue is full the
sample is dropped and counted, so the auditor never blocks a request.
"""

from __future__ import annotations

import json
import queue
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .llm import LLMClient

FAITHFULNESS_GATE = 0.92  # Gate 8 release gate; mcp_config.json min_faithfulness
JUDGE_MAX_TOKENS = 600
JUDGE_TIMEOUT_S = 60.0


class Judge(Protocol):
    def generate(self, prompt: str, *, max_tokens: int, timeout_s: float) -> str: ...


@dataclass
class AuditSample:
    tenant_id: str
    request_id: str
    question: str
    answer: str
    contexts: list[str]


@dataclass
class Scores:
    faithfulness: float | None
    answer_relevancy: float | None
    context_precision: float | None
    notes: list[str] = field(default_factory=list)


def _json_object(text: str) -> dict[str, Any] | None:
    m = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


class RagasJudge:
    """Computes the three metrics with one judge model."""

    def __init__(self, judge: Judge) -> None:
        self.judge = judge

    def _ask(self, prompt: str) -> dict[str, Any] | None:
        return _json_object(self.judge.generate(prompt, max_tokens=JUDGE_MAX_TOKENS, timeout_s=JUDGE_TIMEOUT_S))

    def faithfulness(self, answer: str, contexts: list[str]) -> tuple[float | None, str | None]:
        ctx = "\n---\n".join(contexts)
        obj = self._ask(
            "Split the ANSWER into atomic factual claims. For each claim say whether the CONTEXTS "
            "support it. Reply with JSON only: {\"claims\": [{\"claim\": str, \"supported\": true|false}]}.\n"
            f"CONTEXTS:\n{ctx}\n\nANSWER:\n{answer}"
        )
        claims = (obj or {}).get("claims")
        if not isinstance(claims, list) or not claims:
            return None, "faithfulness: no parsable claims"
        supported = sum(1 for c in claims if isinstance(c, dict) and c.get("supported") is True)
        return round(supported / len(claims), 4), None

    def answer_relevancy(self, question: str, answer: str) -> tuple[float | None, str | None]:
        obj = self._ask(
            "Rate from 0 to 1 how directly the ANSWER addresses the QUESTION. 1 means fully on point, "
            "0 means off topic. Reply with JSON only: {\"score\": number}.\n"
            f"QUESTION: {question}\n\nANSWER:\n{answer}"
        )
        score = (obj or {}).get("score")
        if not isinstance(score, (int, float)) or isinstance(score, bool):
            return None, "answer_relevancy: no parsable score"
        return round(min(1.0, max(0.0, float(score))), 4), None

    def context_precision(self, question: str, contexts: list[str]) -> tuple[float | None, str | None]:
        """RAGAS context precision: average precision over ranks.

        verdicts v_k (1 = relevant) for the contexts in rank order.
        score = sum_k(precision@k * v_k) / (number of relevant contexts).
        Returns 0.0 when no context is relevant, and None when no verdict parsed.
        """
        if not contexts:
            return None, "context_precision: no contexts"
        verdicts: list[bool] = []
        for ctx in contexts:
            obj = self._ask(
                "Is this CONTEXT useful for answering the QUESTION? Reply with JSON only: "
                "{\"relevant\": true|false}.\n"
                f"QUESTION: {question}\n\nCONTEXT:\n{ctx}"
            )
            if isinstance(obj, dict) and isinstance(obj.get("relevant"), bool):
                verdicts.append(obj["relevant"])
            else:
                verdicts.append(None)  # unparsable: excluded from the score
        known = [(i, v) for i, v in enumerate(verdicts) if v is not None]
        if not known:
            return None, "context_precision: no parsable judgements"
        hits = 0
        total_relevant = sum(1 for _, v in known if v)
        if total_relevant == 0:
            return 0.0, None
        ap = 0.0
        for rank, (_, v) in enumerate(known, start=1):
            if v:
                hits += 1
                ap += hits / rank
        return round(ap / total_relevant, 4), None

    def score(self, sample: AuditSample) -> Scores:
        notes: list[str] = []
        f, n1 = self.faithfulness(sample.answer, sample.contexts)
        a, n2 = self.answer_relevancy(sample.question, sample.answer)
        p, n3 = self.context_precision(sample.question, sample.contexts)
        notes.extend(n for n in (n1, n2, n3) if n)
        return Scores(faithfulness=f, answer_relevancy=a, context_precision=p, notes=notes)


class RagasStore:
    """SQLite table of scores. Every row carries tenant_id."""

    def __init__(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS ragas_scores (
                   seq INTEGER PRIMARY KEY AUTOINCREMENT,
                   tenant_id TEXT NOT NULL,
                   request_id TEXT NOT NULL,
                   faithfulness REAL, answer_relevancy REAL, context_precision REAL,
                   notes TEXT NOT NULL, judge_model TEXT NOT NULL, created_at TEXT NOT NULL)"""
        )
        self._conn.commit()

    def save(self, tenant_id: str, request_id: str, scores: Scores, judge_model: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO ragas_scores (tenant_id, request_id, faithfulness, answer_relevancy, "
                "context_precision, notes, judge_model, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (tenant_id, request_id, scores.faithfulness, scores.answer_relevancy,
                 scores.context_precision, json.dumps(scores.notes), judge_model,
                 datetime.now(timezone.utc).isoformat()),
            )
            self._conn.commit()

    def rows(self, tenant_id: str) -> list[tuple]:
        with self._lock:
            return self._conn.execute(
                "SELECT request_id, faithfulness, answer_relevancy, context_precision FROM ragas_scores "
                "WHERE tenant_id = ? ORDER BY seq", (tenant_id,)
            ).fetchall()


class RagasAuditor:
    """Background worker. submit() never blocks the request path."""

    def __init__(self, judge: LLMClient | Judge, store: RagasStore, judge_model: str,
                 max_queue: int = 256, sample_rate: float = 1.0) -> None:
        self.judge = RagasJudge(judge)
        self.store = store
        self.judge_model = judge_model
        self.sample_rate = sample_rate
        self.dropped = 0
        self.errors = 0
        self._queue: queue.Queue[AuditSample | None] = queue.Queue(maxsize=max_queue)
        self._acc = 0.0
        self._thread = threading.Thread(target=self._run, name="ragas-auditor", daemon=True)
        self._thread.start()

    def submit(self, sample: AuditSample) -> bool:
        """Enqueue one sample. Returns False if it was not queued (sampled out or queue full)."""
        # Deterministic sampling: keep every 1/sample_rate-th sample.
        self._acc += self.sample_rate
        if self._acc < 1.0:
            return False
        self._acc -= 1.0
        try:
            self._queue.put_nowait(sample)
            return True
        except queue.Full:
            self.dropped += 1
            return False

    def drain(self, timeout_s: float = 60.0) -> None:
        """Wait until queued samples are processed (used by tests and the golden-run script)."""
        deadline = time.monotonic() + timeout_s
        while not self._queue.empty() or self._queue.unfinished_tasks:
            if time.monotonic() > deadline:
                raise TimeoutError("ragas queue did not drain")
            time.sleep(0.05)

    def _run(self) -> None:
        while True:
            sample = self._queue.get()
            try:
                if sample is None:
                    return
                scores = self.judge.score(sample)
                self.store.save(sample.tenant_id, sample.request_id, scores, self.judge_model)
            except Exception:  # noqa: BLE001 - an auditor failure must not stop the worker
                self.errors += 1
            finally:
                self._queue.task_done()
