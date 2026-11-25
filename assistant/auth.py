"""Authentication and tenant resolution (design §1.2.4, §6.8 of CLAUDE.md).

The sources do not define an identity provider (open item 9). The default
adopted in design §9 is a pluggable interface. The tenant must come from
verified credentials, never from the request body.

DevHeaderAuthenticator trusts X-Tenant-ID and X-User-ID headers. It exists for
local runs and tests only. It refuses to run when APP_ENV=production.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from .contracts import TENANT_RE
from .errors import ErrorCode, PlatformError


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    user_id: str


class Authenticator(Protocol):
    def authenticate(self, headers: Mapping[str, str]) -> Principal: ...


class DevHeaderAuthenticator:
    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        env = os.environ if env is None else env
        if env.get("APP_ENV", "").lower() == "production":
            raise RuntimeError("DevHeaderAuthenticator must not run in production")

    def authenticate(self, headers: Mapping[str, str]) -> Principal:
        tenant = headers.get("x-tenant-id", "")
        user = headers.get("x-user-id", "")
        if not TENANT_RE.fullmatch(tenant):
            raise PlatformError(ErrorCode.TENANT_FORBIDDEN, "missing or invalid tenant")
        if not user:
            raise PlatformError(ErrorCode.TENANT_FORBIDDEN, "missing user")
        return Principal(tenant_id=tenant, user_id=user)
