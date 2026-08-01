"""Loads raw object source text (PL/SQL bodies, view-defining SQL, trigger
bodies) keyed by node id - ground truth the agent can read directly instead
of reconstructing a formula from graph edges alone (PROMPT.md 5.5's
get_object_metadata already exposes columns/relationships; this exposes the
actual code behind them).
"""

from __future__ import annotations

from oia.graph.model import object_node_id
from oia.storage.sqlite_store import SqliteStore


def load_object_sources(store: SqliteStore) -> dict[str, str]:
    sources: dict[str, str] = {}

    for row in store.query(
        "SELECT owner, object_name, object_type, body FROM raw_source WHERE body IS NOT NULL"
    ):
        sources[object_node_id(row["owner"], row["object_name"])] = row["body"]

    for row in store.query(
        "SELECT owner, view_name, text, is_mview FROM raw_view_text WHERE text IS NOT NULL"
    ):
        kind = "MATERIALIZED VIEW" if row["is_mview"] else "VIEW"
        sources[object_node_id(row["owner"], row["view_name"])] = (
            f"CREATE {kind} {row['owner']}.{row['view_name']} AS\n{row['text']}"
        )

    for row in store.query(
        "SELECT owner, trigger_name, body FROM raw_triggers WHERE body IS NOT NULL"
    ):
        sources[object_node_id(row["owner"], row["trigger_name"])] = row["body"]

    return sources
