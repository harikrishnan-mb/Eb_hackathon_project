# CLAUDE.md — "The Unreliable Assistant" → Enterprise Agentic Platform

Turn the fragile prototype into an MCP-enabled, plugin-extensible agentic assistant that answers from a governed knowledge base, performs tenant-scoped reads and HITL-gated writes, and holds 50,000 concurrent VUs at p95 ≤ 450 ms and p99 ≤ 590 ms (KPI-33), verified against 36 stage-level KPIs.

## 0. Sources of truth and provenance

**Order of authority**
1. **`Unreliable_Assistant_MidCheck_Enterprise_Blueprint_v2.docx`** (the Blueprint): the spec. Gates 0–18, the 36 KPIs, and the runbooks come from here.
2. **`Post_Hardening_Engineering_Audit_Report.docx`** (the Audit): the baseline failures to fix, and **claimed** results. Every Audit number is a claim until its artifact exists (see §18).
3. **`design.md`** (repo root; LOCKED 2026-11-18 with the defaults in its §9, see §1): the design record. It fills gaps the Blueprint leaves open (error codes, per-hop latencies, FMEA, ADRs). It does not override the Blueprint. Changes need a new ADR and a version bump.
4. **`docs/workflow_orchestration.png`** (source `docs/workflow_orchestration.dot`): the Gate 2 diagram.

**Provenance tags** (used in headings and tables)
- **[BP]** stated in the Blueprint.
- **[AU]** stated in the Audit.
- **[DD]** a design-draft proposal, or an inference by the author. Not a requirement until `design.md` is locked and approved.

**Rule for deviations.** Where this file departs from a Blueprint value, the departure is listed in the register in §20 with both values and a status. Until a deviation is approved, the Blueprint value is the default, except where the Blueprint value is infeasible (C5, C6).

Reference convention: "design §N" means section N of the design draft. "§N" alone means a section of this file.

## 1. Current state

- `design.md` is **LOCKED (2026-11-18)** with the defaults recorded in its §9. Unanswered items are marked "default adopted" and can be reopened by ADR.
- Code exists: contracts, error codes, auto-repair, RBAC, harness, bounded loop, triage, cache keys, auth interface, guards (`assistant/guard.py`), LLM adapter, orchestrator (`assistant/orchestrator.py`), and FastAPI app (`assistant/api.py`). The test suite has 87 passing tests (`python -m pytest`).
- API: `uvicorn assistant.api:app` serves `GET /`, `GET /healthz`, `POST /api/v1/agent/query` (dev headers `X-Tenant-ID` / `X-User-ID`), and `POST /api/v1/hitl/decide` (requires `X-Role: reviewer`, a dev stub). Auth is a development stub; the production identity model is open (§6.8, §21 item 9).
- Synthetic data exists: `knowledge_vault/` (38 atomic notes) and `data/synthetic/` (250-question golden set, routing, benign, PII, and red-team sets). Regenerate with `python scripts/generate_synthetic_dataset.py`. It is fictional and labeled `synthetic: true`. The routing and benign sets were written alongside the triage rules, so their scores are not an independent measure of triage quality.
- LLM adapter wired: OpenRouter (`anthropic/claude-sonnet-5.5`, ADR-05). Selected by `LLM_PROVIDER` or the first key set. Keys come from the environment only. Live smoke test passed.
- Stage 1 retrieval built: BM25 (k1 1.25, b 0.75) in `assistant/retrieval/bm25.py`. Measured by `scripts/eval_retrieval.py` into `reports/retrieval/bm25_eval.json`: nDCG@4 0.977, recall@50 1.0 on the synthetic golden set. That set is easy by construction, so it is not a KPI result.
- Hybrid pipeline wired: `assistant/retrieval/pipeline.py` runs BM25 → dense → RRF → cross-encoder with abstention at the threshold. Degradations are flagged in the trace (`dense_unavailable`, `rerank_unavailable`). `RETRIEVAL_MODE=bm25` (default) runs BM25 only. `RETRIEVAL_MODE=hybrid` with `MODELS_DIR` loads both models (about 3.5 GB). Hybrid mode runs through the API on the real weights (`MODELS_DIR=models`). The loop budget is `LOOP_BUDGET_MS` (default 380 ms; the local run used `LOOP_BUDGET_MS=0`, see C24). A halt returns status `degraded`, never an error, and is not cached. On the synthetic LOT query, the reranker scored the correct note at 0.23, below the 0.42 threshold, so hybrid mode abstained where BM25 answered. The threshold is a Gate 3 value and has not been changed. Decision (user): keep 0.42 and accept that hybrid mode abstains more often than BM25. Model weights (BGE-large-en-v1.5, bge-reranker-v2-m3) are in `/tmp/hf/BAAI/`, which is RAM-backed and lost on reboot. The root disk was about 98% full, so the weights were not placed under the repo.
- Hybrid evaluations (`scripts/eval_hybrid.py`) have results: `reports/retrieval/hybrid_eval.json` (55 base questions: MRR@4 0.967, nDCG@4 0.934, false abstention 4.3%, abstention accuracy 1.00 on 9 negatives) and `hybrid_eval_hard.json` (55 hard questions: MRR@4 0.891, nDCG@4 0.920, no unanswerable questions in that subset). Synthetic, not KPI results. Reranker latency is about 15 s per question on this CPU.
- Persistence (local): `assistant/store.py` keeps HITL decisions and a SHA-256 hash-chained audit log in SQLite (`state/platform.db`, or `STATE_DB`). Every query filters on `tenant_id`. The Tier 3 request and decision events are audited, and `AuditLog.verify()` detects tampering. This is the SQLite local scope proposed in C10. Postgres and Redis are not built.
- ONNX reranker: `scripts/export_reranker_onnx.py` exports an FP16 graph to `models/bge-reranker-v2-m3/onnx/model_fp16.onnx`. It matches PyTorch closely (max score difference 0.0001 on 12 pairs). On this CPU it is not faster than PyTorch (`reports/retrieval/reranker_fp32_bench.json`), and the reranker takes about 15 s per query, so the KPI-08 and KPI-09 latency budgets are not met.
- MCP servers built (`mcp_servers/`, SDK `mcp<2`): `rag_server.py` (stdio, `hybrid_retrieve`, validated against `HybridRetrieveInput`), `graphify_server.py` (stdio, `graph_subgraph`, built-in wikilink graph, not the Graphify CLI), and `state_store_server.py` (Streamable HTTP at `localhost:8081/mcp`: `session_facts_put/get`, `hitl_request`, `hitl_decide`, `audit_append`). `scripts/mcp_smoke.py` calls all of them through an MCP client and passes. `mcp_config.json` launches the stdio servers with `uv run`, which is not installed here, so use `python mcp_servers/...` instead. The harness is connected to them in MCP mode (`MCP_MODE=1`, `assistant/mcp_bridge.py`): `hybrid_retrieve` and `hitl_request` go to the rag and state-store servers, the Tier 3 gate reads `hitl_status`, and audit writes go to `audit_append`. The harness still validates, repairs, checks RBAC, and enforces idempotency before each call. Run it with the state server started first (`python mcp_servers/state_store_server.py`). Cold start: the RAG server loads its models in a background thread at launch, and the API waits for its `ready` tool at startup (`RAG_READY_TIMEOUT_S`, default 900 s), so the API reports healthy only after the models are loaded. The first query after a restart no longer times out (measured: healthy after about 35 s; first query 18.7 s). Known gap: the harness blocks Tier 3 calls before the handler runs, so the orchestrator writes the pending-request audit entry itself (through the state server, not the harness). The graph tool is not used by the orchestrator yet.
- RAGAS-style auditor built (`assistant/ragas_auditor.py`): faithfulness, answer relevancy, and context precision, judged by an LLM (the same OpenRouter model as the answers, so it is self-judged). It is **not the ragas library**, which is not installed because of disk space, and its numbers are not comparable to published RAGAS values. Context entity recall and context recall are not implemented. It runs asynchronously (C21 is still open): the request path only enqueues, and scores go to `ragas_scores` in SQLite. Enabled by `scripts/run_ragas_golden.py`, which is not wired into the API. Smoke run (5 golden questions, `reports/ragas/golden_run_20261118_5q.json`): faithfulness 1.00, answer relevancy 0.97, context precision 0.30 with my first formula. That was a bug: it used the plain fraction of relevant contexts, while RAGAS uses rank-weighted average precision. With the corrected formula, the 55-question run (`reports/ragas/golden_run_20261118_55q.json`, 37 scored) gives faithfulness 1.00, answer relevancy 0.92, and context precision 0.874, just below the 0.88 gate on this small n. The earlier report is kept as `..._SUPERSEDED_fraction_metric.json`. Full 250-question run (`reports/ragas/golden_run_20261124_250q.json`, 238 scored, every answered response audited including cache hits, answerable-only gate): faithfulness 1.000 (gate 0.92, met), answer relevancy 0.907 (gate 0.90, met, narrowly), context precision on answerable questions 0.969 (n=213, gate 0.88, met). All-questions context precision 0.867 (n=238, not the gate figure). 12 responses were not scored because they had no citations. Investigation: the 238 scored responses are 213 answerable questions plus 25 unanswerable questions that still returned citations. Unanswerable questions have no relevant context, so they score zero. Label-based rank-weighted precision (gold notes, no judge) is 0.969 on the answerable questions, and the judge agreed with the labels on 43 of 44 sampled verdicts. The 0.866 is therefore a metric-definition effect, not a retrieval or judge failure. Whether unanswerable questions belong in this gate is a decision (not yet made); both figures are reported. The earlier 250-question report was superseded because cache hits were not audited.
- Streamlit workbench `app.py` built (six tabs; run with `streamlit run app.py`). Tabs 2–6 are read-only views and rule-based checks, not the full live controls in §16. Tab 5's red-team figures are rule-based guard results, not KPI-02/03 sign-off.
- Postgres and Redis are wired into the app (`assistant/pgstore.py`, `assistant/cachestore.py`, `assistant/backends.py`). Set `STATE_DATABASE_URL` to use Postgres for the HITL queue, audit log, session facts, and RAGAS scores; otherwise SQLite. Set `REDIS_URL` to use Redis for the response cache (TTL 300 s +/- 45 s, fails open). The API, state-store server, workbench, and RAGAS runner all use these backends. Verified: 114 tests pass with the services (`STATE_DATABASE_URL_TEST`, `REDIS_URL_TEST`; live tests use database `ua_test` and Redis DB 15), and a Tier 3 request was approved through the API and recorded in Postgres with reviewer `rev-1`. The bridge calls the state-store server through a fresh session per request, so restarting that server no longer needs an API restart (verified: a Tier 3 approval succeeded after a state-server restart with the API still running). The RAG server is still a persistent stdio child; restarting the API reloads its models.
- Docker services running (localhost only): `ua-postgres` (postgres:16-alpine, port 5432, db `ua`, user `ua`, volume `ua_pgdata`) and `ua-redis` (redis:7-alpine, port 6379, password auth, AOF on, volume `ua_redisdata`). Credentials are in `docker/.env` (mode 600, not in code). Both were verified with authenticated connections. The app is not yet connected to them: the HITL store, audit log, and session facts still use SQLite (`state/platform.db`), and the cache is in-process. Postgres and Redis backends are the next build step.
- Red-team runner through the API is built (`scripts/run_redteam_api.py`). Latest run (after the jailbreak and injection rules): 100/100 blocked, 0 leaked. Contaminated: the rules were written after reading the missed prompts, so this shows the two templates are now caught, not that new attacks are.
- Held-out red-team set (`data/heldout/redteam_heldout.jsonl`, 100 prompts, hash in `MANIFEST.json`, frozen before measurement). Guards acted on 28/100 (direct jailbreak 0/20, indirect injection 3/20, SQLi and overflow 12/20, crescendo 1/20, PII smuggling 12/20). The earlier 100/100 was contaminated, so the rules generalise poorly. Rules were not tuned after this measurement (`reports/redteam/heldout_offline_20261125.json`).
- Guard rules v2 (`assistant/guard.py`) cover instruction override, extraction (including encoded and translated requests), crescendo follow-ups, embedded instructions in documents, broader SQL injection, and obfuscated PII. Benign check: 0 false positives on 120 queries. Measured on `data/heldout/validation_v3.jsonl` (60 prompts, written after the rules were frozen): 39/60 (direct jailbreak 2/12, indirect 6/12, SQLi 10/12, crescendo 9/12, PII 12/12). The earlier held-out set now reads 82/100, but it is contaminated, since the rules were written after reading its misses. Summary: `reports/redteam/rules_v2_summary_20261125.json`.
- Solvable gaps closed in this pass (CLAUDE.md §4, §13, Gates 5, 7, 9, 10, 11, 12, 13, 16):
  - Spans with Gate 10 canonical names (`assistant/obs.py`; console exporter with `OTEL_TRACES_EXPORTER=console`; no collector running here).
  - Prometheus metrics at `GET /metrics` (requests, latency histogram, guard blocks, cache, bulkhead refusals, unsupported claims). Alert rules in `ops/alerts.yml` (runbook thresholds plus the C8 proposal). PromQL not checked with promtool (not installed); YAML parses.
  - Bulkheads: rag 64, llm 256 applied; db 128 defined but no synchronous DB call is wrapped yet. A full pool fails fast (rag: degraded answer; llm: 503).
  - Verification (Gate 7 proxy): `assistant/verifier.py` removes answer sentences with less than 60% content-word support in the cited chunks, and abstains if nothing remains. Lexical, not NLI; a live answer lost one sentence, so false removals are possible and not measured.
  - Observation compression (Gate 5): chunks capped at 180 words in the prompt (approximate; no tokenizer).
  - KPI-02 on `data/synthetic/pii_set.jsonl`: regex redaction recall 0.80; pipeline recall (redacted or blocked) 1.00. The phone pattern was added after this set showed the gap, so the pipeline figure is biased. `reports/pii_kpi02_20261125.json`.
  - KPI-22 lexical entity recall on the golden set: 1.00 (`reports/retrieval/entity_recall_golden.json`), which means this set cannot tell good from poor retrieval.
  - KPI scorecard (`scripts/kpi_scorecard.py` -> `reports/kpi_scorecard.json`): 36 KPIs, 13 measured, 9 passing. Tests fail if a measured KPI has no source file.
  - Topology diagram for Gate 1.3: `docs/topology.dot` and `docs/topology.png`, drawn from the running architecture.
  - KPI-36 soak script `load_tests/k6_soak.js` (SOAK_MINUTES, default 60). Written and syntax-checked with a 15 s run; the 1-hour soak has not been run.
