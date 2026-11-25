"""Evaluate the full retrieval pipeline on the synthetic golden set.

Stages: BM25 (Stage 1) and bge-large dense (Stage 2) -> RRF (k=60, pool=40)
-> bge-reranker-v2-m3 cross-encoder (Stage 3), threshold 0.42 for abstention.

Measured on 213 answerable questions (retrieval) and 37 negatives (abstention).
Relevance: chunk <note_id>-0 is relevant if the note is in gold_note_ids.
Writes reports/retrieval/hybrid_eval.json.

These are measurements on synthetic data, not KPI sign-off. The corpus is easy
and templated. Latency is CPU-only, PyTorch, and not the ONNX FP16 build.
"""

from __future__ import annotations

import argparse
import json
import os
import math
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from assistant.retrieval.bm25 import BM25Index  # noqa: E402
from assistant.retrieval.dense import BGEEncoder, Chunk, DenseIndex, chunk_document  # noqa: E402
from assistant.retrieval.fusion import rrf  # noqa: E402
from assistant.retrieval.rerank import ABSTAIN_THRESHOLD, CrossEncoder  # noqa: E402

MODELS = Path(os.environ.get("MODELS_DIR", ROOT / "models"))  # downloaded by huggingface_hub


def pct(values: list[float], p: float) -> float:
    s = sorted(values)
    return round(s[min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))], 3)


def mrr4(ranked: list[str], rel: set[str]) -> float:
    for i, d in enumerate(ranked[:4]):
        if d in rel:
            return 1.0 / (i + 1)
    return 0.0


def ndcg4(ranked: list[str], rel: set[str]) -> float:
    dcg = sum(1 / math.log2(i + 2) for i, d in enumerate(ranked[:4]) if d in rel)
    ideal = sum(1 / math.log2(i + 2) for i in range(min(len(rel), 4)))
    return dcg / ideal if ideal else 0.0


