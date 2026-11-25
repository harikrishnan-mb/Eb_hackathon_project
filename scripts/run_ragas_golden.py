"""Run the golden set through the pipeline and score it with the RAGAS-style auditor.

Each question goes through the orchestrator (default BM25 retrieval, OpenRouter answers).
Answered HYBRID_RAG responses are judged by the same OpenRouter model (a self-judge, so
scores may be inflated; see assistant/ragas_auditor.py). Costs about 2 + k model calls per
question (k = number of contexts). The full 250-question set costs roughly 1,500 calls.

Writes reports/ragas/golden_run_<UTC date>.json. Synthetic data, proxy metrics: not KPI sign-off.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from assistant.auth import Principal  # noqa: E402
from assistant.llm import from_env  # noqa: E402
from assistant.orchestrator import Corpus, Orchestrator  # noqa: E402
from assistant.backends import ragas_for  # noqa: E402
from assistant.ragas_auditor import RagasAuditor  # noqa: E402
from assistant.pgstore import PgStateDB  # noqa: E402

GATE = {"faithfulness": 0.92, "answer_relevancy": 0.90, "context_precision": 0.88}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", default=str(ROOT / "data/synthetic/golden_set.jsonl"))
    ap.add_argument("--corpus", default=str(ROOT / "knowledge_vault"))
    ap.add_argument("--limit", type=int, default=5, help="questions to run (default 5 for a smoke test)")
    args = ap.parse_args()

    llm = from_env()
    judge_model = getattr(llm, "model", "unknown")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    out_dir = ROOT / "reports" / "ragas"
    out_dir.mkdir(parents=True, exist_ok=True)
    db = out_dir / f"golden_run_{stamp}_{args.limit}q.sqlite"  # one database per run
    url = os.environ.get('STATE_DATABASE_URL', '')
    store = ragas_for(PgStateDB(url) if url else db)
    auditor = RagasAuditor(llm, store, judge_model=judge_model)
    orch = Orchestrator(Corpus.from_dir(args.corpus), llm=llm, auditor=auditor)

    rows = [json.loads(l) for l in Path(args.golden).read_text().splitlines()][: args.limit]
    principal = Principal(tenant_id="tenant_eval", user_id="golden")
    routes: dict[str, int] = {}
    answerable_ids: set[str] = set()  # request ids of answerable questions (for the answerable-only metric)
    started = time.monotonic()
    for i, row in enumerate(rows, 1):
        r = orch.answer(principal, row["question"])
        routes[r["route"]] = routes.get(r["route"], 0) + 1
        if row["answerable"]:
            answerable_ids.add(r["request_id"])
        print(f"[{i}/{len(rows)}] {row['id']} {r['route']} {r['status']}", flush=True)
    auditor.drain(timeout_s=3600)
    elapsed = time.monotonic() - started

    scored = store.rows("tenant_eval")  # this run's database only
    summary: dict = {}
    for idx, name in enumerate(["faithfulness", "answer_relevancy", "context_precision"], start=1):
        vals = [r[idx] for r in scored if r[idx] is not None]
        summary[name] = {
            "mean": round(statistics.fmean(vals), 4) if vals else None,
            "n": len(vals),
            "gate": GATE[name],
            "meets_gate": (statistics.fmean(vals) >= GATE[name]) if vals else None,
        }

    # Context precision on answerable questions only. Unanswerable questions have no relevant
    # context, so they score 0 and pull the all-questions figure down (see CLAUDE.md §1).
    ans_cp = [r[3] for r in scored if r[0] in answerable_ids and r[3] is not None]
    # Decided by the user: the context precision gate applies to answerable questions only.
    summary["context_precision_answerable_only"] = {
        "mean": round(statistics.fmean(ans_cp), 4) if ans_cp else None,
        "n": len(ans_cp),
        "gate": GATE["context_precision"],
        "meets_gate": (statistics.fmean(ans_cp) >= GATE["context_precision"]) if ans_cp else None,
        "note": "Gate figure (answerable questions only, decided by the user).",
    }
    summary["context_precision"]["note"] = "All scored responses. Unanswerable questions score 0; not the gate figure."

    report = {
        "questions_run": len(rows),
        "routes": routes,
        "judge_model": judge_model,
        "answer_model": judge_model,
        "scored_samples": len(scored),
        "dropped": auditor.dropped,
        "judge_errors": auditor.errors,
        "metrics": summary,
        "elapsed_min": round(elapsed / 60, 2),
        "state_db": str(db.relative_to(ROOT)),
        "questions_scored_note": "Only answered retrieval responses with citations are scored; abstentions are not.",
        "note": ("Synthetic golden set. RAGAS-style metrics from an LLM judge (same model as the "
                 "answer model, so self-judged). Not the ragas library, and not KPI sign-off."),
    }
    report_path = out_dir / f"golden_run_{stamp}_{len(rows)}q.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