- Still not built, and not solvable here: distilled triage classifier (needs labelled training data, and the synthetic routing set is circular), horizontal scale (no Compose plugin; Docker Compose not installed), Triton or ONNX microservice, OTel collector, the Graphify runtime use, shared circuit-breaker state (C20 decision), live workbench controls that depend on C20 and C22, and the full 1-hour soak run.
- Not built yet: load tests at 50K scale.
- Not a git repository.
- Exists: the two source docx files, `docs/workflow_orchestration.png` and its `.dot` source, this file, `design.md`, `pyproject.toml`, `mcp_config.json`, `assistant/` (platform core), and `tests/`.
- Load tests written (`load_tests/`): `k6_50k_load.js` (Blueprint stages, thresholds 240/450/590 ms, `BASE_URL` env, `SMOKE=1` short profile) and `jmeter_50k_plan.jmx` (properties `host`, `port`, `threads`, `ramp`, `duration`, `resp_timeout`; tenants from `tenants.csv`). Smoke runs only (`reports/` not written; results in `load_tests/results_*`): k6 2 VUs x 20 s, 4 iterations, 0 HTTP failures, p95 14.2 s (threshold 450 ms, not met). JMeter 2 threads x 30 s, 5 requests: 3 fast (3-38 ms, most likely cache hits, since each thread keeps one user and repeats the same query) and 2 at 32-33 s on the retrieval path (fails the 600 ms duration assertion). Fix applied: both tests now draw varied golden-set questions (`load_tests/queries.json`, `queries.csv`, 50 questions) per request, so repeated queries no longer inflate the results. Re-run smoke: k6 2 requests, both about 17.6 s (no cache hits); JMeter 5 requests, 4 over the 600 ms assertion (about 25-33 s) and 1 fast (4 ms). These are not KPI results. A 50K run needs distributed injectors, and the JMeter plan has no throughput timer (X6) and no faithfulness assertion (the API does not return faithfulness; RAGAS is async, C21).
- Still to create:
  - red-team runner through the API (the 100-prompt set exists in `data/synthetic/redteam_set.jsonl`)
  - `reports/` evidence artifacts (§18.5): partly present (`reports/retrieval/`, `reports/ragas/`)

## 2. Hard rules

1. **Gate 1 before code [BP].** `design.md` is locked (2026-11-18) with seven sections (1.1 charter, 1.2 MCP/data contracts, 1.3 topology, 1.4 latency budget, 1.5 KPI matrix, 1.6 FMEA of 12 rows, 1.7 ADRs). Changes to a locked decision require a new ADR and a version bump.
2. **Gate 2 before app logic [BP].** `docs/workflow_orchestration.png` must exist first. A draft exists; see §4.
3. **Non-goals are enforced, not advisory.**
   - No autonomous clinical, financial, or legal execution without HITL [BP, Gate 1.1].
   - No Tier 3 action without a `PENDING_APPROVAL` → approved transition [BP, Gate 9.4]. No bypass flag [DD].
   - Zero raw PII persistence [BP, Gate 13]. Redacted text only in logs, cache, traces, prompts, and `session_facts` [DD extends].
   - Execution subagent has no RAG or external web access [BP]. Retrieval is read-only [BP]; "no web" for Retrieval is [DD].
   - Every request ends in `DONE` or `HALTED_*` [BP, Gate 4].
   - Out-of-scope queries get `REJECT_OOS` [BP, Gate 6].
4. **Tenant isolation [BP, Gate 13].** `tenant_id` in every database query and every Redis key. A repository layer and a failing test for any query that omits it are [DD]. KPI-28 (0 cross-tenant leaks) is a release blocker.
5. **Fail closed [BP].** `pii_redactor` and `injection_shield` are `fail_closed: true`. The 503 with `Retry-After` response is [DD].
6. **No fabricated results.** Never write a KPI, latency, or RAGAS number into docs or code unless a run produced it. If a check can't run here, say so.

## 3. Charter and personas [DD]

The Blueprint requires a charter, non-goals, and personas (Gate 1.1) but supplies none. The draft proposes:

**Purpose:** answer enterprise questions from a governed knowledge base, perform tenant-scoped reads and HITL-gated writes through tools, and sustain 50,000 concurrent VUs inside the latency SLA.

| Persona | Needs | Primary surface |
|---|---|---|
| End user (employee or customer) | Grounded, cited answers; fast responses | `POST /api/v1/agent/query` |
| Approver / reviewer | Approve or reject Tier 3 actions | HITL queue (API, Streamlit Tab 1) |
| Operator / SRE | Health, breakers, runbooks, traces | Grafana; Streamlit Tabs 3, 5, 6 |
| Compliance auditor | Tamper-evident audit trail, tenant evidence | Audit log export; `session_facts` |
| Knowledge owner | Keep policies current and linked | Obsidian vault (`knowledge_vault/`) |
| Developer | Build and verify against KPIs | CI; Streamlit Tabs 2, 4 |

