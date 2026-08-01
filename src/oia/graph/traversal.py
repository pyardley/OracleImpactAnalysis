"""Upstream trace / downstream impact traversal over the compiled graph (PROMPT.md 5.4).

Both directions share one BFS: upstream trace walks OUT-edges of
DERIVED_FROM/READS_FROM (edge direction is "derived thing -> the thing it's
derived from" - PROMPT.md 5.3), so following out-edges walks toward sources.
Downstream impact walks IN-edges of every edge type from the changed object
*and* all of its column nodes at once, which gives object-level impact
(REFERENCES/CALLS/WRITES_TO into the object) and column-level impact
(DERIVED_FROM into its columns) in a single pass, degrading gracefully
wherever column-level edges don't exist.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import networkx as nx

LOW_CONFIDENCE = {"low", "none"}
LINEAGE_EDGE_TYPES = {"DERIVED_FROM", "READS_FROM"}


@dataclass
class PathStep:
    edge_type: str
    src: str
    dst: str
    confidence: str
    method: str
    source_object: str | None = None
    transform_expression: str | None = None
    filter_expression: str | None = None


@dataclass
class TraversalResult:
    start_nodes: list[str] = field(default_factory=list)
    visited: dict[str, list[PathStep]] = field(default_factory=dict)  # node_id -> path of steps from a start
    expanded: set[str] = field(default_factory=set)  # nodes with >=1 qualifying edge, regardless of where it led
    frontier_cut_off: list[str] = field(default_factory=list)  # nodes at max_depth, not fully explored
    incomplete: bool = False
    incomplete_reasons: list[str] = field(default_factory=list)
    cycles: list[list[str]] = field(default_factory=list)


def _bfs(
    g: nx.MultiDiGraph,
    starts: list[str],
    direction: str,
    max_depth: int,
    edge_types: set[str] | None,
) -> TraversalResult:
    starts = [s for s in starts if s in g]
    result = TraversalResult(start_nodes=starts)
    visited: set[str] = set(starts)
    # (node, path_steps, ancestor_set) - ancestor_set is this path's own lineage,
    # distinct from `visited` (which dedups across the whole traversal).
    frontier: list[tuple[str, list[PathStep], frozenset[str]]] = [(s, [], frozenset({s})) for s in starts]
    depth = 0
    edge_fn = g.out_edges if direction == "out" else g.in_edges

    while frontier and depth < max_depth:
        next_frontier: list[tuple[str, list[PathStep], frozenset[str]]] = []
        for node, path, ancestors in frontier:
            qualifying = [
                (u, v, data)
                for u, v, data in edge_fn(node, data=True)
                if not edge_types or data["edge_type"] in edge_types
            ]
            if qualifying:
                result.expanded.add(node)
            for u, v, data in qualifying:
                other = v if direction == "out" else u
                step = PathStep(
                    edge_type=data["edge_type"],
                    src=u,
                    dst=v,
                    confidence=data["confidence"],
                    method=data["method"],
                    source_object=data.get("source_object"),
                    transform_expression=data.get("transform_expression"),
                    filter_expression=data.get("filter_expression"),
                )
                if data["confidence"] in LOW_CONFIDENCE:
                    result.incomplete = True
                    result.incomplete_reasons.append(
                        f"{u} -> {v} has {data['confidence']}-confidence lineage ({data['method']})"
                    )
                if other in ancestors:
                    result.cycles.append([*list(ancestors), other])
                    continue
                if other in visited:
                    continue
                visited.add(other)
                new_path = [*path, step]
                result.visited[other] = new_path
                next_frontier.append((other, new_path, ancestors | {other}))
        frontier = next_frontier
        depth += 1

    if frontier:
        result.frontier_cut_off = [n for n, _, _ in frontier]

    return result


def trace_upstream(g: nx.MultiDiGraph, start: str, max_depth: int = 10) -> TraversalResult:
    """Traces a COLUMN node back to its base-table sources."""
    return _bfs(g, [start], direction="out", max_depth=max_depth, edge_types=LINEAGE_EDGE_TYPES)


def impact_downstream(g: nx.MultiDiGraph, node_id: str, max_depth: int = 10) -> TraversalResult:
    """Finds everything downstream of a table/view/procedure/column - object-level
    and column-level impact together (see module docstring)."""
    if node_id not in g:
        return TraversalResult(start_nodes=[])
    node_data = g.nodes[node_id]
    starts = [node_id]
    if node_data.get("node_type") != "COLUMN":
        owner, obj = node_data.get("owner"), node_data.get("object_name")
        starts += [
            n
            for n, d in g.nodes(data=True)
            if d.get("node_type") == "COLUMN" and d.get("owner") == owner and d.get("object_name") == obj
        ]
    return _bfs(g, starts, direction="in", max_depth=max_depth, edge_types=None)


def sources_of(result: TraversalResult) -> list[str]:
    """Nodes reached (or started from) with no further upstream lineage edges of
    their own - i.e. genuine base columns, not just depth-limited cut-offs."""
    all_nodes = set(result.start_nodes) | set(result.visited)
    return sorted(n for n in all_nodes if n not in result.expanded and n not in result.frontier_cut_off)


def affected_objects(g: nx.MultiDiGraph, result: TraversalResult) -> list[str]:
    """De-duplicated OBJECT-level node ids touched by an impact traversal (a COLUMN
    node's owning object counts too, so column-level hits still roll up to their object)."""
    objects: set[str] = set()
    for node_id in result.visited:
        data = g.nodes[node_id]
        if data.get("node_type") == "COLUMN":
            objects.add(f"{data['owner']}.{data['object_name']}")
        else:
            objects.add(node_id)
    return sorted(objects)
