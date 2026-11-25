"""Tier model and subagent ceilings (design.md §1.2.1, §1.2.4 step 3).

Tier order: TIER_1_READ < TIER_2_COMPUTE < TIER_3_MUTATE.

TIER_2_COMPUTE is part of the order but no tool is assigned to it, because the
sources never define it (open item C24). Tools are assigned tiers below; the
assignments marked "DD" are design-draft decisions, not Blueprint text.
"""

from __future__ import annotations

from enum import IntEnum


class Tier(IntEnum):
    TIER_1_READ = 1
    TIER_2_COMPUTE = 2
    TIER_3_MUTATE = 3


class Subagent:
    RETRIEVAL = "retrieval"
    EXECUTION = "execution"
    VERIFICATION = "verification"


# Ceiling per subagent. Retrieval is read-only (Blueprint Gate 7).
# Execution may reach Tier 3, but only through the HITL gate (Gate 9.4).
# Verification may only append to the audit log (Gate 7, "audit log only").
SUBAGENT_CEILING: dict[str, Tier] = {
    Subagent.RETRIEVAL: Tier.TIER_1_READ,
    Subagent.EXECUTION: Tier.TIER_3_MUTATE,
    Subagent.VERIFICATION: Tier.TIER_1_READ,
}

# Tool tier assignments. audit_append is assigned TIER_1 (DD: the sources name
# no tier for it; it is an append-only write).
TOOL_TIER: dict[str, Tier] = {
    "hybrid_retrieve": Tier.TIER_1_READ,
    "graph_subgraph": Tier.TIER_1_READ,
    "session_facts_get": Tier.TIER_1_READ,
    "session_facts_put": Tier.TIER_1_READ,
    "audit_append": Tier.TIER_1_READ,
    "workflow_transition": Tier.TIER_3_MUTATE,
    "hitl_request": Tier.TIER_3_MUTATE,
    "hitl_decide": Tier.TIER_3_MUTATE,
}


def is_allowed(subagent: str, tool: str) -> bool:
    """True if ``subagent``'s ceiling covers ``tool``'s tier."""
    if subagent not in SUBAGENT_CEILING or tool not in TOOL_TIER:
        return False
    return SUBAGENT_CEILING[subagent] >= TOOL_TIER[tool]
