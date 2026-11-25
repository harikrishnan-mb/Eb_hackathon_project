"""Measure Stage 1 (BM25) retrieval on the synthetic golden set.

Metrics (binary relevance; relevant set = gold_note_ids):
  Recall@50   fraction of gold notes found in the top 50      (KPI-10 family, BM25 only here)
  MRR@4       reciprocal rank of the first relevant doc in top 4 (KPI-12)
  nDCG@4      normalized DCG over the top 4                       (KPI-13)
  latency     p50 / p95 of BM25 search per query                  (KPI-07 family)

Only BM25 is measured. Dense retrieval and the cross-encoder are not built yet,
so these are a BM25-only baseline, not the hybrid KPI values. Negatives have no
gold notes and are excluded from retrieval metrics. Their abstention needs the
reranker score and is measured later.

Writes reports/retrieval/bm25_eval.json.
"""

from __future__ import annotations

import json
import math
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from assistant.retrieval.bm25 import BM25Index  # noqa: E402


def load_corpus() -> dict[str, str]:
    docs = {}
    for path in sorted((ROOT / "knowledge_vault").glob("*.md")):
        text = path.read_text(encoding="utf-8")
        body = text.split("---", 2)[-1]  # drop frontmatter
        docs[path.stem] = body
    return docs


def ndcg_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    dcg = sum(1.0 / math.log2(i + 2) for i, d in enumerate(ranked[:k]) if d in relevant)
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(len(relevant), k)))
    return dcg / ideal if ideal else 0.0


def mrr_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    for i, d in enumerate(ranked[:k]):
        if d in relevant:
            return 1.0 / (i + 1)
    return 0.0


def main() -> None:
    index = BM25Index(load_corpus())
    golden = [json.loads(l) for l in (ROOT / "data/synthetic/golden_set.jsonl").read_text().splitlines()]
    rows = [r for r in golden if r["answerable"]]

    recall, mrr, ndcg, latencies_ms = [], [], [], []
    by_type: dict[str, list[float]] = {}
    for r in rows:
        relevant = set(r["gold_note_ids"])
        t0 = time.perf_counter()
        hits = index.search(r["question"], top_k=50)
        latencies_ms.append((time.perf_counter() - t0) * 1000.0)
        ranked = [h.doc_id for h in hits]
        rec = len(relevant & set(ranked)) / len(relevant)
        m = mrr_at_k(ranked, relevant, 4)
        n = ndcg_at_k(ranked, relevant, 4)
        recall.append(rec)
        mrr.append(m)
        ndcg.append(n)
        by_type.setdefault(r["type"], []).append(n)

    lat = sorted(latencies_ms)
    report = {
        "stage": "BM25 only (Stage 1)",
        "corpus_notes": len(index.doc_ids),
        "queries": len(rows),
        "recall_at_50": round(statistics.fmean(recall), 4),
        "mrr_at_4": round(statistics.fmean(mrr), 4),
        "ndcg_at_4": round(statistics.fmean(ndcg), 4),
        "ndcg_at_4_by_type": {k: round(statistics.fmean(v), 4) for k, v in sorted(by_type.items())},
        "latency_ms_p50": round(lat[len(lat) // 2], 3),
        "latency_ms_p95": round(lat[int(len(lat) * 0.95) - 1], 3),
        "note": "Synthetic corpus of 38 short notes. Latency is not representative of 512-token chunks at scale.",
    }
    out = ROOT / "reports" / "retrieval"
    out.mkdir(parents=True, exist_ok=True)
    (out / "bm25_eval.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
