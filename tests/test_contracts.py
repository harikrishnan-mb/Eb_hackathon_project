import uuid

import pytest
from pydantic import ValidationError

from assistant import contracts

UUID4 = str(uuid.uuid4())
TENANT = "tenant_acme-01"


def test_hybrid_retrieve_defaults():
    m = contracts.HybridRetrieveInput(request_id=UUID4, tenant_id=TENANT, query="lot 44 policy")
    assert (m.top_k, m.rerank_k, m.threshold) == (50, 4, 0.42)


def test_hybrid_retrieve_rejects_extra_fields():
    with pytest.raises(ValidationError):
        contracts.HybridRetrieveInput(request_id=UUID4, tenant_id=TENANT, query="abc", user="x")


def test_strict_mode_rejects_string_for_int():
    with pytest.raises(ValidationError):
        contracts.HybridRetrieveInput(request_id=UUID4, tenant_id=TENANT, query="abc", top_k="10")


def test_tenant_pattern_enforced():
    with pytest.raises(ValidationError):
        contracts.HybridRetrieveInput(request_id=UUID4, tenant_id="Tenant-A", query="abc")


def test_uuid_must_be_v4():
    v1 = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
    with pytest.raises(ValidationError):
        contracts.HybridRetrieveInput(request_id=v1, tenant_id=TENANT, query="abc")


def test_top_k_bounds():
    with pytest.raises(ValidationError):
        contracts.HybridRetrieveInput(request_id=UUID4, tenant_id=TENANT, query="abc", top_k=51)


def test_graph_depth_max_two():
    with pytest.raises(ValidationError):
        contracts.GraphSubgraphInput(anchor_id="SKU-099", depth=3, max_tokens=100)


def test_tier3_requires_risk_tier_literal():
    with pytest.raises(ValidationError):
        contracts.Tier3Request(
            request_id=UUID4, idempotency_key=str(uuid.uuid4()), tenant_id=TENANT,
            action="close_order", risk_tier="TIER_1_READ",
        )
