"""Column-level lineage for views/materialized views via sqlglot (PROMPT.md 5.2).

Every output column of every view is resolved independently with
sqlglot.lineage.lineage(), walked down to its base-table leaves. A column
that fails to parse (unsupported Oracle syntax, sqlglot bug, etc.) is simply
skipped - never guessed - and counted as a parse error so `oia graph stats`
can surface it; the object-level REFERENCES edge from ALL_DEPENDENCIES still
covers that view at coarser granularity regardless.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from sqlglot.lineage import lineage as sqlglot_lineage

from oia.graph.model import Edge, column_node_id, object_node_id
from oia.lineage._common import lineage_leaves, schema_by_owner
from oia.storage.sqlite_store import SqliteStore

logger = logging.getLogger("oia.lineage.ddl")


def build_ddl_lineage_edges(
    store: SqliteStore, node_ids: set[str], only_objects: set[tuple[str, str]] | None = None
) -> tuple[list[Edge], int]:
    """`only_objects`, when given, restricts (re-)parsing to that set of (owner,
    view_name) keys - the incremental-refresh path (PROMPT.md 5.1); the caller is
    responsible for carrying forward prior edges for whatever's excluded."""
    edges: list[Edge] = []
    parse_errors = 0

    owner_schemas = schema_by_owner(store)

    columns_by_object: dict[tuple[str, str], list[str]] = defaultdict(list)
    for row in store.query("SELECT owner, object_name, column_name FROM raw_columns"):
        columns_by_object[(row["owner"], row["object_name"])].append(row["column_name"])

    view_rows = store.query("SELECT owner, view_name, text FROM raw_view_text WHERE text IS NOT NULL")

    for view_row in view_rows:
        owner, view_name, sql_text = view_row["owner"], view_row["view_name"], view_row["text"]
        if only_objects is not None and (owner, view_name) not in only_objects:
            continue
        if not sql_text or not sql_text.strip():
            continue
        dst_object_id = object_node_id(owner, view_name)
        if dst_object_id not in node_ids:
            continue

        # Unqualified table refs in a view's stored DDL resolve within the view's own
        # schema for a single-schema setup like RetailDemo; cross-schema views need
        # explicit owner-qualified refs (handled via leaf.source.db below) to resolve.
        schema = dict(owner_schemas.get(owner, {}))

        for column in columns_by_object.get((owner, view_name), []):
            dst_col_id = column_node_id(owner, view_name, column)
            if dst_col_id not in node_ids:
                continue
            try:
                root = sqlglot_lineage(column, sql_text, schema=schema, dialect="oracle")
            except Exception as exc:  # sqlglot raises a variety of error types
                logger.debug("DDL lineage parse failed for %s.%s: %s", dst_col_id, column, exc)
                parse_errors += 1
                continue

            for leaf in lineage_leaves(root):
                src_owner = leaf.source.db or owner
                src_table = leaf.source.name
                src_column = leaf.name.split(".")[-1]
                src_id = column_node_id(src_owner, src_table, src_column)
                if src_id not in node_ids or src_id == dst_col_id:
                    continue
                edges.append(
                    Edge(
                        edge_type="DERIVED_FROM",
                        src_node_id=dst_col_id,
                        dst_node_id=src_id,
                        confidence="high",
                        method="ddl_parse",
                        source_object=dst_object_id,
                        transform_expression=(
                            leaf.expression.sql(dialect="oracle") if leaf.expression is not None else None
                        ),
                    )
                )

    return edges, parse_errors
