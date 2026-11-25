from assistant.retrieval.bm25 import BM25Index, tokenize


def test_entity_codes_survive_tokenization_and_are_repaired():
    toks = tokenize("Check SKU99 and lot 44")
    assert "sku-099" in toks and "lot-00044" in toks


def test_stopwords_removed():
    assert "the" not in tokenize("the returns policy")


def test_exact_entity_match_ranks_first():
    idx = BM25Index({
        "a": "Storage temperature for SKU-015 is 2 to 8 degrees.",
        "b": "Refund approval limits for all regions.",
        "c": "Warranty periods for cookware.",
    })
    hits = idx.search("temperature of SKU-015", top_k=3)
    assert hits[0].doc_id == "a"
    assert [h.rank for h in hits] == list(range(1, len(hits) + 1))


def test_unmatched_query_returns_no_hits():
    idx = BM25Index({"a": "returns policy"})
    assert idx.search("zebra submarine", top_k=5) == []


def test_top_k_truncates():
    docs = {str(i): f"shipping note {i}" for i in range(10)}
    assert len(BM25Index(docs).search("shipping", top_k=4)) == 4
