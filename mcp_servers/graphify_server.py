"""MCP server: graphify-vault (CLAUDE.md §5, §6.2, §15; mcp_config.json "graphify-vault").

Transport: stdio. Tool: graph_subgraph. Tier: TIER_1_READ.

The graph is built from [[wikilinks]] between notes in knowledge_vault/. This is a
small built-in graph, not the Graphify tool (the Graphify CLI is not installed).
Traversal is breadth-first, at most depth 2, and the output is cut to max_tokens
(token estimate: characters / 4).

Anchor forms (contract §6.2): note_<slug>, or an entity code SKU-### / LOT-#####
which maps to note_sku_### / note_lot_#####.
"""

from __future__ import annotations

import os
import re
import sys
from collections import deque
from pathlib import Path

from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcp.server.fastmcp import FastMCP  # noqa: E402
from pydantic import BaseModel, Field, field_validator  # noqa: E402

mcp = FastMCP("graphify-vault")

_LINK = re.compile(r"\[\[([^\]|#]+)")
_ANCHOR = re.compile(r"^(note_[a-z0-9_-]+|[A-Z]{2,4}-[0-9]{3,6})$")
_ENTITY = re.compile(r"^([A-Z]{2,4})-([0-9]{3,6})$")


class GraphInput(BaseModel, extra="forbid"):
    anchor_id: str
    depth: int = Field(ge=0, le=2)
    max_tokens: int = Field(ge=1, le=320)

    @field_validator("anchor_id")
    @classmethod
    def _anchor(cls, v: str) -> str:
        if not _ANCHOR.match(v):
            raise ValueError("anchor_id must be note_<slug> or SKU-### / LOT-#####")
        return v


def _vault_dir() -> Path:
    return Path(os.environ.get("CORPUS_DIR", str(ROOT / "knowledge_vault")))


def _load_graph(vault: Path) -> tuple[dict[str, str], dict[str, set[str]]]:
    texts: dict[str, str] = {}
    edges: dict[str, set[str]] = {}
    for p in sorted(vault.glob("*.md")):
        node = p.stem
        body = p.read_text(encoding="utf-8").split("---", 2)[-1].strip()
        texts[node] = body
        edges.setdefault(node, set())
        for target in _LINK.findall(body):
            t = target.strip()
            edges[node].add(t)
            edges.setdefault(t, set()).add(node)  # treat links as undirected
    return texts, edges


def _anchor_node(anchor: str) -> str:
    m = _ENTITY.match(anchor)
    if m:
        return f"note_{m.group(1).lower()}_{m.group(2)}"
    return anchor


def graph_subgraph(anchor_id: str, depth: int = 1, max_tokens: int = 320) -> dict:
    """Return the connected subgraph around an anchor, cut to max_tokens."""
    try:
        args = GraphInput(anchor_id=anchor_id, depth=depth, max_tokens=max_tokens)
    except ValidationError as exc:
        return {"error": {"code": -32602, "name": "E_VALIDATION", "detail": exc.errors(include_input=False)}}

    texts, edges = _load_graph(_vault_dir())
    start = _anchor_node(args.anchor_id)
    if start not in texts:
        return {"error": {"code": -32602, "name": "E_VALIDATION", "detail": "anchor not found in vault"}}

    seen = {start: 0}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        if seen[node] == args.depth:
            continue
        for nxt in sorted(edges.get(node, ())):
            if nxt in texts and nxt not in seen:
                seen[nxt] = seen[node] + 1
                queue.append(nxt)

    nodes, used = [], 0
    for node in sorted(seen, key=lambda n: (seen[n], n)):
        text = texts[node][:800]
        cost = max(1, len(text) // 4)
        if used + cost > args.max_tokens:
            break
        used += cost
        nodes.append({"id": node, "depth": seen[node], "text": text})
    kept = {n["id"] for n in nodes}
    out_edges = sorted(
        (a, b) for a in kept for b in edges.get(a, ()) if b in kept and a < b
    )
    return {
        "nodes": nodes,
        "edges": [{"source": a, "target": b} for a, b in out_edges],
        "token_estimate": used,
    }


mcp.tool(name="graph_subgraph")(graph_subgraph)

if __name__ == "__main__":
    mcp.run()
