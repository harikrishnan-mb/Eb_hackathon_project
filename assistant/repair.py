"""Deterministic, regex-only auto-repair (design.md §1.2.4 step 2).

Runs one pass over text fields. No LLM is involved. Every change is recorded
with the original value so the repair is auditable.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any

_SKU = re.compile(r"\bSKU-?(\d{1,3})\b")
_LOT = re.compile(r"\b[Ll][Oo][Tt][ -]?(\d{1,5})\b")


@dataclass(frozen=True)
class Repair:
    field: str
    original: str
    repaired: str


def _repair_text(text: str) -> str:
    text = _SKU.sub(lambda m: f"SKU-{int(m.group(1)):03d}", text)
    text = _LOT.sub(lambda m: f"LOT-{int(m.group(1)):05d}", text)
    return text


def auto_repair(payload: dict[str, Any]) -> tuple[dict[str, Any], list[Repair]]:
    """Return a repaired copy of ``payload`` and the list of repairs made.

    Only string values are touched. Keys are never changed. The input dict is
    not mutated.
    """
    repaired = copy.deepcopy(payload)
    log: list[Repair] = []

    def visit(path: str, value: Any) -> Any:
        if isinstance(value, str):
            new = _repair_text(value)
            if new != value:
                log.append(Repair(field=path, original=value, repaired=new))
            return new
        if isinstance(value, dict):
            return {k: visit(f"{path}.{k}" if path else k, v) for k, v in value.items()}
        if isinstance(value, list):
            return [visit(f"{path}[{i}]", v) for i, v in enumerate(value)]
        return value

    result = visit("", repaired)
    return result, log
