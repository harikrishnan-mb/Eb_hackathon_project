import random

import pytest

from assistant.cache import cache_key, normalize_query, ttl_seconds
from assistant.repair import auto_repair


def test_sku_repair_pads_to_three_digits():
    out, log = auto_repair({"query": "check SKU99 and SKU-7"})
    assert out["query"] == "check SKU-099 and SKU-007"
    assert len(log) == 1 and log[0].original == "check SKU99 and SKU-7"


def test_lot_repair():
    out, _ = auto_repair({"query": "status of lot 44"})
    assert out["query"] == "status of LOT-00044"


def test_repair_does_not_touch_keys_or_mutate_input():
    raw = {"query": "SKU99", "SKU99": "keep"}
    out, _ = auto_repair(raw)
    assert "SKU99" in out and raw["query"] == "SKU99"


def test_normalize_folds_unicode_case_and_whitespace():
    assert normalize_query("  Ｌｏｔ   44\tPolicy ") == "lot 44 policy"


def test_cache_key_is_tenant_scoped_and_deterministic():
    a = cache_key("tenant_a", "u1", "Lot 44")
    b = cache_key("tenant_a", "u1", "lot   44")
    c = cache_key("tenant_b", "u1", "lot 44")
    assert a == b
    assert a != c
    assert a.startswith("tenant_a:cache:") and c.startswith("tenant_b:cache:")


def test_cache_key_rejects_bad_tenant():
    with pytest.raises(ValueError):
        cache_key("TenantA", "u1", "q")


def test_ttl_within_jitter_band():
    rng = random.Random(0)
    values = [ttl_seconds(rng) for _ in range(500)]
    assert min(values) >= 255 and max(values) <= 345
