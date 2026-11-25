"""Synchronous bridge from the harness to the MCP servers (CLAUDE.md §4, §5).

The harness is synchronous. The MCP Python client is asyncio. This module runs one
event loop in a background thread. That loop owns the long-lived sessions:
  - "rag":   stdio child process, mcp_servers/rag_server.py
  - "state": Streamable HTTP, http://localhost:8081/mcp (already running)

Calls return the tool's JSON result as a dict. Failures map to PlatformError:
  timeout              -> E_TIMEOUT
  server unreachable   -> E_DEGRADED
  tool returned error  -> E_VALIDATION (the server's own validation)
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import sys
import threading
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from .errors import ErrorCode, PlatformError

ROOT = Path(__file__).resolve().parents[1]


class McpBridge:
    def __init__(
        self,
        rag_script: str | Path | None = None,
        state_url: str | None = None,
        start_timeout_s: float = 120.0,
    ) -> None:
        self._rag_script = Path(rag_script) if rag_script else ROOT / "mcp_servers" / "rag_server.py"
        self._state_url = state_url
        self._start_timeout_s = start_timeout_s
        self._sessions: dict[str, Any] = {}
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="mcp-bridge", daemon=True)
        self._thread.start()
        self._stop: asyncio.Event | None = None
        self._ready: concurrent.futures.Future = concurrent.futures.Future()
        asyncio.run_coroutine_threadsafe(self._serve(), self._loop)
        self._ready.result(timeout=start_timeout_s)

    # ---- lifecycle (runs on the bridge's loop) ---------------------------
    async def _serve(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp.client.streamable_http import streamablehttp_client

        self._stop = asyncio.Event()
        try:
            async with AsyncExitStack() as stack:
                params = StdioServerParameters(
                    command=sys.executable, args=[str(self._rag_script)], cwd=str(ROOT),
                    env=dict(os.environ),  # the SDK default env would drop MODELS_DIR, RETRIEVAL_MODE, etc.
                )
                r, w = await stack.enter_async_context(stdio_client(params))
                rag = await stack.enter_async_context(ClientSession(r, w))
                await rag.initialize()
                self._sessions["rag"] = rag

                # The state server is called through a fresh session per request (see _state_call),
                # so a restart of that server cannot leave this bridge holding a dead session.

                self._ready.set_result(True)
                await self._stop.wait()
        except BaseException as exc:  # noqa: BLE001 - report startup failure to the caller
            if not self._ready.done():
                self._ready.set_exception(exc)
            raise

    async def _state_call(self, tool: str, args: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        """One short-lived Streamable HTTP session per call. Slightly slower, but a restarted
        state server is picked up on the next request without restarting the API."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async def _do() -> dict[str, Any]:
            async with streamablehttp_client(self._state_url) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool, args)
            text = result.content[0].text if result.content else "{}"
            return json.loads(text)

        return await asyncio.wait_for(_do(), timeout_s)

    def close(self) -> None:
        if self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)

    # ---- calls -------------------------------------------------------------
    def wait_ready(self, server: str = "rag", timeout_s: float = 900.0, poll_s: float = 2.0) -> None:
        """Block until the server reports ready (models loaded), or raise PlatformError on timeout."""
        import time

        deadline = time.monotonic() + timeout_s
        while True:
            try:
                if self.call(server, "ready", {}, timeout_s=10.0).get("ready"):
                    return
            except PlatformError:
                pass  # not up yet; keep polling until the deadline
            if time.monotonic() > deadline:
                raise PlatformError(ErrorCode.TIMEOUT, f"{server} not ready after {timeout_s}s")
            time.sleep(poll_s)

    def call(self, server: str, tool: str, args: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        if server == "state" and self._state_url:
            async def _invoke() -> dict[str, Any]:
                return await self._state_call(tool, args, timeout_s)
        elif server in self._sessions:
            async def _invoke() -> dict[str, Any]:
                result = await asyncio.wait_for(self._sessions[server].call_tool(tool, args), timeout_s)
                text = result.content[0].text if result.content else "{}"
                return json.loads(text)
        else:
            raise PlatformError(ErrorCode.DEGRADED, f"mcp server '{server}' not connected")

        future = asyncio.run_coroutine_threadsafe(_invoke(), self._loop)
        try:
            out = future.result(timeout=timeout_s + 5.0)
        except (asyncio.TimeoutError, concurrent.futures.TimeoutError) as exc:
            future.cancel()
            raise PlatformError(ErrorCode.TIMEOUT, f"{server}.{tool} timed out after {timeout_s}s") from exc
        except PlatformError:
            raise
        except Exception as exc:  # noqa: BLE001 - connection or protocol failure
            raise PlatformError(ErrorCode.DEGRADED, f"{server}.{tool} unavailable") from exc
        if "error" in out:
            raise PlatformError(ErrorCode.VALIDATION, f"{server}.{tool} rejected arguments",
                                {"detail": out["error"]})
        return out


class McpHitlStore:
    """HITL decisions held by the state-store server (same interface as harness.HitlStore)."""

    def __init__(self, bridge: McpBridge) -> None:
        self.bridge = bridge

    def decide(self, tenant_id: str, request_id: str, decision: str, reviewer_id: str = "reviewer") -> None:
        self.bridge.call("state", "hitl_decide", {
            "request_id": request_id, "tenant_id": tenant_id,
            "reviewer_id": reviewer_id, "decision": decision,
        }, timeout_s=5.0)

    def is_approved(self, tenant_id: str, request_id: str) -> bool:
        out = self.bridge.call("state", "hitl_status", {"tenant_id": tenant_id, "request_id": request_id},
                               timeout_s=5.0)
        return bool(out.get("approved"))


class McpAudit:
    """Audit appends through the state-store server (hash-chained there)."""

    def __init__(self, bridge: McpBridge) -> None:
        self.bridge = bridge

    def append(self, tenant_id: str, event: str, payload: dict) -> str:
        return self.bridge.call("state", "audit_append",
                                {"tenant_id": tenant_id, "event": event, "payload": payload},
                                timeout_s=5.0)["hash"]
