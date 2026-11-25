from assistant.retrieval.fusion import rrf


def test_rrf_prefers_items_ranked_high_in_both_lists():
    fused = rrf([["a", "b", "c"], ["b", "a", "d"]])
    assert [i for i, _ in fused[:2]] == ["a", "b"]  # a and b share the top ranks


def test_rrf_score_formula_k60():
    fused = dict(rrf([["x"], ["x"]]))
    assert abs(fused["x"] - 2 / 61) < 1e-12


def test_rrf_dedups_within_a_list():
    fused = dict(rrf([["x", "x", "y"]]))
    assert abs(fused["x"] - 1 / 61) < 1e-12  # counted once, at rank 1


def test_rrf_pool_limit():
    lists = [[f"d{i}" for i in range(100)]]
    assert len(rrf(lists)) == 40
