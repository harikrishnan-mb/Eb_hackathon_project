"""Context control (Gate 5, CLAUDE.md §8).

Observation compression: each retrieved chunk is cut to MAX_OBS_TOKENS before it enters the
prompt. Tokens are approximated by whitespace words (no tokenizer is loaded here), so the
cap is approximate. Boilerplate headings such as "# Title" are dropped.
"""

from __future__ import annotations

import re

MAX_OBS_TOKENS = 180
_HEADING = re.compile(r"^\s*#+\s.*$", re.MULTILINE)


def compress_observation(text: str, max_tokens: int = MAX_OBS_TOKENS) -> str:
    body = _HEADING.sub("", text).strip()
    words = body.split()
    if len(words) <= max_tokens:
        return " ".join(words)
    return " ".join(words[:max_tokens]) + " …"
