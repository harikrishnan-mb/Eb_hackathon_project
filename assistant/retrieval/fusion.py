"""Stage 2b rank fusion: Reciprocal Rank Fusion (Gate 3, ADR-01).

score(d) = sum over lists of 1 / (k + rank(d)), with k = 60.
Candidates are deduplicated by chunk id. The fused pool is cut to 40
(design §1.9, Gate 3).
"""

from __future__ import annotations

RRF_K = 60
POOL = 40


def rrf(ranked_lists: list[list[str]], k: int = RRF_K, pool: int = POOL) -> list[tuple[str, float]]:
    """Fuse ranked id lists. Ids are dedup'd by identity. Returns (id, score) best first."""
    scores: dict[str, float] = {}
    for ranking in ranked_lists:
        seen: set[str] = set()
        for rank, item in enumerate(ranking, start=1):
            if item in seen:  # dedup within a list
                continue
            seen.add(item)
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    ordered = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return ordered[:pool]
