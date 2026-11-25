"""Tenant-isolated cache keys (design.md ADR-03, Gate 11).

Key = SHA256(tenant_id ":" user_id ":" norm_query), with the tenant prefix
enforced. norm_query is undefined in the sources (C25). Default adopted in
design §9: NFKC fold, case-fold, whitespace collapse.

TTL is 300 s ± 45 s jitter (Gate 11). The random source is injectable for tests.
"""

from __future__ import annotations

import hashlib
import random
import re
import unicodedata

from .contracts import TENANT_RE

TTL_BASE_S = 300
TTL_JITTER_S = 45


def normalize_query(query: str) -> str:
    folded = unicodedata.normalize("NFKC", query).casefold()
    return " ".join(folded.split())


def cache_key(tenant_id: str, user_id: str, query: str) -> str:
    if not TENANT_RE.fullmatch(tenant_id):
        raise ValueError("invalid tenant_id")
    if not user_id or not re.fullmatch(r"[A-Za-z0-9_.@-]{1,128}", user_id):
        raise ValueError("invalid user_id")
    digest = hashlib.sha256(
        f"{tenant_id}:{user_id}:{normalize_query(query)}".encode("utf-8")
    ).hexdigest()
    # The tenant prefix makes cross-tenant key collisions visible in key scans.
    return f"{tenant_id}:cache:{digest}"


def ttl_seconds(rng: random.Random | None = None) -> int:
    rng = rng or random.Random()
    return TTL_BASE_S + rng.randint(-TTL_JITTER_S, TTL_JITTER_S)