def recall(ranked: list[str], rel: set[str], k: int) -> float:
    return len(set(ranked[:k]) & rel) / len(rel)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", default=str(ROOT / "knowledge_vault"))
    parser.add_argument("--golden", default=str(ROOT / "data/synthetic/golden_set.jsonl"))
    parser.add_argument("--out", default=str(ROOT / "reports/retrieval/hybrid_eval.json"))
    parser.add_argument("--limit", type=int, default=0,
                        help="use only the first N questions of the golden file (0 = all)")
    args = parser.parse_args()

    docs, bodies = {}, {}
    for p in sorted(Path(args.corpus).glob("*.md")):
        body = p.read_text(encoding="utf-8").split("---", 2)[-1]
        docs[p.stem] = body
        bodies[p.stem] = body

    encoder = BGEEncoder(MODELS / "bge-large-en-v1.5")
    chunks: list[Chunk] = []
    for doc_id, body in docs.items():
        chunks.extend(chunk_document(doc_id, body, encoder.tokenizer))
    text_of = {c.chunk_id: c.text for c in chunks}
    chunk_doc = {c.chunk_id: c.doc_id for c in chunks}

    bm25 = BM25Index({c.chunk_id: c.text for c in chunks})
    dense = DenseIndex(encoder, chunks)
    reranker = CrossEncoder(MODELS / "bge-reranker-v2-m3")

    golden = [json.loads(l) for l in Path(args.golden).read_text().splitlines()]
    if args.limit:
        golden = golden[: args.limit]
    answerable = [r for r in golden if r["answerable"]]
    negatives = [r for r in golden if not r["answerable"]]

    lat = {"bm25": [], "dense": [], "rrf": [], "rerank": []}
    m = {"bm25_recall50": [], "dense_recall50": [], "rrf_recall40": [],
         "rrf_mrr4": [], "rrf_ndcg4": [], "rerank_mrr4": [], "rerank_ndcg4": []}
    by_type: dict[str, list[float]] = {}
    answerable_abstained = 0

    def to_docs(chunk_ids: list[str]) -> list[str]:
        return [chunk_doc[c] for c in chunk_ids]

    t_start = time.perf_counter()
    width = 40
    for i, r in enumerate(answerable, start=1):
        rel = set(r["gold_note_ids"])
        q = r["question"]
        done = i - 1
        if done:
            elapsed = time.perf_counter() - t_start
            eta = elapsed / done * (len(answerable) - done)
            bar = "#" * int(width * done / len(answerable))
            print(f"\r[{bar:<{width}}] {done}/{len(answerable)} "
                  f"elapsed {elapsed/60:.1f} min, eta {eta/60:.1f} min", end="", file=sys.stderr, flush=True)

        t = time.perf_counter(); b_hits = bm25.search(q, top_k=50); lat["bm25"].append((time.perf_counter() - t) * 1e3)
        t = time.perf_counter(); d_hits = dense.search(q, top_k=50); lat["dense"].append((time.perf_counter() - t) * 1e3)

        b_ids = [h.doc_id for h in b_hits]
        d_ids = [h.chunk_id for h in d_hits]
        m["bm25_recall50"].append(recall(to_docs(b_ids), rel, 50))
        m["dense_recall50"].append(recall(to_docs(d_ids), rel, 50))

        t = time.perf_counter(); fused = rrf([b_ids, d_ids]); lat["rrf"].append((time.perf_counter() - t) * 1e3)
        fused_ids = [cid for cid, _ in fused]
        fused_docs = to_docs(fused_ids)
        m["rrf_recall40"].append(recall(fused_docs, rel, 40))
        m["rrf_mrr4"].append(mrr4(fused_docs, rel))
        m["rrf_ndcg4"].append(ndcg4(fused_docs, rel))

        t = time.perf_counter()
        scores = reranker.score(q, [text_of[c] for c in fused_ids])
        lat["rerank"].append((time.perf_counter() - t) * 1e3)
        ranked = sorted(zip(fused_ids, scores), key=lambda x: -x[1])
        top_score = ranked[0][1] if ranked else 0.0
        if top_score < ABSTAIN_THRESHOLD:
            answerable_abstained += 1
        rerank_docs = to_docs([cid for cid, _ in ranked[:4]])
        m["rerank_mrr4"].append(mrr4(rerank_docs, rel))
        n = ndcg4(rerank_docs, rel)
        m["rerank_ndcg4"].append(n)
        by_type.setdefault(r["type"], []).append(n)

    neg_abstained = 0
    for r in negatives:
        b_ids = [h.doc_id for h in bm25.search(r["question"], top_k=50)]
        d_ids = [h.chunk_id for h in dense.search(r["question"], top_k=50)]
        fused_ids = [cid for cid, _ in rrf([b_ids, d_ids])]
        scores = reranker.score(r["question"], [text_of[c] for c in fused_ids]) if fused_ids else []
        top = max(scores) if scores else 0.0
        if top < ABSTAIN_THRESHOLD:
            neg_abstained += 1

    report = {
        "stages": "BM25 + bge-large dense -> RRF(k=60,pool=40) -> bge-reranker-v2-m3 (PyTorch CPU)",
        "answerable_questions": len(answerable),
        "negative_questions": len(negatives),
        "chunks": len(chunks),
        "retrieval": {
            "bm25_recall_at_50": round(statistics.fmean(m["bm25_recall50"]), 4),
            "dense_recall_at_50": round(statistics.fmean(m["dense_recall50"]), 4),
            "rrf_recall_at_40": round(statistics.fmean(m["rrf_recall40"]), 4),
            "rrf_mrr_at_4": round(statistics.fmean(m["rrf_mrr4"]), 4),
            "rrf_ndcg_at_4": round(statistics.fmean(m["rrf_ndcg4"]), 4),
        },
        "after_rerank": {
            "mrr_at_4": round(statistics.fmean(m["rerank_mrr4"]), 4),
            "ndcg_at_4": round(statistics.fmean(m["rerank_ndcg4"]), 4),
            "ndcg_at_4_by_type": {k: round(statistics.fmean(v), 4) for k, v in sorted(by_type.items())},
            "false_abstention_rate": round(answerable_abstained / len(answerable), 4),
        },
        "abstention": {
            "threshold": ABSTAIN_THRESHOLD,
            "negatives_abstained": neg_abstained,
            "accuracy": round(neg_abstained / len(negatives), 4) if negatives else None,
        },
        "latency_ms_p50_p95": {k: [pct(v, 0.5), pct(v, 0.95)] for k, v in lat.items()},
        "note": (f"Synthetic corpus of {len(docs)} notes, {len(chunks)} chunks. CPU PyTorch reranker, not ONNX FP16. "
                 "Not KPI sign-off."),
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
