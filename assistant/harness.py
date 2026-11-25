"""ToolExecutionHarness (design.md §1.2.4).

Every MCP call passes these steps in order:
  1. Strict validation (Pydantic v2, extra="forbid").
  2. Deterministic auto-repair, one pass, original value logged.
  3. RBAC tier check against the calling subagent's ceiling.
  4. Idempotency for mutating calls.
Tier 3 calls additionally require an approved HITL record (Gate 9.4).

The idempotency and HITL stores here are in-memory. They satisfy the contract
for tests and local runs. Production needs a shared store (Postgres or Redis)
with tenant-scoped keys (design §1.11).
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from . import contracts
from .errors import ErrorCode, PlatformError
from .rbac import TOOL_TIER, Tier, is_allowed
from .repair import Repair, auto_repair

log = logging.getLogger("assistant.harness")

# tool name -> (input model, mutating?)
TOOL_MODELS: dict[str, tuple[type[BaseModel], bool]] = {
    "hybrid_retrieve": (contracts.HybridRetrieveInput, False),
    "graph_subgraph": (contracts.GraphSubgraphInput, False),
    "session_facts_put": (contracts.SessionFactsPutInput, True),
    "workflow_transition": (contracts.Tier3Request, True),
    "hitl_request": (contracts.Tier3Request, True),
}


@dataclass
class HitlStore:
    """Approved HITL decisions, keyed by (tenant_id, request_id)."""

    _approved: set[tuple[str, str]] = field(default_factory=set)

    def decide(self, tenant_id: str, request_id: str, decision: str, reviewer_id: str = "reviewer") -> None:
        if decision == "APPROVE":
            self._approved.add((tenant_id, request_id))
        else:
            self._approved.discard((tenant_id, request_id))

    def is_approved(self, tenant_id: str, request_id: str) -> bool:
        return (tenant_id, request_id) in self._approved


@dataclass
class IdempotencyStore:
    """Stored results for mutating calls, keyed by idempotency_key."""

    _records: dict[str, tuple[str, dict[str, Any]]] = field(default_factory=dict)

    def lookup(self, key: str) -> tuple[str, dict[str, Any]] | None:
        return self._records.get(key)

    def store(self, key: str, args_hash: str, result: dict[str, Any]) -> None:
        self._records[key] = (args_hash, result)


@dataclass
class ToolResult:
    ok: bool
    value: dict[str, Any] | None = None
    error: PlatformError | None = None
    repairs: list[Repair] = field(default_factory=list)


def _args_hash(model: BaseModel) -> str:
    payload = model.model_dump(mode="json", exclude={"idempotency_key"})
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class ToolExecutionHarness:
    def __init__(
        self,
        handlers: dict[str, Callable[[BaseModel], dict[str, Any]]],
        hitl: HitlStore | None = None,
        idempotency: IdempotencyStore | None = None,
    ) -> None:
        self._handlers = handlers
        self.hitl = hitl or HitlStore()
        self.idempotency = idempotency or IdempotencyStore()

    def call(self, *, tool: str, subagent: str, raw: dict[str, Any]) -> ToolResult:
        # Step 0: the tool must exist.
        if tool not in TOOL_MODELS or tool not in self._handlers:
            return self._fail(PlatformError(ErrorCode.INVALID_REQUEST, f"unknown tool '{tool}'"))

        model_cls, mutating = TOOL_MODELS[tool]

        # Step 2 (auto-repair) runs before strict validation.
        repaired, repairs = auto_repair(raw)
        for r in repairs:
            log.info("auto-repair tool=%s field=%s", tool, r.field)

        # Step 1: strict validation.
        try:
            model = model_cls.model_validate(repaired, strict=True)
        except ValidationError as exc:
            fields = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
            return self._fail(
                PlatformError(ErrorCode.VALIDATION, "schema validation failed", {"fields": fields}),
                repairs,
            )

        # Step 3: RBAC.
        if not is_allowed(subagent, tool):
            return self._fail(
                PlatformError(
                    ErrorCode.RBAC_DENIED,
                    f"subagent '{subagent}' may not call '{tool}' ({TOOL_TIER[tool].name})",
                ),
                repairs,
            )

        # Step 4: idempotency for mutating calls.
        if mutating:
            # Tier 3 calls carry idempotency_key. session_facts_put has no such
            # field in design §1.2.2, so its request_id is the idempotency key
            # (a documented default, not Blueprint text).
            key = getattr(model, "idempotency_key", None) or model.request_id  # type: ignore[attr-defined]
            h = _args_hash(model)
            prior = self.idempotency.lookup(key)
            if prior is not None:
                prior_hash, stored = prior
                if prior_hash == h:
                    return ToolResult(ok=True, value=stored, repairs=repairs)
                return self._fail(
                    PlatformError(ErrorCode.IDEMPOTENT_REPLAY, "same idempotency_key, different args"),
                    repairs,
                )

        # Tier 3: an approved HITL record must exist before execution.
        if TOOL_TIER[tool] == Tier.TIER_3_MUTATE:
            tenant = model.tenant_id
            request_id = model.request_id
            if not self.hitl.is_approved(tenant, request_id):
                return self._fail(
                    PlatformError(ErrorCode.HITL_PENDING, "awaiting approved hitl_decide record"),
                    repairs,
                )

        try:
            result = self._handlers[tool](model)
        except PlatformError as exc:
            return self._fail(exc, repairs)
        except Exception:  # noqa: BLE001 - surfaced as a degraded response
            log.exception("handler failure tool=%s", tool)
            return self._fail(PlatformError(ErrorCode.DEGRADED, "tool handler failed"), repairs)

        if mutating:
            self.idempotency.store(model.idempotency_key, _args_hash(model), result)  # type: ignore[attr-defined]
        return ToolResult(ok=True, value=result, repairs=repairs)

    @staticmethod
    def _fail(error: PlatformError, repairs: list[Repair] | None = None) -> ToolResult:
        return ToolResult(ok=False, error=error, repairs=repairs or [])