## 4. Architecture

**Request path [BP, Gates 2 and 4]**

`Ingress → pii_redactor → injection_shield → Redis semantic cache check → Triage Agent → Subagent → ToolExecutionHarness → MCP server → Bounded ReAct loop → Circuit Breaker → Tier 3 HITL Gate → Post-Execution RAGAS & OTel export → response`

**Rules**
- API workers are stateless; all state lives in Redis or Postgres [BP Gate 15, DD].
- Bulkheads per MCP server [BP, Gate 9]: `rag_pool=64`, `db_pool=128`, `llm_pool=256`. A slow reranker must not starve health checks or triage.
- **OTel export** is off the request path [DD].
- **RAGAS scoring is asynchronous (decision, C21, user-approved).** The request path only enqueues a sample; scores are computed off the path and stored (`ragas_scores`). Enabled with `RAGAS_ENABLED=1` (off by default, since each answered retrieval response costs about 4-6 judge calls). Verified: one live query returned at once, and its scores (faithfulness 1.00, answer relevancy 0.93, context precision 1.00) appeared in Postgres `ragas_scores` about a minute later. Consequences: the API does not return `faithfulness`, so the JMeter `$.faithfulness` assertion (Blueprint) cannot be checked on the response; the Gate 11 faithfulness alert must be computed from the score store. Earlier text, kept for the record: **RAGAS scoring: synchronous or asynchronous was undecided (C21).** The Blueprint places "Post-Execution RAGAS" in the egress chain (Gate 2), gives `ragas_auditor` a `min_faithfulness: 0.92` setting, asserts `$.faithfulness >= 0.92` on the HTTP response in the JMeter plan, and has the Verification subagent "veto ungrounded drafts". None of those says "async". The draft's latency budget assumes it is off-path. See X1.
- `mcp-hybrid-rag` and `mcp-graphify-vault` run as long-lived stdio processes [DD]. Per-call spawn cannot meet the 150 ms timeout or KPI-30. See X10 for the cost of stdio.
- Horizontal scale [BP, Gate 15]: stateless FastAPI behind NGINX/Envoy; Redis Cluster; ONNX bi-encoder and cross-encoder workers with micro-batching (batch 32, max wait 8 ms); vLLM or TensorRT-LLM with continuous batching and prefix caching.

**Stack [BP, Gate 0]:** Python 3.11; FastAPI + Uvicorn (async); `mcp.ClientSession` host; MCP over JSON-RPC 2.0 (stdio for local workers, Streamable HTTP/SSE for distributed services); Rank-BM25; FAISS; BGE-Large; ONNX Runtime (reranker, Triton or ONNX microservice); PostgreSQL + pgvector; Redis Cluster; Obsidian + Graphify; Pluggy hooks. Pydantic v2 for tool contracts [BP, Gate 4].

### Gate 2 diagram requirements [BP]

`docs/workflow_orchestration.png`: 300 DPI, Graphviz (`diagrams` / `graphviz` Python library), displayed in Streamlit Tab 1. It must explicitly show:
1. **Ingress and security:** Ingress → Pre-Execution Plugins (PII Redactor → Injection Classifier) → Redis semantic cache check.
2. **Triage and routing:** Triage Agent (`temperature=0.0`, "< 30 ms" in Gate 2; KPI-05 says ≤ 25 ms) branching into Direct Response, Retrieval Subagent, Execution Subagent, or Audit Subagent.
3. **MCP and 3-stage RAG:** JSON-RPC calls to `mcp-hybrid-rag` (BM25 + bi-encoder → RRF → cross-encoder), `mcp-graphify-vault`, and `mcp-state-store`.
4. **Control plane and egress:** Bounded ReAct loop (`max_steps=5`) → Circuit Breaker → Tier 3 HITL Gate → Post-Execution RAGAS & OTel export.

**Status:** the current PNG follows that chain literally: triage has four branches (Direct, Retrieval, Execution, Audit/Verification), plus a separate `REJECT_OOS` exit, and the chain runs ReAct → Circuit Breaker → HITL → Post-Execution. It was built with the `dot` CLI, not the Python libraries the Blueprint names; the output is the same. Open items are marked on the diagram (C5, C11, C21, C2, X3). Regenerate after those are decided. The Streamlit Tab 1 rendering does not exist yet.

**Gap [BP, Gate 1.3]:** Gate 1.3 asks for a complete topology diagram showing the API Gateway, Redis cache, MCP servers, ONNX reranker, and PostgreSQL. The current PNG is the Gate 2 workflow. It shows Postgres only inside the state-store box and has no ONNX reranker node, so the Gate 1.3 topology must be drawn for `design.md`.

## 5. MCP servers and plugins

| Server | Transport | Tools / resources | Tier ceiling |
|---|---|---|---|
| `mcp-hybrid-rag` [BP] | stdio | `hybrid_retrieve` tool; `rag://chunks/{id}` resources | TIER_1_READ [DD] |
| `mcp-graphify-vault` [BP] | stdio | `graph_subgraph` [DD name]; `vault://notes/{id}` [DD] | TIER_1_READ [DD] |
| `mcp-state-store` [BP] | Streamable HTTP, `pool_size` 200 | `session_facts_get/put`, `workflow_transition`, `hitl_request`, `hitl_decide`, `audit_append` [DD names] | TIER_3_MUTATE (harness-approved calls only) [DD] |

**Subagent tier ceilings [DD]**
- Retrieval: `TIER_1_READ` [BP: read-only, no state-mutating tools].
- Execution: up to `TIER_3_MUTATE`, only through the HITL gate [BP: Tier 3 triggers HITL freeze].
- Verification: `audit_append` only [BP: audit log only]. That is an append-only write, and the sources assign it no tier.
- **`TIER_2_COMPUTE` [BP, Gate 4] is named but no tool uses it and the sources never define it.** Define it or remove it.

**`mcp_config.json` [BP, Gate 0]**
- `hybrid-rag`: `uv run mcp_servers/rag_server.py`, transport `stdio`, `timeout_ms: 150`.
- `state-store`: `http://localhost:8081/mcp`, transport `streamable-http`, `pool_size: 200`.
- `graphify-vault`: `uv run mcp_servers/graphify_server.py`, transport `stdio`.
- Config keys are the short names. Prose uses the `mcp-` prefix.

**Plugins [BP]**
- `pii_redactor`: PRE_EXECUTION, priority 10, `fail_closed: true`.
- `injection_shield`: PRE_EXECUTION, priority 20, `fail_closed: true`.
- `circuit_breaker`: TOOL_DISPATCH, priority 10, `max_identical_calls: 2`.
- `ragas_auditor`: POST_EXECUTION, priority 10, `min_faithfulness: 0.92`.
- `otel_exporter`: ALL_SPANS, endpoint `http://localhost:4317` (dev).
- **HITL:** the Gate 0 table lists "Pre-Hook, Post-Hook, PII, OTel, **HITL** Plugins", but `mcp_config.json` has no HITL entry. Gate 9.4 specifies the HITL gate as Tier 3 behavior. Add a `hitl_gate` plugin entry, or document that it is built in [DD].
- Plugins are "hot-swappable deterministic interceptors" [BP]. Tab 3 toggles: see C22.

## 6. Tool contracts

The Blueprint requires "exact JSON-RPC / MCP tool schemas, Pydantic v2 models, regex constraints, and error codes" (Gate 1.2) and gives only the entity examples in §6.5. Everything else in this section is **[DD]**.

### 6.1 `hybrid_retrieve` [name BP; schema DD]

**Input** (`additionalProperties: false`)

| Field | Type and constraint | Default |
|---|---|---|
| `request_id` | UUIDv4 | required |
| `tenant_id` | `^tenant_[a-z0-9_-]{1,56}$` | required |
| `query` | string, 3–512 chars | required |
| `top_k` | integer 1–50 | 50 |
| `rerank_k` | integer 1–4 | 4 |
| `threshold` | number 0–1 | 0.42 |

**Output**
- `abstain`: boolean.
- `chunks`: array, max 4. Each has `chunk_id`, `chunk_hash` (SHA-256 hex), `text` (≤ 4096 chars), `bm25`, `cosine`, `rrf_rank` (1–40), `rerank_score` (0–1).
- `stage_ms`: object with `bm25`, `biencoder`, `rrf`, `rerank`.

### 6.2 `graph_subgraph`

- Input: `anchor_id` (`^[A-Z]{2,4}-[0-9]{3,6}$` or `^note_[a-z0-9_-]+$`), `depth` 0–2 [BP: at most 2 edges], `max_tokens` 1–320.
- Output: `nodes`, `edges`, `token_estimate`.

### 6.3 `session_facts_put`

- Input: `request_id`, `tenant_id`, `session_id` (UUIDv4), `fact_key` (`^[a-z_]{3,64}$`), `fact_value` (≤ 1024 chars), `source_chunk_hash`.
- Writes only verified facts [BP, Gate 5].

### 6.4 Tier 3: `workflow_transition` and `hitl_request`

- Input: `request_id`, `idempotency_key` (UUIDv4 [BP]), `tenant_id`, `action` (`^[a-z_]{3,48}$`), `args`, `risk_tier: "TIER_3_MUTATE"`.
- No approved `hitl_decide` record → rejected with `E_HITL_PENDING`.

### 6.5 Entity formats [BP examples; patterns DD]

