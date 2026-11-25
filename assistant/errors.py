"""JSON-RPC error codes from design.md §1.2.3."""

from __future__ import annotations

from enum import IntEnum
from typing import Any


class ErrorCode(IntEnum):
    INVALID_REQUEST = -32600
    VALIDATION = -32602
    TENANT_FORBIDDEN = -32001
    TIMEOUT = -32002
    BREAKER_OPEN = -32003
    HITL_PENDING = -32004
    RBAC_DENIED = -32005
    IDEMPOTENT_REPLAY = -32006
    DEGRADED = -32007


_NAMES = {
    ErrorCode.INVALID_REQUEST: "E_INVALID_REQUEST",
    ErrorCode.VALIDATION: "E_VALIDATION",
    ErrorCode.TENANT_FORBIDDEN: "E_TENANT_FORBIDDEN",
    ErrorCode.TIMEOUT: "E_TIMEOUT",
    ErrorCode.BREAKER_OPEN: "E_BREAKER_OPEN",
    ErrorCode.HITL_PENDING: "E_HITL_PENDING",
    ErrorCode.RBAC_DENIED: "E_RBAC_DENIED",
    ErrorCode.IDEMPOTENT_REPLAY: "E_IDEMPOTENT_REPLAY",
    ErrorCode.DEGRADED: "E_DEGRADED",
}


class PlatformError(Exception):
    """A structured error returned to callers. Never carries raw PII."""

    def __init__(self, code: ErrorCode, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})

    @property
    def name(self) -> str:
        return _NAMES[self.code]

    def to_jsonrpc(self) -> dict[str, Any]:
        return {
            "code": int(self.code),
            "message": self.name,
            "data": {"detail": self.message, **self.details},
        }

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"{self.name}: {self.message}"
