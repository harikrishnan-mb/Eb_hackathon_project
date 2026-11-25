"""Harness and orchestrator wired to MCP servers, with the bridge routed to the server
functions in-process (no subprocesses). Real processes are covered by scripts/mcp_smoke.py
and by the live API run recorded in CLAUDE.md."""

import importlib.util
import sys
from pathlib import Path

import pytest

from assistant.auth import Principal
from assistant.errors import ErrorCode, PlatformError
from assistant.mcp_bridge import McpAudit, McpHitlStore
from assistant.orchestrator import Corpus, Orchestrator

ROOT = Path(__file__).resolve().parents[1]
KB = ROOT / "knowledge_vault"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "mcp_servers" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeBridge:
    """Same call() contract as McpBridge, dispatching to the server functions in-process."""

    def __init__(self, state_db, rag):
        self.rag = rag
        self.state = _load("state_store_server")
        self.routes = {
            ("rag", "hybrid_retrieve"): rag.hybrid_retrieve,
            ("state", "hitl_request"): self.state.hitl_request,
            ("state", "hitl_decide"): self.state.hitl_decide,
            ("state", "hitl_status"): self.state.hitl_status,
            ("state", "audit_append"): self.state.audit_append,
        }

    def call(self, server, tool, args, timeout_s):
        out = self.routes[(server, tool)](**args)
        if "error" in out:
            raise PlatformError(ErrorCode.VALIDATION, f"{server}.{tool} rejected arguments")
        return out


@pytest.fixture()
def bridge(monkeypatch, tmp_path):
    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("CORPUS_DIR", str(KB))
    monkeypatch.delenv("RETRIEVAL_MODE", raising=False)
    rag = _load("rag_server")
    rag._retriever = None
    return FakeBridge(tmp_path / "state.db", rag)


def test_retrieval_goes_through_rag_server(bridge):
    orch = Orchestrator(Corpus.from_dir(KB), mcp=bridge)
    r = orch.answer(Principal("tenant_a", "u1"), "What is the returns policy?")
    assert r["route"] == "HYBRID_RAG" and r["status"] == "answered"
    assert "tool:hybrid_retrieve" in r["trace"]
    assert r["citations"]


def test_hitl_store_round_trip_is_tenant_scoped(bridge):
    store = McpHitlStore(bridge)
    rid = "3f2b6c1e-8a9d-4b7e-9c1a-2d4e6f8a0b1c"
    assert store.is_approved("tenant_a", rid) is False
    store.decide("tenant_a", rid, "APPROVE", reviewer_id="rev-1")
    assert store.is_approved("tenant_a", rid) is True
    assert store.is_approved("tenant_b", rid) is False


def test_audit_entries_are_written_by_state_server(bridge):
    audit = McpAudit(bridge)
    audit.append("tenant_a", "hitl_request", {"request_id": "r1"})
    assert bridge.state._state()[2].verify() is None
    assert [row[1] for row in bridge.state._state()[2].rows_for_tenant("tenant_a")] == ["hitl_request"]


def test_rag_validation_error_surfaces_as_platform_error(bridge):
    with pytest.raises(PlatformError):
        bridge.call("rag", "hybrid_retrieve", {"request_id": "bad", "tenant_id": "tenant_a", "query": "ab"}, 5)
