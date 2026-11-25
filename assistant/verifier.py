"""Verification step (Gate 7 "Verification" subagent, CLAUDE.md §8). Deterministic.

Each sentence of a drafted answer must be supported by the cited chunks: at least
SUPPORT (60%) of its content words must appear in the chunk text. Unsupported sentences are
removed (the "veto"). If nothing survives, the caller abstains.

This is a lexical proxy, not NLI. It catches sentences that add facts absent from the sources.
It does not check whether a supported sentence is correct, and paraphrased support can fail.
"""

from __future__ import annotations

import re

from .retrieval.bm25 import STOPWORDS, tokenize

SUPPORT = 0.6
_CITE = re.compile(r"\[[^\]]*\]")
_SENT = re.compile(r"(?<=[.!?])\s+|\n+")


def _content(text: str) -> set[str]:
    return {t for t in tokenize(text) if len(t) >= 4 and t not in STOPWORDS}


def verify(answer: str, chunk_texts: list[str]) -> tuple[str, list[str]]:
    """Return (kept_answer, removed_sentences)."""
    source = _content(" ".join(chunk_texts))
    kept, removed = [], []
    for raw in _SENT.split(answer.strip()):
        sentence = _CITE.sub("", raw).strip()
        if not sentence:
            continue
        words = _content(sentence)
        if not words:  # no content words (e.g. "Yes."): keep it
            kept.append(raw.strip())
            continue
        if len(words & source) / len(words) >= SUPPORT:
            kept.append(raw.strip())
        else:
            removed.append(sentence)
    return " ".join(kept), removed
