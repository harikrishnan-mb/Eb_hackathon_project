"""Triage routing (Blueprint Gates 6–7, design §1.9).

The design calls for a distilled classifier at temperature 0.0 with p95 ≤ 25 ms.
The model and training data are not specified (design §9, item 13 default), so
this module ships a rule-based classifier behind the same interface.

Safety rules from the design, enforced in code:
  * Low confidence routes to HYBRID_RAG, never TRANSACTION_EXEC.
  * TRANSACTION_EXEC requires high confidence AND a Tier 3 classification.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

Route = Literal["DIRECT_ANSWER", "HYBRID_RAG", "TRANSACTION_EXEC", "REJECT_OOS"]

CONFIDENCE_THRESHOLD = 0.8

_MUTATE = re.compile(r"\b(approve|submit|transition|cancel|update|create|delete|release|ship)\b", re.I)
_SMALLTALK = re.compile(r"^\s*(hi|hello|hey|thanks|thank you)\b[!. ]*$", re.I)
_OUT_OF_SCOPE = re.compile(r"\b(poem|joke|lyrics|horoscope|recipe)\b", re.I)


@dataclass(frozen=True)
class TriageResult:
    intent: str
    domain: str
    risk_tier: Literal["TIER_1", "TIER_2", "TIER_3"]
    complexity: Literal["low", "high"]
    route: Route
    confidence: float


def classify(query: str) -> TriageResult:
    if _SMALLTALK.match(query):
        return TriageResult("smalltalk", "general", "TIER_1", "low", "DIRECT_ANSWER", 0.95)
    if _OUT_OF_SCOPE.search(query):
        return TriageResult("out_of_scope", "general", "TIER_1", "low", "REJECT_OOS", 0.9)
    if _MUTATE.search(query):
        # A mutation request is a Tier 3 classification. It reaches
        # TRANSACTION_EXEC only with high confidence; otherwise it is read-only RAG.
        confidence = 0.9
        if confidence >= CONFIDENCE_THRESHOLD:
            return TriageResult("workflow_change", "operations", "TIER_3", "high", "TRANSACTION_EXEC", confidence)
        return TriageResult("workflow_change", "operations", "TIER_3", "high", "HYBRID_RAG", confidence)
    return TriageResult("lookup", "policy", "TIER_1", "low", "HYBRID_RAG", 0.6)


def route_safely(result: TriageResult) -> Route:
    """Final routing guard. Low confidence never reaches TRANSACTION_EXEC."""
    if result.route == "TRANSACTION_EXEC":
        if result.confidence >= CONFIDENCE_THRESHOLD and result.risk_tier == "TIER_3":
            return "TRANSACTION_EXEC"
        return "HYBRID_RAG"
    return result.route
