"""Developer workbench (CLAUDE.md §16, Gate 18). Run: streamlit run app.py

Six tabs. Every number comes from code run in this session or from a file in reports/.
The answer path uses the mock LLM, so the workbench makes no paid model calls.
Tabs 2, 4, and 5 use the local knowledge base, guards, and state database.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from assistant import guard  # noqa: E402
from assistant.auth import Principal  # noqa: E402
from assistant.llm import MockLLM  # noqa: E402
from assistant.orchestrator import Corpus, Orchestrator  # noqa: E402
from assistant.retrieval.bm25 import BM25Index  # noqa: E402
from assistant.backends import audit_for, open_state  # noqa: E402
from assistant.triage import classify, route_safely  # noqa: E402

KB = Path(os.environ.get("CORPUS_DIR", ROOT / "knowledge_vault"))
STATE_DB = Path(os.environ.get("STATE_DB", ROOT / "state" / "platform.db"))
REPORTS = ROOT / "reports"

st.set_page_config(page_title="Unreliable Assistant — Workbench", layout="wide")


@st.cache_resource
def _corpus() -> Corpus:
    return Corpus.from_dir(KB)


@st.cache_resource
def _orchestrator() -> Orchestrator:
    return Orchestrator(_corpus(), llm=MockLLM())


def _read_json(path: Path) -> dict | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
    "1 Architecture & Workflow",
    "2 3-Stage RAG Lab",
    "3 MCP & Plugin Inspector",
    "4 Memory & Audit",
    "5 Red Team & Circuit Breaker",
    "6 KPI Scorecard",
])

# ---- Tab 1 -----------------------------------------------------------------
with tab1:
    st.subheader("Gate 2 workflow")
    png = ROOT / "docs" / "workflow_orchestration.png"
    if png.exists():
        st.image(str(png), width="stretch")
    else:
        st.warning("docs/workflow_orchestration.png is missing.")
    st.subheader("Live triage routing console")
    q = st.text_input("Query", "What is the returns policy?", key="t1q")
    if q:
        redacted, kinds = guard.redact(q)
        injected = guard.check_injection(redacted)
        route = "REJECT_OOS" if injected else route_safely(classify(redacted))
        c1, c2, c3 = st.columns(3)
        c1.metric("Route", route)
        c2.metric("PII kinds", ", ".join(kinds) or "none")
        c3.metric("Injection flagged", "yes" if injected else "no")
    st.subheader("ReAct trace (mock LLM)")
    if st.button("Run through the pipeline", key="t1run") and q:
        r = _orchestrator().answer(Principal("tenant_workbench", "dev"), q)
        st.json({k: r[k] for k in ("route", "status", "trace", "citations", "cache_hit")})
        st.write(r["answer"])

# ---- Tab 2 -----------------------------------------------------------------
with tab2:
    st.subheader("Stage 1 BM25 with interactive parameters")
    st.caption("Dense and RRF scores need the bge models; they are shown in the hybrid run, not here.")
    c1, c2, c3 = st.columns(3)
    k1 = c1.slider("k1", 0.1, 3.0, 1.25, 0.05)
    b = c2.slider("b", 0.0, 1.0, 0.75, 0.05)
    threshold = c3.slider("Abstention threshold (reranker scale)", 0.0, 1.0, 0.42, 0.01)
    query = st.text_input("Query", "What is the return window for SKU-001?", key="t2q")
    corpus = _corpus()
    index = BM25Index(corpus.texts, k1=k1, b=b)
    hits = index.search(query, top_k=10) if query.strip() else []
    if hits:
        st.dataframe([{"rank": h.rank, "chunk_id": h.doc_id, "bm25": round(h.score, 4)} for h in hits],
                     width="stretch")
    else:
        st.info("No lexical match. The pipeline would abstain.")
    st.caption(f"Threshold {threshold:.2f} applies to reranker scores; BM25 has no abstention threshold.")

# ---- Tab 3 -----------------------------------------------------------------
with tab3:
    st.subheader("MCP servers (mcp_config.json)")
    cfg = _read_json(ROOT / "mcp_config.json") or {}
    servers = cfg.get("mcpServers", {})
    rows = []
    for name, spec in servers.items():
        if "url" in spec:
            import urllib.error
            import urllib.request

            try:
                urllib.request.urlopen(spec["url"], timeout=2)
                status = "reachable"
            except urllib.error.HTTPError:
                status = "reachable (HTTP error on GET, expected for MCP)"
            except Exception:  # noqa: BLE001
                status = "not running"
        else:
            status = "stdio: starts with the host process"
        rows.append({"server": name, "transport": spec.get("transport"), "status": status})
    st.dataframe(rows, width="stretch")
    st.subheader("Plugins")
    plugins = cfg.get("plugins", [])
    st.dataframe([{"name": p["name"], "stage": p["stage"], "priority": p.get("priority"),
                   "fail_closed": p.get("fail_closed", False),
                   "toggle": "locked (fail-closed)" if p.get("fail_closed") else "toggle in dev only"}
                  for p in plugins], width="stretch")
    st.caption("C22: pii_redactor and injection_shield stay locked in production. Runtime toggles are not built.")

# ---- Tab 4 -----------------------------------------------------------------
with tab4:
    st.subheader("Hash-chained audit log (state/platform.db)")
    if not STATE_DB.exists():
        st.info("No state database yet. Submit a Tier 3 request through the API to create it.")
    else:
        db = open_state()
        audit = audit_for(db)
        broken = audit.verify()
        st.metric("Chain integrity", "intact" if broken is None else f"broken at seq {broken}")
        tenant = st.text_input("Tenant", "tenant_a", key="t4tenant")
        if re.match(r"^tenant_[a-z0-9_-]{1,56}$", tenant):
            st.dataframe([{"seq": r[0], "event": r[1], "payload": r[2], "hash": r[3][:16] + "…"}
                          for r in audit.rows_for_tenant(tenant)], width="stretch")
        else:
            st.warning("Tenant must match ^tenant_[a-z0-9_-]{1,56}$")
    st.caption("Session memory and the HITL queue are read through the state-store server in MCP mode.")

# ---- Tab 5 -----------------------------------------------------------------
with tab5:
    st.subheader("Red-team set through the rule-based guards")
    rt_path = ROOT / "data" / "synthetic" / "redteam_set.jsonl"
    rows = [json.loads(l) for l in rt_path.read_text(encoding="utf-8").splitlines()]
    results: dict[str, list[int]] = {}
    for row in rows:
        redacted, kinds = guard.redact(row["prompt"])
        blocked = bool(kinds) or guard.check_injection(redacted)
        results.setdefault(row["category"], []).append(1 if blocked else 0)
    st.dataframe([{"category": c, "prompts": len(v), "blocked_or_redacted": sum(v),
                   "rate": round(sum(v) / len(v), 3)} for c, v in results.items()], width="stretch")
    total = sum(sum(v) for v in results.values())
    st.caption(f"{total}/{len(rows)} blocked or redacted. Rule-based guards, synthetic set, not KPI-02/03 sign-off "
               "(KPI-02 and KPI-03 need larger benchmark sets, X11).")
    st.subheader("Circuit breaker (CLAUDE.md §8)")
    st.write("Trips after 2 identical failures of (tool, args hash) and halts the loop with HALTED_BREAKER. "
             "Live trip and reset controls are not built (C20 scope is open).")

# ---- Tab 6 -----------------------------------------------------------------
with tab6:
    st.subheader("36-KPI scorecard")
    st.caption("Status is 'measured' only when a report in reports/ holds the value. "
               "Every measured value is synthetic or proxy and is not KPI sign-off.")
    claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    kpis = []
    for m in re.finditer(r"^\| (KPI-\d\d) \| ([^|]+) \| ([^|]+) \| ([^|]+) \|", claude, re.M):
        kpis.append({"id": m.group(1), "stage": m.group(2).strip(), "kpi": m.group(3).strip(),
                     "threshold": m.group(4).strip()})

    measured: dict[str, tuple[str, str]] = {}
    bm = _read_json(REPORTS / "retrieval" / "bm25_eval.json") or {}
    hy = _read_json(REPORTS / "retrieval" / "hybrid_eval.json") or {}
    hh = _read_json(REPORTS / "retrieval" / "hybrid_eval_hard.json") or {}
    if hy:
        measured["KPI-12"] = (f"{hy['after_rerank']['mrr_at_4']} (base, synthetic)", "hybrid_eval.json")
        measured["KPI-13"] = (f"{hy['after_rerank']['ndcg_at_4']} (base, synthetic)", "hybrid_eval.json")
        measured["KPI-23"] = (f"{hy['abstention']['accuracy']} (9 negatives, synthetic)", "hybrid_eval.json")
    if hh:
        measured["KPI-12 (hard)"] = (f"{hh['after_rerank']['mrr_at_4']} (hard, synthetic)", "hybrid_eval_hard.json")
    if bm:
        measured["KPI-07 (BM25 p95 latency)"] = (f"{bm.get('latency_ms_p50_p95', {}).get('bm25', 'n/a')} "
                                                   "(ms p50, p95; BM25 eval)", "bm25_eval.json")
    for name, label, key in [("KPI-20", "faithfulness", "faithfulness"),
                             ("KPI-21", "answer_relevancy", "answer_relevancy"),
                             ("KPI-11", "context_precision", "context_precision")]:
        r = _read_json(REPORTS / "ragas" / "golden_run_20261118_55q.json") or {}
        if r.get("metrics", {}).get(key):
            m = r["metrics"][key]
            measured[name] = (f"{m['mean']} (55q, judge proxy, n={m['n']})", "golden_run_20261118_55q.json")
    rows = []
    for k in kpis:
        value, source = measured.get(k["id"], ("not measured", ""))
        rows.append({**k, "status": "measured (synthetic/proxy)" if source else "not measured",
                     "value": value, "source": source})
    st.metric("KPIs measured here", f"{sum(1 for r in rows if r['source'])} of {len(rows)}")
    st.dataframe(rows, width="stretch")
