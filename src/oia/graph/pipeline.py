"""Orchestrates graph compilation: object-level graph -> DDL lineage -> PL/SQL
procedural lineage -> manual overrides -> persisted to SQLite. Object nodes and
REFERENCES/CALLS/FK edges are always fully rebuilt (cheap - plain SQL scans, no
parsing); DDL/PL-SQL lineage parsing is the expensive step incremental mode
skips for objects whose LAST_DDL_TIME hasn't changed since the last run,
reusing their previously-computed edges instead (PROMPT.md 5.1).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from oia.config.settings import Settings
from oia.graph.builder import build_object_graph
from oia.graph.model import Edge, Node, edge_from_row, save_graph
from oia.lineage.ddl_lineage import build_ddl_lineage_edges
from oia.lineage.overrides import load_override_edges
from oia.lineage.plsql_lineage import build_plsql_lineage_edges
from oia.storage.sqlite_store import SqliteStore


@dataclass
class GraphBuildStats:
    node_count: int = 0
    edge_count: int = 0
    edges_by_confidence: dict[str, int] | None = None
    parse_errors: int = 0
    objects_reparsed: int = 0
    objects_reused: int = 0


# Object types whose lineage parsing (view/mview DDL, PL/SQL bodies) is worth
# skipping when unchanged. Everything else (TABLE, SYNONYM, ...) has no lineage
# of its own to parse in the first place.
_PARSEABLE_TYPES = ("VIEW", "MATERIALIZED VIEW", "PROCEDURE", "FUNCTION", "PACKAGE BODY", "TRIGGER")


def _current_ddl_times(store: SqliteStore) -> dict[tuple[str, str, str], str | None]:
    rows = store.query(
        f"SELECT owner, object_name, object_type, last_ddl_time FROM raw_objects "
        f"WHERE object_type IN ({','.join('?' * len(_PARSEABLE_TYPES))})",
        _PARSEABLE_TYPES,
    )
    current = {(r["owner"], r["object_name"], r["object_type"]): r["last_ddl_time"] for r in rows}
    # Triggers aren't in OBJECT_TYPES_OF_INTEREST's dependency chain but do live in
    # raw_objects as TRIGGER type already, so the query above covers them too.
    return current


def _changed_objects(store: SqliteStore) -> set[tuple[str, str]]:
    """(owner, object_name) pairs whose LAST_DDL_TIME differs from what we last
    recorded, or that are new since the last extraction_state snapshot."""
    prior = store.get_extraction_state()
    current = _current_ddl_times(store)
    changed = {
        (owner, name)
        for (owner, name, otype), ddl_time in current.items()
        if prior.get((owner, name, otype)) != ddl_time
    }
    return changed


def _reused_edges(store: SqliteStore, unchanged: set[tuple[str, str]]) -> list[Edge]:
    from oia.graph.model import object_node_id

    reused: list[Edge] = []
    for owner, name in unchanged:
        source_object = object_node_id(owner, name)
        rows = store.query("SELECT * FROM graph_edges WHERE source_object = ?", (source_object,))
        reused.extend(edge_from_row(r) for r in rows)
    return reused


def build_full_graph(settings: Settings, store: SqliteStore, incremental: bool = False) -> GraphBuildStats:
    obj_nodes, obj_edges = build_object_graph(store, settings)
    node_ids = {n.node_id for n in obj_nodes}

    only_objects: set[tuple[str, str]] | None = None
    reused: list[Edge] = []
    objects_reused = 0
    if incremental:
        current = _current_ddl_times(store)
        all_keys = {(o, n) for (o, n, _t) in current}
        changed = _changed_objects(store)
        unchanged = all_keys - changed
        only_objects = changed
        reused = _reused_edges(store, unchanged)
        objects_reused = len(unchanged)

    ddl_edges, ddl_errors = build_ddl_lineage_edges(store, node_ids, only_objects=only_objects)
    plsql_edges, plsql_errors = build_plsql_lineage_edges(store, node_ids, only_objects=only_objects)
    override_edges = load_override_edges(settings, node_ids)

    all_edges: list[Edge] = [*obj_edges, *ddl_edges, *plsql_edges, *reused, *override_edges]
    nodes: list[Node] = obj_nodes

    save_graph(store, nodes, all_edges)

    # Refresh extraction_state to the current LAST_DDL_TIME for every parseable
    # object, and drop state for anything that's disappeared since.
    now = datetime.now(UTC).isoformat()
    current = _current_ddl_times(store)
    store.set_extraction_state([(o, n, t, ddl, now) for (o, n, t), ddl in current.items()])
    store.prune_extraction_state(set(current))

    by_confidence: dict[str, int] = {}
    for e in all_edges:
        by_confidence[e.confidence] = by_confidence.get(e.confidence, 0) + 1

    return GraphBuildStats(
        node_count=len(nodes),
        edge_count=len(all_edges),
        edges_by_confidence=by_confidence,
        parse_errors=ddl_errors + plsql_errors,
        objects_reparsed=len(only_objects) if only_objects is not None else len(_current_ddl_times(store)),
        objects_reused=objects_reused,
    )