- SKU: `^SKU-[0-9]{3}$`. Repair `SKU99` → `SKU-099`.
- LOT: `^LOT-[0-9]{5}$`. Repair `lot 44` → `LOT-00044`.

### 6.6 JSON-RPC error codes [DD]

| Code | Name | Client action |
|---|---|---|
| -32600 | `E_INVALID_REQUEST` | Fix client |
| -32602 | `E_VALIDATION` | Return structured remediation prompt |
| -32001 | `E_TENANT_FORBIDDEN` | Deny; log security event |
| -32002 | `E_TIMEOUT` | Retry only if budget remains |
| -32003 | `E_BREAKER_OPEN` | Show remediation; do not retry |
| -32004 | `E_HITL_PENDING` | Show pending status |
| -32005 | `E_RBAC_DENIED` | Deny |
| -32006 | `E_IDEMPOTENT_REPLAY` | Same key, different args: deny; log |
| -32007 | `E_DEGRADED` | Serve fallback; flag response |

### 6.7 ToolExecutionHarness [BP, Gate 4; every MCP call, in order]

1. **Strict validation:** Pydantic v2 (`strict=True`, `extra="forbid"` are [DD] settings).
2. **Deterministic regex auto-repair.** No LLM [DD]; at most one pass, logging the original value [DD].
3. **RBAC tier check:** `TIER_1_READ` / `TIER_2_COMPUTE` / `TIER_3_MUTATE`.
4. **UUIDv4 idempotency enforcement.** Replay semantics (same args → stored result; different args → `E_IDEMPOTENT_REPLAY`) are [DD].

### 6.8 Public API contract [DD, inferred from the k6 and JMeter plans]

The Blueprint names the endpoint (`POST /api/v1/agent/query`) but never defines it. The load plans imply:
- **Request:** JSON body `{tenant_id, user_id, query}`; header `X-Tenant-ID`; `Content-Type: application/json`.
- **Response:** must contain `tenant_id` (the k6 `no cross-tenant leak` check) and `faithfulness` (the JMeter JSONPath assertion). The k6 query asks to "return triage status", so a triage status field is also implied. Answer text and citations are expected but not specified.
- **Authentication and authorization are not specified anywhere.** The tenant must come from verified credentials, not from the client-supplied body or header. A mismatch returns `E_TENANT_FORBIDDEN`. Reviewer identity for the HITL queue is likewise undefined. Decide this before locking (§21).

## 7. Latency budget

[BP] targets: p50 ≤ 240 (KPI-31), p95 ≤ 450 (KPI-32), p99 ≤ 590 (KPI-33, "< 600"), TTFT p95 ≤ 180 (KPI-25). The Blueprint's range is "200–600 ms"; the 200 ms lower bound is not checked by any gate.

**Nominal HYBRID_RAG path, p95 [DD table; basis shown]**

| # | Hop | ms | Basis |
|---|---|---:|---|
| 1 | Ingress: gateway, TLS, auth, JSON parse | 10 | Assumption [DD] |
| 2 | `hook.pre` (PII + injection) | 15 | KPI-01 [BP] |
| 3 | `cache.lookup` (Redis) | 2 | Gate 0 says "sub-millisecond"; 2 ms is [DD] |
| 4 | `triage` | 25 | KPI-05 [BP] |
| 5 | `rag.bm25` | 12 | KPI-07 [BP] |
| 6 | `rag.dense` (FAISS HNSW) | 25 | KPI-08 [BP] |
| 7 | RRF | 3 | Gate 3 [BP] |
| 8 | `rag.rerank` | 45 | KPI-09 [BP] |
| 9 | MCP serialization, 3 calls × 4 ms | 12 | KPI-30 [BP] |
| 10 | `graph_subgraph` | 15 | Assumption [DD] |
| 11 | `session_facts` read | 5 | Assumption [DD] |
| 12 | `llm.generate` (TTFT ≤ 180 + stream) | 250 | KPI-25 for TTFT [BP]; 250 is [DD] |
| 13 | Egress | 10 | Assumption [DD] |
| | **Nominal total** | **429** | 21 ms margin to 450 |

- Hops 5–12 are the bounded loop: nominal 367 ms. **Loop ceiling: Blueprint 450 ms; proposed 380 ms (C5).** With 450 ms the worst case is 10 + 15 + 2 + 25 + 450 + 10 = **512 ms, over the 450 ms p95 gate**. With 380 ms it is 442 ms.
- The 120 ms `per_step_timeout` [BP] cannot contain a 250 ms LLM call, so the draft adds a separate 250 ms LLM step timeout (C6) [DD].
- **Missing from the table:** a Verification (NLI) hop and a synchronous RAGAS hop, if either is on the request path (X1). Per-hop p95s are shown but **p99 is not demonstrated**, which Gate 1.4 asks for. Summing per-hop p95s gives a conservative p95 bound only.
- BM25 and dense retrieval may run in parallel (saves up to 12 ms). The budget does not assume it.

**Other routes [DD]**

| Route | Nominal p95 | Notes |
|---|---:|---|
| `DIRECT_ANSWER` | ≈ 60 ms + LLM | No retrieval hops |
| `HYBRID_RAG`, abstention | ≤ 160 ms | Gate 9.3 says the abstention response is "< 90 ms"; clock start undefined (X3) |
| `TRANSACTION_EXEC` | Read path + HITL | Pending HITL returns `E_HITL_PENDING`; not a latency failure |
| `REJECT_OOS` | ≤ 40 ms | No LLM call |

