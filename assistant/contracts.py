"""Pydantic v2 tool contracts (design.md §1.2.2).

Every input model is strict and forbids extra fields. Identifier formats are
regex-checked. UUIDs must be canonical lowercase UUIDv4.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

TENANT_RE = re.compile(r"^tenant_[a-z0-9_-]{1,56}$")
CHUNK_ID_RE = re.compile(r"^[a-z0-9_-]{1,128}$")
SHA256_HEX_RE = re.compile(r"^[a-f0-9]{64}$")
FACT_KEY_RE = re.compile(r"^[a-z_]{3,64}$")
ACTION_RE = re.compile(r"^[a-z_]{3,48}$")
ANCHOR_RE = re.compile(r"^([A-Z]{2,4}-[0-9]{3,6}|note_[a-z0-9_-]+)$")


def _require_uuid4(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("must be a UUID") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError("must be a canonical lowercase UUIDv4")
    return value


def _require_tenant(value: str) -> str:
    if not TENANT_RE.fullmatch(value):
        raise ValueError("tenant_id does not match ^tenant_[a-z0-9_-]{1,56}$")
    return value


class _Strict(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class HybridRetrieveInput(_Strict):
    request_id: str
    tenant_id: str
    query: str = Field(min_length=3, max_length=512)
    top_k: int = Field(default=50, ge=1, le=50)
    rerank_k: int = Field(default=4, ge=1, le=4)
    threshold: float = Field(default=0.42, ge=0.0, le=1.0)

    _uuid_request = field_validator("request_id")(_require_uuid4)
    _tenant = field_validator("tenant_id")(_require_tenant)


class ChunkOut(_Strict):
    chunk_id: str
    chunk_hash: str
    text: str = Field(max_length=4096)
    bm25: float
    cosine: float
    rrf_rank: int = Field(ge=1, le=40)
    rerank_score: float = Field(ge=0.0, le=1.0)

    @field_validator("chunk_id")
    @classmethod
    def _chunk_id(cls, v: str) -> str:
        if not CHUNK_ID_RE.fullmatch(v):
            raise ValueError("chunk_id format")
        return v

    @field_validator("chunk_hash")
    @classmethod
    def _chunk_hash(cls, v: str) -> str:
        if not SHA256_HEX_RE.fullmatch(v):
            raise ValueError("chunk_hash must be 64 lowercase hex chars")
        return v


class HybridRetrieveOutput(_Strict):
    abstain: bool
    chunks: list[ChunkOut] = Field(max_length=4)
    stage_ms: dict[str, float]


class GraphSubgraphInput(_Strict):
    anchor_id: str
    depth: int = Field(ge=0, le=2)
    max_tokens: int = Field(ge=1, le=320)

    @field_validator("anchor_id")
    @classmethod
    def _anchor(cls, v: str) -> str:
        if not ANCHOR_RE.fullmatch(v):
            raise ValueError("anchor_id format")
        return v


class SessionFactsPutInput(_Strict):
    request_id: str
    tenant_id: str
    session_id: str
    fact_key: str
    fact_value: str = Field(max_length=1024)
    source_chunk_hash: str

    _uuid_request = field_validator("request_id")(_require_uuid4)
    _uuid_session = field_validator("session_id")(_require_uuid4)
    _tenant = field_validator("tenant_id")(_require_tenant)

    @field_validator("fact_key")
    @classmethod
    def _fact_key(cls, v: str) -> str:
        if not FACT_KEY_RE.fullmatch(v):
            raise ValueError("fact_key format")
        return v

    @field_validator("source_chunk_hash")
    @classmethod
    def _hash(cls, v: str) -> str:
        if not SHA256_HEX_RE.fullmatch(v):
            raise ValueError("source_chunk_hash must be SHA-256 hex")
        return v


class Tier3Request(_Strict):
    """Input for workflow_transition and hitl_request (Tier 3 only)."""

    request_id: str
    idempotency_key: str
    tenant_id: str
    action: str
    args: dict[str, Any] = Field(default_factory=dict)
    risk_tier: Literal["TIER_3_MUTATE"]

    _uuid_request = field_validator("request_id")(_require_uuid4)
    _uuid_idem = field_validator("idempotency_key")(_require_uuid4)
    _tenant = field_validator("tenant_id")(_require_tenant)

    @field_validator("action")
    @classmethod
    def _action(cls, v: str) -> str:
        if not ACTION_RE.fullmatch(v):
            raise ValueError("action format")
        return v


class HitlDecideInput(_Strict):
    request_id: str
    tenant_id: str
    reviewer_id: str = Field(min_length=1, max_length=128)
    decision: Literal["APPROVE", "REJECT"]

    _uuid_request = field_validator("request_id")(_require_uuid4)
    _tenant = field_validator("tenant_id")(_require_tenant)
