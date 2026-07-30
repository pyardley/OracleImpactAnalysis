"""Shared helpers for DDL and PL/SQL lineage parsing (both built on sqlglot)."""

from __future__ import annotations

from collections import defaultdict

from oia.storage.sqlite_store import SqliteStore


def schema_by_owner(store: SqliteStore) -> dict[str, dict[str, dict[str, str]]]:
    """owner -> table/view -> column -> data_type, for sqlglot's `schema=` kwarg."""
    schema: dict[str, dict[str, dict[str, str]]] = defaultdict(lambda: defaultdict(dict))
    for row in store.query("SELECT owner, object_name, column_name, data_type FROM raw_columns"):
        schema[row["owner"]][row["object_name"]][row["column_name"]] = row["data_type"] or "VARCHAR2"
    return schema


def lineage_leaves(node) -> list:
    """Walks a sqlglot.lineage.Node tree down to its base-table references."""
    out = []
    stack = [node]
    while stack:
        current = stack.pop()
        if current.downstream:
            stack.extend(current.downstream)
        elif type(current.source).__name__ == "Table":
            out.append(current)
    return out


def table_ref(table_expr, default_owner: str) -> tuple[str, str]:
    owner = getattr(table_expr, "db", "") or default_owner
    name = table_expr.name
    return owner, name
