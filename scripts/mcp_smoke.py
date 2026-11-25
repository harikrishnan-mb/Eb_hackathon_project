"""Smoke test: call each MCP server through a real MCP client, using mcp_config.json transports.

hybrid-rag and graphify-vault: stdio (one child process each).
state-store: Streamable HTTP at http://localhost:8081/mcp (start it first:
  python mcp_servers/state_store_server.py).

Prints the tool list and one call per tool. Exit code 1 if any call fails.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
STATE_URL = "http://localhost:8081/mcp"
TENANT = "tenant_a"


def _data(result) -> dict:
    text = result.content[0].text if result.content else "{}"
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text}


async def _stdio(script: str):
    params = StdioServerParameters(command=PY, args=[str(ROOT / script)], cwd=str(ROOT))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return session


async def check_rag(failures: list[str]) -> None:
    params = StdioServerParameters(command=PY, args=[str(ROOT / "mcp_servers/rag_server.py")], cwd=str(ROOT))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            tools = [t.name for t in (await s.list_tools()).tools]
            print("hybrid-rag tools:", tools)
            r = await s.call_tool("hybrid_retrieve", {
                "request_id": str(uuid.uuid4()), "tenant_id": TENANT,
                "query": "What is the LOT-00044 recall about?"})
            d = _data(r)
            print("hybrid_retrieve ->", d.get("abstain"), [c["chunk_id"] for c in d.get("chunks", [])], d.get("flags"))
            if "chunks" not in d:
                failures.append(f"hybrid_retrieve: {d}")
            bad = await s.call_tool("hybrid_retrieve", {"request_id": "x", "tenant_id": TENANT, "query": "ab"})
            if "E_VALIDATION" not in json.dumps(_data(bad)):
                failures.append("hybrid_retrieve did not reject invalid input")


async def check_graph(failures: list[str]) -> None:
    params = StdioServerParameters(command=PY, args=[str(ROOT / "mcp_servers/graphify_server.py")], cwd=str(ROOT))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            print("graphify-vault tools:", [t.name for t in (await s.list_tools()).tools])
            d = _data(await s.call_tool("graph_subgraph", {"anchor_id": "note_lot_recall", "depth": 2, "max_tokens": 320}))
            print("graph_subgraph ->", [n["id"] for n in d.get("nodes", [])], d.get("token_estimate"))
            if not d.get("nodes"):
                failures.append(f"graph_subgraph: {d}")


async def check_state(failures: list[str]) -> None:
    async with streamablehttp_client(STATE_URL) as (read, write, _):
        async with ClientSession(read, write) as s:
            await s.initialize()
            print("state-store tools:", [t.name for t in (await s.list_tools()).tools])
            rid = str(uuid.uuid4())
            sid = str(uuid.uuid4())
            req = await s.call_tool("hitl_request", {
                "request_id": rid, "idempotency_key": str(uuid.uuid4()), "tenant_id": TENANT,
                "action": "workflow_change", "args": {"query": "refund"}, "risk_tier": "TIER_3_MUTATE"})
            print("hitl_request ->", _data(req).get("status"))
            dec = await s.call_tool("hitl_decide", {
                "request_id": rid, "tenant_id": TENANT, "reviewer_id": "rev-1", "decision": "APPROVE"})
            print("hitl_decide ->", _data(dec).get("decision"))
            if _data(dec).get("decision") != "APPROVE":
                failures.append("hitl_decide")
            put = await s.call_tool("session_facts_put", {
                "request_id": rid, "tenant_id": TENANT, "session_id": sid, "fact_key": "order_status",
                "fact_value": "shipped", "source_chunk_hash": "a" * 64})
            got = await s.call_tool("session_facts_get", {"tenant_id": TENANT, "session_id": sid, "fact_key": "order_status"})
            g = _data(got)
            print("session_facts_get ->", g.get("found"), g.get("fact_value"))
            if not (g.get("found") and g.get("fact_value") == "shipped"):
                failures.append("session_facts round trip")
            other = await s.call_tool("session_facts_get", {"tenant_id": "tenant_b", "session_id": sid, "fact_key": "order_status"})
            if _data(other).get("found"):
                failures.append("session facts leaked across tenants")
            audit = await s.call_tool("audit_append", {"tenant_id": TENANT, "event": "smoke_check", "payload": {"ok": True}})
            print("audit_append ->", "hash" in _data(audit))
            if "hash" not in _data(audit):
                failures.append("audit_append")


async def main() -> int:
    failures: list[str] = []
    await check_rag(failures)
    await check_graph(failures)
    await check_state(failures)
    print("FAILURES:" if failures else "ALL CHECKS PASSED", *failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
