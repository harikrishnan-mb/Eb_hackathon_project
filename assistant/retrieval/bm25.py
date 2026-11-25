"""Stage 1 sparse retrieval: BM25Okapi over an in-memory inverted index.

Parameters from Gate 3: k1 = 1.25, b = 0.75, top_k = 50, pre-tokenized corpus.

Deviation (recorded in CLAUDE.md §9): the design names PorterStemmer. NLTK is
not installed, so a conservative suffix stripper is used instead. Entity codes
(SKU-### and LOT-#####) are kept intact so they are never stemmed.

Idf follows the Okapi formulation with the usual floor for common terms
(epsilon × mean idf, epsilon = 0.25).
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

from ..repair import _repair_text  # reuse the entity repair rules

K1 = 1.25
B = 0.75
EPSILON = 0.25

STOPWORDS = frozenset(
    "a an and are as at be by do does for from how i in is it of on or our that the this "
    "to was what when where which who why will with you your can".split()
)

_TOKEN = re.compile(r"sku-\d{3}|lot-\d{5}|[a-z0-9]+")


def _stem(token: str) -> str:
    """Strip one common English suffix when at least 3 characters remain."""
    if token.startswith(("sku-", "lot-")) or token.isdigit():
        return token
    for suffix in ("ing", "ed", "es", "s"):
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            return token[: -len(suffix)]
    return token


def tokenize(text: str) -> list[str]:
    # Repair before case-folding: the repair patterns expect uppercase SKU/LOT.
    text = _repair_text(unicodedata.normalize("NFKC", text)).casefold()
    return [_stem(t) for t in _TOKEN.findall(text) if t not in STOPWORDS]


@dataclass(frozen=True)
class Hit:
    doc_id: str
    score: float
    rank: int  # 1-based


class BM25Index:
    def __init__(self, docs: dict[str, str], k1: float = K1, b: float = B) -> None:
        self.k1 = k1
        self.b = b
        self.doc_ids = list(docs)
        self._tf: dict[str, Counter[str]] = {}
        self._len: dict[str, int] = {}
        self._postings: dict[str, set[str]] = {}
        for doc_id, text in docs.items():
            toks = tokenize(text)
            self._tf[doc_id] = Counter(toks)
            self._len[doc_id] = len(toks)
            for t in set(toks):
                self._postings.setdefault(t, set()).add(doc_id)
        n = len(self.doc_ids)
        self._avgdl = (sum(self._len.values()) / n) if n else 0.0
        self._idf: dict[str, float] = {}
        negative: list[str] = []
        total = 0.0
        for term, posting in self._postings.items():
            df = len(posting)
            idf = math.log((n - df + 0.5) / (df + 0.5))
            self._idf[term] = idf
            if idf < 0:
                negative.append(term)
            total += idf
        mean_idf = total / len(self._idf) if self._idf else 0.0
        for term in negative:
            self._idf[term] = EPSILON * mean_idf

    def score(self, query: str) -> dict[str, float]:
        scores: dict[str, float] = {d: 0.0 for d in self.doc_ids}
        for term in set(tokenize(query)):
            posting = self._postings.get(term)
            if not posting:
                continue
            idf = self._idf[term]
            for d in posting:
                tf = self._tf[d][term]
                denom = tf + self.k1 * (1 - self.b + self.b * self._len[d] / (self._avgdl or 1.0))
                scores[d] += idf * tf * (self.k1 + 1) / denom
        return scores

    def search(self, query: str, top_k: int = 50) -> list[Hit]:
        scores = self.score(query)
        ranked = sorted(
            ((d, s) for d, s in scores.items() if s > 0.0),
            key=lambda x: (-x[1], x[0]),
        )[:top_k]
        return [Hit(doc_id=d, score=s, rank=i + 1) for i, (d, s) in enumerate(ranked)]
