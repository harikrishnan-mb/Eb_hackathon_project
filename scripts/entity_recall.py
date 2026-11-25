"""KPI-22 context entity recall, lexical (no judge). Run: python scripts/entity_recall.py

Entities = numbers, SKU/LOT codes, and capitalised words in each gold answer (stopwords removed).
Recall per question = share of those entities that appear in the top-4 BM25 contexts.
Writes reports/retrieval/entity_recall_golden.json.
"""

from __future__ import annotations

import json
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from assistant.orchestrator import Corpus  # noqa: E402
from assistant.retrieval.bm25 import STOPWORDS  # noqa: E402

_ENT = re.compile(r"\b(?:SKU-\d{3}|LOT-\d{5}|\d+(?:\.\d+)?|[A-Z][a-z]+)\b")


def entities(text: str) -> set[str]:
    return {e.lower() for e in _ENT.findall(text) if e.lower() not in STOPWORDS and len(e) > 1}


def main() -> None:
    corpus = Corpus.from_dir(ROOT / "knowledge_vault")
    rows = [json.loads(l) for l in (ROOT / "data/synthetic/golden_set.jsonl").read_text().splitlines()]
    rows = [r for r in rows if r["answerable"] and r.get("gold_answer")]
    scores = []
    for r in rows:
        ents = entities(r["gold_answer"])
        if not ents:
            continue
        top = [h.doc_id for h in corpus.index.search(r["question"], top_k=4)]
        ctx = " ".join(corpus.texts[c] for c in top).lower()
        found = sum(1 for e in ents if e in ctx)
        scores.append(found / len(ents))
    out = {
        "kpi": "KPI-22", "method": "lexical entity overlap of gold answer with top-4 BM25 contexts; no judge",
        "questions": len(scores), "mean_entity_recall": round(statistics.fmean(scores), 4),
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "note": "Synthetic golden set. Lexical proxy, not the RAGAS judge. Not KPI sign-off.",
    }
    (ROOT / "reports/retrieval/entity_recall_golden.json").write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