**Capacity (Little's law) [DD]:** at 8,500 RPS (KPI-35) and a 35% hit ratio (KPI-27), ≈ 5,525 LLM requests/s × 0.25 s ≈ **1,380 generations in flight fleet-wide**. This depends on the request rate, which the load plans contradict (X6). Unverified.

## 8. ReAct loop, circuit breaker, and context

**Loop [BP, Gate 4]:** state machine `INIT → TRIAGE → REASONING → TOOL_CALL → OBSERVATION → VERIFY → DONE/HALTED`. `max_iterations = 5` (Gate 2 calls it `max_steps=5`), `per_step_timeout = 120 ms`, `total_loop_budget = 450 ms` (proposed 380, C5). Zero recursive self-calls without state mutation. KPI-17: mean ≤ 2.2 steps per task.

**Circuit breaker [BP, Gate 9.1]:** trips when `(tool_name, canonical_args_hash)` fails ≥ 2 times → `HALTED_BREAKER`, with a structured user remediation prompt. **Scope is unspecified (C20).** The draft says "within a request", but Streamlit Tab 5 has live trip and reset controls, which implies state that outlives one request. Suggested default [DD]: a per-request counter for loop escape (KPI-16), plus a tenant-scoped shared breaker state that Tab 5 can reset. Decide in `design.md`.

**Context [BP, Gate 5]**
1. Immutable `GoalAnchorBlock` (original user intent, normalized entity IDs, active policy constraints) at index 0 of every ReAct prompt.
2. Extractive compressor: strip boilerplate JSON keys, truncate each observation to ≤ 180 tokens.
3. Keep the last 3 ReAct turns in hot Redis/RAM. Persist verified facts in PostgreSQL `session_facts`, keyed by `session_id`.

**LLM 429 [DD]:** one jittered retry only if ≥ 150 ms of loop budget remains; otherwise return an extractive answer with `E_DEGRADED`.

**Triage Agent [BP, Gates 6 and 7]**
- Distilled classifier, ≤ 25 ms inference, `temperature = 0.0`.
- Tags `(intent, domain, risk_tier, complexity)` and routes to `DIRECT_ANSWER`, `HYBRID_RAG`, `TRANSACTION_EXEC`, or `REJECT_OOS`.
- Low confidence → `HYBRID_RAG`, never `TRANSACTION_EXEC` [DD].

**Subagents [BP, Gate 7]**

| Subagent | Core responsibility | Bound MCP servers | Isolation and guardrail rule |
|---|---|---|---|
| Retrieval | Query rewriting, 3-stage hybrid RAG, citation grounding | `mcp-hybrid-rag`, `mcp-graphify-vault` | Read-only; cannot invoke state-mutating tools |
| Execution | Schema validation, form submission, workflow transitions | `mcp-state-store` | No RAG or external web; Tier 3 triggers HITL freeze |
| Verification | Claim-to-chunk NLI check, RAGAS triad audit, OTel logging | `mcp-state-store` (audit log only) | Independent model context; vetoes ungrounded drafts |

Gate 2 draws a fourth triage branch called "Audit Subagent". Treating it as the Verification subagent is an **assumption** (C7, C23): the four triage labels do not map onto the four boxes in Gate 2, and the Audit report corroborates the name "Verification".

**Reliability patterns [BP, Gate 9]**
- **Bulkhead isolation:** `rag_pool=64`, `db_pool=128`, `llm_pool=256`.
- **Deterministic abstention short-circuit:** cross-encoder top score < 0.42 → bypass the LLM and emit a deterministic abstention in "< 90 ms" (zero hallucination risk, ~80% token savings on those queries).
- **Asynchronous HITL gate:** Tier 3 writes a `PENDING_APPROVAL` record to Postgres and pauses until an authorized reviewer approves via UI or API.

## 9. Retrieval pipeline and RAGAS evaluation [BP, Gates 3 and 8]

| Stage | Engine and settings | Budget | Output |
|---|---|---|---|
| 1 Sparse | BM25Okapi + PorterStemmer + inverted index; k1 = 1.25, b = 0.75, top_k = 50, pre-tokenized | < 12 ms | 50 lexical candidates + raw BM25 scores |
| 2 Dense | bge-large-en-v1.5 ONNX + FAISS HNSW; chunk 512 tok, overlap 64; efSearch = 64; top_k = 50 | < 25 ms | 50 semantic candidates + cosine |
| Fusion | RRF, smoothing k = 60, dedup by `chunk_hash`, pool = 40 | < 3 ms | Top 40 unified, scale-normalized |
| 3 Cross-encoder | bge-reranker-v2-m3 ONNX FP16, batch = 32; rerank_k = 4; sigmoid > 0.42; max_len 512 | < 45 ms | Top 4 verified chunks + confidence |

All three stages are implemented in `mcp_servers/rag_server.py` [BP]. Gate 0 mentions INT8/FP16 quantization for the reranker; the draft's "INT8 only if KPI-09 fails and KPI-20 / KPI-11 still pass" policy is [DD].

**Degradations [DD]:** reranker timeout → RRF top-4 flagged `rerank_unavailable` with extractive-quote mode and no abstention short-circuit. FAISS missing → BM25-only, flagged `dense_unavailable`; KPI-10 is expected to fail then.

**Sizing risks (X4):** pool 40 with batch 32 means two reranker passes inside a 45 ms budget, and a 512-token chunk plus the query exceeds `max_len` 512, so pairs will be truncated.

### 9.1 RAGAS evaluation (Gate 8, "Step 8: RAGAS Continuous Evaluation") [BP]

The "Step 6", "Step 8", and "Step 10" labels in the Blueprint trail the paragraphs that hold their content (checked in a LibreOffice text conversion). Nothing is missing (C15 closed).

- Automated RAGAS scoring over a **250-question golden dataset**: Factoid 25%, Complex Reasoning 25%, Multi-Hop 20%, Summary 15%, Negative/Unanswerable 15%.
- **Integer counts [DD]:** 15% of 250 is 37.5, so round: Factoid 63, Complex 62, Multi-Hop 50, Summary 38, Negative 37 (total 250).
- **Release gates:** Faithfulness ≥ 0.92; Answer Relevancy ≥ 0.90; Context Precision ≥ 0.88 (decided: measured on answerable questions only; the all-questions figure is reported as well, since unanswerable questions have no relevant context); Context Recall ≥ 0.85; Context Entity Recall ≥ 0.85.
- **Mapping to KPIs:** Faithfulness = KPI-20, Answer Relevancy = KPI-21, Context Entity Recall = KPI-22, Context Precision ≈ KPI-11. Context Recall ≥ 0.85 has no KPI row. KPI-10 is a different measure (hybrid recall@50 ≥ 0.92).
- Run in CI and show in Streamlit Tab 6 [DD].
- **Production sampling [DD] (D5):** the Gate 11 faithfulness alert (< 0.90 over 5 min) and Gate 8's "Continuous Evaluation" both need RAGAS scoring of live production traffic, not only the 250-question CI run. The sampling rate and sampled set are not specified in either source. Whether scoring is synchronous or asynchronous depends on C21.

## 10. Cache

- **Key [BP, Gate 11]:** `SHA256(tenant_id:user_id:norm_query)` prefix + jittered TTL (300 s ± 45 s). The Audit writes the key with the raw `query`. **`norm_query` is undefined in both sources.** Define it (suggested [DD]: Unicode NFKC fold, case-fold, whitespace collapse). It drives KPI-27 and overlaps the homoglyph defense.
- **"Semantic" cache vs. an exact hash (X2):** an exact SHA-256 key cannot do cosine matching, yet the Gate 11 mitigation says "aggressive semantic cache (cosine ≥ 0.92)". A semantic lookup needs a query embedding (not in the 2 ms `cache.lookup` budget) and a vector namespace per tenant or per user.
- **Hit ratio vs. key (C11):** per-user keys prevent cross-user reads, but KPI-27 (≥ 35%) is then only reachable through repeat queries by the same user. A tenant-scoped key with a per-user ACL on cached responses would raise hits at the cost of relying on that ACL. **Unresolved.** Do not change the key without an ADR.
- **Redis down [DD]:** bypass the cache and emit `cache_degraded`.
- **Load tests:** the k6 script keys users as `user_${vuId}`, which yields near-zero hits by construction. Use a realistic repeat-query distribution to measure KPI-27.

## 11. Failure modes (FMEA) [DD; Blueprint Gate 1.6 requires 12 rows]

| # | Failure | Detection | Deterministic fallback | Residual risk |
|---|---|---|---|---|
| 1 | Redis down or timeout | `cache.lookup` > 2 ms or connection error | Bypass cache; emit `cache_degraded` | Latency and LLM cost up |
| 2 | `mcp-hybrid-rag` crash or hang | JSON-RPC > 150 ms, > 1% [BP] | Trip bulkhead; fail over to cached read-replica index; restart worker [BP]; if none, abstain with `E_DEGRADED` | Stale index |
| 3 | Reranker timeout | > 45 ms | RRF top-4 flagged `rerank_unavailable`; extractive-quote mode | Hallucination risk up; quote mode limits it |
| 4 | FAISS index missing or corrupt | Load-time check | BM25-only; flag `dense_unavailable`; alert | Recall drop; KPI-10 fails |
| 5 | `mcp-state-store` down | Health check; HTTP error | Read-only degraded mode; **all Tier 3 rejected** | Session context lost for the window |
| 6 | `mcp-graphify-vault` down | Timeout | Inject raw top-4 chunks (≈ 1,400 tokens vs. 320) | Token cost up; KPI-26 fails |
| 7 | LLM 429 | HTTP 429 | One jittered retry if ≥ 150 ms remains; else extractive answer, `E_DEGRADED` | Lower answer quality |
| 8 | LLM stall (> 250 ms step) | Step timer | Cancel stream; extractive answer; record `llm_timeout` | Partial answer; flagged |
| 9 | Triage misroute | Confidence below threshold | Default `HYBRID_RAG`; never `TRANSACTION_EXEC` | Unneeded retrieval latency |
| 10 | PII redactor or injection shield exception | Plugin exception | Fail closed: 503 with `Retry-After` | Availability loss, by design |
| 11 | Postgres unavailable during HITL write | `hitl_request` error | Reject the Tier 3 action; no partial write | User must retry |
| 12 | OTel collector down | Exporter error | Bounded in-memory queue; drop oldest spans; never block | Trace gaps (counted) |

## 12. Governance and security [BP, Gates 13 and 14]

- **Audit log:** SHA-256 hash-chained, immutable (`prev_hash + event_payload`). A scheduled verifier and "break = Sev-1" are [DD].
- **Tenant isolation:** see §2 rule 4.
- **Zero raw PII persistence.**
- **Signed MCP server manifests.** Verification at connect time is [DD].
- **Database [C10]:** the Blueprint specifies PostgreSQL (Gate 0), but also says "SQLite/Postgres" (Gate 13), "validate JDBC/SQLite persistence" (Gate 16, JMeter), and "live SQLite/Redis session state browser" (Gate 18, Tab 4). Proposed: Postgres in production; SQLite allowed for local, test, and workbench use. Confirm.
- **Red-team suite (100 prompts) [BP, Gate 14]:**
  1. Direct jailbreaks and system-prompt extraction.
  2. Indirect prompt injection hidden in retrieved Obsidian/PDF chunks.
  3. Multi-turn crescendo social engineering.
  4. Unicode homoglyph and `name[at]domain` PII smuggling.
  5. Tool argument overflow and SQLi fuzzing.
- **Statistical limits (X11):** a 100-prompt suite cannot demonstrate KPI-02 (≥ 99.8%) or KPI-03 (≥ 99.5%). Larger benchmark sets are needed for those KPIs.

## 13. Observability [BP, Gates 10 and 11]

- W3C TraceContext (`traceparent`) on every hop → OpenTelemetry Collector → Prometheus + Tempo + Grafana.
- **Gate 10 span names:** `hook.pre`, `cache.lookup`, `triage.classify`, `mcp.rag.bm25`, `mcp.rag.biencoder`, `mcp.rag.rrf`, `mcp.rag.rerank`, `agent.react.step`, `llm.generate`, `hook.post.ragas`.
- **Gate 12 and the Audit use shorter names.** Alias map (C18):

| Gate 10 span | Gate 12 / Audit name |
|---|---|
| `triage.classify` | `triage` |
| `mcp.rag.bm25` | `rag.bm25` |
| `mcp.rag.biencoder` | `rag.dense` |
| `mcp.rag.rerank` | `rag.rerank` |
| `hook.post.ragas` | `ragas` |
| `hook.pre`, `llm.generate` | same in both |

  Emit one canonical set and keep the alias map so KPI queries and Audit evidence stay comparable. The KPI table in §17 keeps the Blueprint's names.

**Incident runbook [BP, Gate 11]**

| Scenario | Alert trigger | Mitigation |
|---|---|---|
| Latency breach (> 600 ms) | p95 e2e latency > **550 ms for 30 s** | Enable aggressive semantic cache (cosine ≥ 0.92); drop BM25 and dense `top_k` from 50 to 25 |
| Hallucination surge | RAGAS faithfulness < 0.90 (5 min window) | Raise cross-encoder cutoff 0.42 → 0.55; strict extractive-quote mode |
| MCP server hang | JSON-RPC timeout > 150 ms (> 1% rate) | Trip MCP bulkhead circuit; fail over to cached read-replica index; restart MCP worker |
| Cache stampede / leak | Tenant key collision or Redis CPU > 85% | Enforce `SHA256(tenant_id:user_id:norm_query)` prefix + jittered TTL (300 s ± 45 s) |

**Proposed additions [DD] (C8):** the 550 ms trigger sits above the 450 ms p95 gate, so it fires only after the SLA is breached. Add a warning at p95 > 400 ms for 60 s and a page at p95 > 450 ms for 60 s, alongside the Blueprint row.

**Runtime-tunable controls [DD] (D3):** the Gate 11 mitigations change live settings: the cache cosine threshold, BM25 and dense `top_k`, the cross-encoder cutoff, extractive-quote mode, and bulkhead circuit trip and reset. These must be adjustable at runtime without a redeploy. The Blueprint column heading is "Automated Alert Trigger" [BP], so the alerts are meant to fire automatically. The sources do not say whether the mitigations apply automatically or need an operator's approval. Decide this in `design.md`.

## 14. Load testing [BP, Gates 15 and 16]

**k6** (`load_tests/k6_50k_load.js`)
- Stages: 2 min → 10K VUs; 3 min → 50K; 5 min at 50K; 2 min → 0.
- Thresholds as written in the Blueprint script [BP]: `http_req_duration` p(50) < 250, p(95) < 450, p(99) < 600; `http_req_failed` rate < 0.0005; `checks` rate > 0.999.
- **Implementation choice [DD] (M6):** set the thresholds to p(50) < 240, p(95) < 450, p(99) < 590, so a single run checks both the script and the KPIs (C9, C19). The other two thresholds stay as written.
- **Distributed execution [BP, Gate 15 title "Distributed Load Test"] (D4):** one k6 process cannot generate 50,000 VUs. The run needs distributed injectors, and the chosen method is undecided. The script hardcodes `http://localhost:8000`. Make the base URL an environment variable, so the same script runs against a staging host.
- Request: `POST http://localhost:8000/api/v1/agent/query`; body `tenant_id = tenant_${vuId % 50}`, `user_id = user_${vuId}`, query "Verify batch LOT-88392 safety protocol and return triage status"; header `X-Tenant-ID`.
- Checks: status 200; latency ≤ 600 ms; **`no cross-tenant leak`** (response `tenant_id` equals the request's), which is the KPI-28 probe. Keep it.
- Each iteration ends with `sleep(1)`. That drives the offered rate (X6).

**JMeter** (`load_tests/jmeter_50k_plan.jmx`)
- 1 controller, 10 workers × 5,000 threads = 50,000 VUs; ramp-up 180 s; loop count infinite over 600 s; non-GUI: `jmeter -n -t jmeter_50k_plan.jmx -R worker1..worker10 -l results_50k.jtl -e -o dashboard_50k/`.
- Duration Assertion 600 ms; JSONPath Assertion `$.faithfulness >= 0.92`; Constant Throughput Timer; Backend Listener (InfluxDB or Prometheus); validate JDBC/SQLite persistence and HTTP endpoints together.

**Gaps**
- **KPI-36 needs a 1-hour soak with RSS measurement.** The k6 plan runs 12 minutes and JMeter 10. Add a soak scenario [DD].
- The Audit claims stability "under burst traffic". No burst or spike scenario exists in the Blueprint's plans.

## 15. Token optimization [BP, Gate 17]

- Atomic Obsidian notes in `knowledge_vault/`, each with strict YAML frontmatter (`id`, `domain`, `risk_tier`, `version`) and explicit `[[wikilinks]]` to related rules.
- `graphify build ./src ./knowledge_vault --output graph.json` builds a unified graph of Python modules, Pydantic schemas, and Obsidian policy nodes.
- At runtime `mcp-graphify-vault` resolves the anchor node and traverses at most `depth=2`, injecting only the connected subgraph (~320 tokens vs. ~1,400 for a raw chunk dump).
- Gate: ≥ 45% prompt token reduction (KPI-26). The Blueprint also cites 55% (Gate 0) and 45–65% (Gate 17); the Audit claims 52.4%.

## 16. Developer workbench (Streamlit `app.py`) [BP, Gate 18]

1. **Architecture & Workflow:** `design.md` contract checklist; `workflow_orchestration.png`; live triage routing console; ReAct step-by-step trace accordion; Tier 3 HITL approve/reject queue.
2. **3-Stage Hybrid RAG Lab:** side-by-side table of Stage 1 BM25 scores, Stage 2 bi-encoder cosine scores, RRF fused ranks, and Stage 3 cross-encoder scores; interactive `k1`, `b`, `RRF k`, and `threshold` sliders.
3. **MCP & Plugin Inspector:** live status of `hybrid-rag`, `state-store`, `graphify-vault`; JSON-RPC payload inspector; **hot-toggle switches for Pre/Post execution plugins**. The draft locks the fail-closed plugins at runtime. That is a stricter proposal (C22): allow toggling in the dev workbench only, and lock `pii_redactor` and `injection_shield` in production.
4. **Memory & Graphify Tokens:** Graphify subgraph visualizer; Obsidian atomic note viewer; live **SQLite/Redis** session state browser; real-time **Token Savings & Cost Reduction** gauge.
5. **Red Team & Circuit Breaker:** one-click adversarial runner covering direct and indirect injections, obfuscated PII, an **infinite SKU retry loop**, and a **cross-tenant cache probe**; live circuit breaker trip and reset controls.
6. **36-KPI Scorecard & 50K Load:** live OTel span waterfall; RAGAS triad gauges; k6/JMeter 50,000-VU latency histogram (p50/p95/p99 vs. the 200–600 ms SLA); automated pass/fail scorecard. Gate 12 says the scorecard is in "Tab 5"; Gate 18 puts it in Tab 6 (C16).

## 17. KPI gate matrix [BP, Gate 12]

All 36 KPIs must be measured continuously and pass in CI/CD and the Streamlit scorecard. "Measured by" is the Blueprint's text (see §13 for span aliases).

| ID | Stage | KPI | Threshold | Measured by |
|---|---|---|---|---|
| KPI-01 | Ingress & Triage | Pre-Hook Guardrail Latency (p95) | ≤ 15 ms | OTel span `hook.pre` |
| KPI-02 | Ingress & Triage | PII Redaction Recall (Email/Phone/SSN/Obfuscated) | ≥ 99.8% | Presidio / Regex Audit |
| KPI-03 | Ingress & Triage | Prompt Injection Block Rate (Direct + Indirect) | ≥ 99.5% | Red Team Benchmark |
| KPI-04 | Ingress & Triage | Triage Routing Accuracy | ≥ 97.0% | Labeled Intent Set |
| KPI-05 | Ingress & Triage | Triage Classification Latency (p95) | ≤ 25 ms | OTel span `triage` |
| KPI-06 | Ingress & Triage | False Positive Refusal Rate on Benign Queries | ≤ 1.0% | Benign Control Set |
| KPI-07 | Hybrid Retrieval | Stage 1 BM25 Retrieval Latency (p95) | ≤ 12 ms | OTel span `rag.bm25` |
| KPI-08 | Hybrid Retrieval | Stage 2 Bi-Encoder + FAISS Latency (p95) | ≤ 25 ms | OTel span `rag.dense` |
| KPI-09 | Hybrid Retrieval | Stage 3 Cross-Encoder Rerank Latency (p95) | ≤ 45 ms | OTel span `rag.rerank` |
| KPI-10 | Hybrid Retrieval | Hybrid Context Recall@50 | ≥ 0.92 | RAGAS / IR Eval |
| KPI-11 | Hybrid Retrieval | Post-Rerank Context Precision@4 | ≥ 0.88 | RAGAS Evaluator |
| KPI-12 | Hybrid Retrieval | Mean Reciprocal Rank (MRR@4) | ≥ 0.85 | IR Benchmark Script |
| KPI-13 | Hybrid Retrieval | nDCG@4 | ≥ 0.86 | IR Benchmark Script |
| KPI-14 | Agent & Loop | Tool Schema First-Pass Validation Rate | ≥ 96.0% | Pydantic Telemetry |
| KPI-15 | Agent & Loop | Auto-Repair Success Rate on Malformed Args | ≥ 90.0% | Harness Metrics |
| KPI-16 | Agent & Loop | Infinite Loop Escape Rate (Circuit Breaker) | 100.0% | Chaos Loop Suite |
| KPI-17 | Agent & Loop | Mean ReAct Steps per Completed Task | ≤ 2.2 steps | OTel Trace Count |
| KPI-18 | Agent & Loop | Context Drift Score (Goal Cosine Alignment) | ≥ 0.85 | Embedding Drift Audit |
| KPI-19 | Agent & Loop | Tier 3 HITL Gate Enforcement Rate | 100.0% | Audit Log Integrity |
| KPI-20 | Generation & RAGAS | RAGAS Faithfulness Score | ≥ 0.92 | Post-Hook RAGAS |
| KPI-21 | Generation & RAGAS | RAGAS Answer Relevancy Score | ≥ 0.90 | Post-Hook RAGAS |
| KPI-22 | Generation & RAGAS | RAGAS Context Entity Recall | ≥ 0.85 | Post-Hook RAGAS |
| KPI-23 | Generation & RAGAS | Unanswerable Query Abstention Accuracy | ≥ 98.0% | Negative Test Set |
| KPI-24 | Generation & RAGAS | Unsupported Claim / Hallucination Rate | ≤ 0.5% | NLI Claim Verifier |
| KPI-25 | Generation & RAGAS | Time-To-First-Token (TTFT p95) | ≤ 180 ms | FastAPI Stream Metric |
| KPI-26 | Token & Graphify | Prompt Token Reduction via Graphify + Obsidian | ≥ 45.0% | Token Counter Hook |
| KPI-27 | Token & Graphify | Semantic Cache Hit Ratio (Steady State) | ≥ 35.0% | Redis Hit/Miss Metric |
| KPI-28 | Token & Graphify | Cross-Tenant Cache Isolation Integrity | 100.0% (0 leaks) | Concurrency Probe |
| KPI-29 | Token & Graphify | Cost per 1,000 Enterprise Queries | ≤ $0.45 | OTel Token Cost Calc |
| KPI-30 | Token & Graphify | MCP JSON-RPC Serialization Overhead (p95) | ≤ 4 ms | MCP Client Timer |
| KPI-31 | Scale & Load (50K) | End-to-End Response Latency (p50) @ 50K VUs | ≤ 240 ms | k6 + JMeter Summary |
| KPI-32 | Scale & Load (50K) | End-to-End Response Latency (p95) @ 50K VUs | ≤ 450 ms | k6 + JMeter Summary |
| KPI-33 | Scale & Load (50K) | End-to-End Response Latency (p99) @ 50K VUs | **≤ 590 ms (< 600 ms)** | k6 + JMeter Summary |
| KPI-34 | Scale & Load (50K) | HTTP 5xx Error Rate @ 50K VUs | ≤ 0.05% | k6 / JMeter Threshold |
| KPI-35 | Scale & Load (50K) | Sustained Throughput (Requests/Sec) | ≥ 8,500 RPS | k6 / JMeter Counter |
| KPI-36 | Scale & Load (50K) | Memory Leak / RSS Growth over 1-Hr Soak | ≤ 3.0% delta | Prometheus Node Exporter |

Owners are not yet assigned (a locking criterion, §22).

## 18. Audit report analysis

### 18.1 Baseline vs. claimed results [AU]. All claims are unverified.

| Dimension | Baseline [AU] | Claimed hardened [AU] | Gate | Status |
|---|---|---|---|---|
| E2E latency p50 / p95 / p99 | 2,400 / 4,800 / 7,100 ms | 185 / 390 / 540 ms | KPI-31..33 | Unverified. p50 185 ms is below the 200 ms lower bound, and the non-LLM hops of the HYBRID_RAG path already sum to 179 ms, so it is only plausible if most traffic is cache hits, direct answers, or abstentions. The Audit gives no route mix |
| Throughput @ 50K VUs | Crashed at 120 VUs | 9,240 RPS (0.01% 5xx) | KPI-34, KPI-35 | Unverified. 50,000 VUs / 9,240 RPS ≈ 5.4 s per iteration, inconsistent with k6's `sleep(1)` (X6) |
| RAGAS triad (Faith / Prec / Recall) | 0.61 / 0.54 / 0.68 | 0.94 / 0.91 / 0.89 | Gate 8 | Unverified. Answer Relevancy (KPI-21) and Entity Recall (KPI-22) are not reported |
| Prompt tokens / cost per 1K queries | 2,850 tok ($3.40) | 1,320 tok ($0.38) | KPI-26, KPI-29 | Unverified. Token drop ≈ 53.7% (Audit says 52.4%). Cost drop ≈ 89% is far larger than the token drop; scaling by tokens gives ≈ $1.57 (≈ $1.02 with a 35% hit ratio), so something else changed (X7) |
| Loop escape and HITL | 0% (infinite loops) | 100% deterministic | KPI-16, KPI-19 | Unverified |
| Adversarial and PII block | 42.0% leaked PII | 99.9% blocked | KPI-02 ≥ 99.8%, KPI-03 ≥ 99.5% | Unverified. One figure covers two KPIs; 99.9% cannot be measured on a 100-prompt suite (X11) |
| "All 36 KPIs passed" | — | — | All | The Audit shows six metric rows; the other KPIs have no numbers |
| "Zero security breaches" | — | — | — | No evidence attached |

### 18.2 Inconsistencies inside the Audit
- Audit section 3 says "99.9% Blocked"; Audit section 4 says "100% mitigation" of injections, schema fuzzing, and PII smuggling.
- "Stable latency under burst traffic": the Blueprint has no burst scenario.
- "2,850 tok / 1K queries": the unit is ambiguous. It reads as a per-query average, with dollars per 1K queries.
- The Audit's span names (`triage`, `rag.bm25`, `rag.dense`, `rag.rerank`, `ragas`) match Gate 12, not Gate 10.
- The Audit's cache key uses the raw `query`; the Blueprint uses `norm_query`.
- TensorRT-LLM appears only under "future roadmap", so the LLM server in use is presumably not TensorRT-LLM. Blueprint Gate 15 says "vLLM/TensorRT-LLM".
- The Audit says "18 rigorous architectural gates". The Blueprint's own title says "18-Gate" but numbers Gates 0–18, which is 19 gates. This count is a naming issue inside the Blueprint, not an Audit error.
- Pipeline is called "4-stage" (Blueprint headings say "3-stage"; C17).

### 18.3 Baseline failure → control → KPI [AU root causes]

| Root cause [AU] | Control in this design | KPIs |
|---|---|---|
| 1. Retrieval and grounding: keyword-blind single BM25; chunk boundaries split policy clauses; 38.6% hallucination | Hybrid BM25 + dense with 512/64 overlap chunks, RRF, cross-encoder, abstention < 0.42, Verification NLI | KPI-10..13, 20, 23, 24 |
| 2. Concurrency and latency: blocking synchronous DB calls; unbatched embedding inference; global Redis key collisions leaking across users; spikes over 7,100 ms | Async FastAPI and **non-blocking database I/O** [DD rule: no blocking calls on the event loop]; ONNX micro-batching; tenant-isolated cache keys; bulkheads | KPI-08, 09, 28, 31..36 |
| 3. Control plane: unbounded ReAct loops; brittle schema validation (`SKU99`) | Bounded loop; harness auto-repair; circuit breaker | KPI-14..17 |
| 4. Token inefficiency: raw unpruned policy PDFs in prompts | Atomic Obsidian notes; Graphify depth-2 pruning; observation compression | KPI-25, 26, 29 |
| 5. Governance: indirect injection, obfuscated PII (`name[at]domain`), no HITL | Fail-closed pre-hooks; HITL gate; hash-chained audit log; signed manifests; red-team suite | KPI-02, 03, 19 |
| Context drift [BP mission; not one of the Audit's five causes] | `GoalAnchorBlock` at index 0; observation compression; sliding window of 3 turns | KPI-18 |

### 18.4 Audit roadmap: out of scope [AU §5]

The Audit lists three future items: shadow-traffic A/B testing of fine-tuned LoRA adapters, multi-region database replication, and TensorRT-LLM serving. None is in the current build. Treat them as out of scope until a gate adds them.

### 18.5 Evidence checklist [AU lists the evidence; paths are DD]

| Audit evidence | Artifact to produce |
|---|---|
| k6 and JMeter 50K VU summary | `reports/load/k6_summary.json`, `results_50k.jtl`, `dashboard_50k/` |
| Load test dashboard | Grafana export in `reports/load/` |
| 250-query golden set run | `reports/ragas/golden_run_<date>.json` |
| OTel token counter audit | `reports/tokens/token_audit_<date>.json` |
| Chaos and red-team test suite | `reports/chaos/`, `reports/redteam/` |
| 100-prompt red-team log | `reports/redteam/log_<date>.jsonl` |
| 36-KPI scorecard | `reports/kpi_scorecard.json` |
| 1-hour soak (KPI-36) | `reports/soak/rss_<date>.csv` |

## 19. Architecture decision records [DD; Blueprint Gate 1.7 requires ADRs on retrieval weights, ONNX quantization, Redis key isolation, bounded ReAct loops]

- **ADR-01 — RRF over weighted-score fusion.** BM25 and cosine are on different scales, so weighted sums need normalization and a tuned weight. RRF uses ranks only; `RRF k` is the only fusion knob (Tab 2). Dense-only was rejected: keyword-blind on SKU/LOT codes, the documented cause of the 38.6% hallucination rate.
- **ADR-02 — FP16 reranker; INT8 only on evidence.** INT8 only if KPI-09 fails and KPI-20 and KPI-11 still pass after quantization.
- **ADR-03 — Redis key isolation.** §10. Open trade-off with KPI-27.
- **ADR-04 — Bounded ReAct loops.** The Blueprint's 450 ms loop budget equals the entire p95 SLA. The draft proposes 380 ms so the worst case is 442 ms (C5).

## 20. Deviation register (Blueprint/Audit value vs. this file)

Statuses: **Resolved** = the sources conflict and the stricter value satisfies both; **Proposed** = a deviation that needs approval; **Open** = undecided; **Assumption** = an inference by the author; **Closed** = checked, no action needed; **Draft done** = the artifact exists but must be regenerated after open items are decided.

| # | Topic | Blueprint / Audit says | This file says | Status |
|---|---|---|---|---|
| C1 | FAISS index | Gate 0: IVF-PQ. Gate 3: HNSW | **HNSW** (Gate 3 parameters and KPI-08 match it) | Resolved |
| C2 | Triage latency | Gate 2: < 30 ms. Gate 6 and KPI-05: ≤ 25 ms | **25 ms** | Resolved |
| C3 | Reranker latency | Gate 0: "sub-35 ms". Gate 3 and KPI-09: < 45 ms | **45 ms** gate; 35 ms is an aim | Resolved |
| C4 | Token reduction | 55% (Gate 0); 45–65% (Gate 17); ≥ 45% (KPI-26); Audit 52.4% | **≥ 45%** gate | Resolved |
| C5 | Loop budget | `total_loop_budget` 450 ms (equals the p95 SLA) | Proposed **380 ms** | Proposed, needs approval |
| C6 | Step vs. LLM timeout | `per_step_timeout` 120 ms | Separate 250 ms LLM step timeout | Proposed |
| C7 | Third subagent | Gate 2 "Audit"; Gate 7 and Audit report "Verification" | Treated as the same subagent | Assumption (see C23) |
| C8 | Latency alert | p95 > 550 ms for 30 s | Blueprint row kept; adds warn 400 / page 450 ms | Proposed addition |
| C9 | p50 threshold | KPI-31 ≤ 240 ms vs. k6 `p(50)<250` | **240 ms** | Resolved (stricter) |
| C10 | Database | Postgres (Gate 0); SQLite mentioned in Gates 13, 16, 18 | Postgres in production; SQLite for local, test, and workbench | Proposed, confirm |
| C11 | Cache hit ratio vs. key | Key includes `user_id`; KPI-27 ≥ 35% | Per-user key kept; trade-off flagged | Open |
| C12 | Gate 2 image | Required before app logic | PNG exists and follows the Gate 2 chain; regenerate after open items | Draft done |
| C13 | Audit scope | "All 36 KPIs passed"; six metric rows | Treated as unverified | Resolved |
| C14 | stdio transport | `mcp-hybrid-rag` over stdio, 150 ms timeout | Long-lived process, not per-call spawn | Proposed |
| C15 | "Truncated" Step 6 / 8 / 10 text | Labels trail the paragraphs that hold the content | Nothing missing | **Closed** |
| C16 | Scorecard tab | Gate 12: Tab 5. Gate 18: Tab 6 | **Tab 6** (Gate 18 defines the tabs) | Resolved |
| C17 | Pipeline name | Audit "4-stage"; Blueprint "3-stage" | "3-stage retrieval + RRF fusion" | Resolved |
| C18 | Span names | Gate 10: `mcp.rag.*`, `triage.classify`. Gate 12 and Audit: `rag.*`, `triage`, `ragas` | One canonical set plus alias map (§13) | Open |
| C19 | KPI-33 vs. k6 | KPI-33 ≤ 590 ms; k6 `p(99)<600` | **590 ms** KPI gate; 600 ms is the k6 outer threshold | Resolved (stricter) |
| C20 | Circuit-breaker scope | Unspecified; Tab 5 has trip/reset | Per-request counter plus shared resettable state suggested | Open |
| C21 | RAGAS sync vs. async | Not stated; several items imply synchronous | **Asynchronous** (user decision). Off the request path; scores stored; API does not return faithfulness | Resolved (user decision) |
| C22 | Tab 3 plugin toggles | Hot-toggle for Pre/Post plugins | Locked in production for PII and injection; toggle in dev | Proposed |
| C24 | Loop budget enforcement, local hybrid run | CLAUDE.md §8 and C5: 380 ms total loop budget | Default stays 380 ms (`LOOP_BUDGET_MS`). The local hybrid API was run with `LOOP_BUDGET_MS=0` (no budget) at the user's direction, because the CPU reranker takes about 15 s per query. Latency gates are not met in that run | Approved by user for local work; revert to 380 ms (or an approved value) when latency work is done |
| C25 | MCP call timeout, `hybrid-rag` | `mcp_config.json` `timeout_ms: 150` | `MCP_RAG_TIMEOUT_S` default 60 s. The CPU reranker takes about 15 s per call, so 150 ms cannot be met locally. Approved by user for local work | Approved by user for local work; revert when latency work is done |
| C23 | Gate 2 "Audit Subagent" branch | Four boxes (Direct, Retrieval, Execution, Audit) vs. four triage labels (no Audit label; `REJECT_OOS` unmatched) | Drawn as an Audit/Verification branch plus a separate `REJECT_OOS` exit | Assumption |

## 21. Open decisions (required before locking)

From the design draft's §9:
1. **Cache key (C11 / ADR-03)** and what "semantic" means (X2).
2. **Loop budget (C5):** accept 380 ms, or change the SLA?
3. *(closed: C15)*
4. **Audit evidence:** it cannot exist before code does, and code is blocked until locking. Resolve by treating the Audit's numbers as claims (§18) and removing any that are not reproduced.
5. **LLM capacity:** replace "confirm ~1,380 in flight" with a capacity plan based on the request rate chosen in X6.
6. *(resolved: C19)* KPI-33 gate is ≤ 590 ms.

Added after source review:
7. **RAGAS synchronous or asynchronous (C21).** Resolved: asynchronous (user decision).
8. **Circuit-breaker scope (C20).**
9. **Authentication and authorization model**, and where the tenant comes from (§6.8).
10. **SQLite scope (C10).**
11. **Test-set sizes** for KPI-02, 03, 04, 06, 23 (X11).
12. **stdio vs. a shared inference service (X10).**
13. **LLM model and serving hardware; triage classifier model and training data.** Neither source specifies them, and KPI-25, 29, and 35 depend on them.

## 22. Definition of "locked"

`design.md` is locked when:
- every deviation in §20 has a status of Resolved or Closed, or an approved Proposed;
- the open decisions in §21 are answered;
- the 36 KPIs have owners;
- a reviewer has signed off on the FMEA table.

After locking, changes go through a new ADR and a version bump in `design.md`'s header.

## 23. Known contradictions in the sources (decide before locking)

Figures marked "≈" are arithmetic from the sources, not measurements. No threshold has been changed to hide these.

- **X1 — Post-execution work on the request path.** The Verification subagent runs an NLI check and can veto drafts. `ragas_auditor` has `min_faithfulness: 0.92`, and the JMeter plan asserts `$.faithfulness` on the response. The 13-hop table has no verification or RAGAS hop. If any of that is synchronous, the 429 ms nominal total is understated (C21).
- **X2 — "Semantic" cache vs. exact key** (§10).
- **X3 — Abstention "< 90 ms" has no clock start.** The hops before the rerank decision sum to 137 ms (≈ 125 ms with BM25 and dense in parallel). 90 ms can only hold if measured from the rerank decision.
- **X4 — Rerank sizing** (§9).
- **X5 — What the SLA measures.** k6's `http_req_duration` runs to the last byte. `llm.generate` gets 250 ms including a 180 ms TTFT, leaving about 70 ms to stream the answer. Define first-byte vs. last-byte.
- **X6 — Load model vs. throughput target.** k6 runs 50,000 VUs with `sleep(1)`, offering ≈ 50,000 / (0.24 + 1) ≈ 40,000 RPS. KPI-35 asks for ≥ 8,500 RPS, and the Audit reports 9,240 RPS at 50K VUs (≈ 5.4 s per iteration). At ≈ 40,000 RPS and a 35% hit ratio, the in-flight generation estimate is ≈ 6,500, not ≈ 1,380. The Constant Throughput Timer in the JMeter plan also caps the rate. Decide the intended request rate and think time.
- **X7 — Audit cost vs. tokens.** $3.40 → $0.38 is ≈ 89% while tokens fell ≈ 54%; see §18.1. Per-hop p95s also do not prove p99.
- **X8 — Hook latency.** PII redaction (Presidio) plus the injection classifier in 15 ms p95 is tight. Benchmark it before accepting KPI-01.
- **X9 — Locking deadlock.** §21 items 4 and 5 cannot be satisfied before code exists.
- **X10 — stdio vs. micro-batching.** The Blueprint puts all three RAG stages in `rag_server.py` over stdio. A stdio server is one child process per API worker, so batch-32 / 8 ms micro-batching only combines requests inside one worker, and every worker loads its own FAISS index and ONNX models. Gate 0's "in-process Triton / ONNX microservice" points the other way. This is a risk to evaluate, not a decision.
- **X11 — Test sets too small for the thresholds.** Zero failures in 100 trials only bounds the failure rate at ≈ 3% (rule of three), and ≈ 600 trials are needed to bound it at 0.5%. With about 38 negative questions, KPI-23 (≥ 98%) allows zero misses (37/38 ≈ 97.4%). The sizes of the labeled routing set (KPI-04), benign set (KPI-06), and PII set (KPI-02) are unspecified.

## 24. Commands and target repo layout

Only the diagram command works today.

```bash
# Gate 2 diagram (works now; run from the repo root)
dot -Tpng -Gdpi=300 docs/workflow_orchestration.dot -o docs/workflow_orchestration.png

# MCP servers (from mcp_config.json)
uv run mcp_servers/rag_server.py
uv run mcp_servers/graphify_server.py

# Graph build (Gate 17)
graphify build ./src ./knowledge_vault --output graph.json

# Developer workbench
streamlit run app.py

# Load tests (single node: fine for smoke runs only; 50K VUs needs distributed injectors, see §14)
k6 run load_tests/k6_50k_load.js
jmeter -n -t load_tests/jmeter_50k_plan.jmx -R worker1..worker10 -l results_50k.jtl -e -o dashboard_50k/
```

Not specified in the sources: the API server start command, the test runner, and the linter. Add them when the code exists.

```
.
├── CLAUDE.md
├── design.md                  # Gate 1, locked before code
├── app.py                     # Streamlit, 6 tabs
├── mcp_config.json
├── mcp_servers/
│   ├── rag_server.py
│   ├── graphify_server.py
│   └── state_store/           # Streamable HTTP
├── knowledge_vault/           # atomic Obsidian notes
├── load_tests/
│   ├── k6_50k_load.js
│   └── jmeter_50k_plan.jmx
├── reports/                   # evidence artifacts (§18.5)
├── docs/
│   ├── workflow_orchestration.dot
│   └── workflow_orchestration.png
└── (src/, tests/, red-team suite, golden set)
```

## 25. Working conventions

- Stay within MCP. Don't add tool integrations that bypass the harness.
- Tag new statements [BP], [AU], or [DD]. Record every deviation from the Blueprint in §20.
- Keep this file current. Update it when a gate passes or a §21 decision is answered.
- Language: Python 3.11, type hints, Pydantic v2 for all tool contracts.
