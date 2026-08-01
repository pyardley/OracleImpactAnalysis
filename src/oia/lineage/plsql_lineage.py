"""Column-level (INSERT...SELECT) and object-level (everything else) lineage
for PL/SQL procedures, functions, packages, and triggers (PROMPT.md 5.2).

Column-level derivation is only attempted for INSERT ... SELECT - the one
procedural pattern where "target column <- source expression" is unambiguous
without simulating control flow. UPDATE/DELETE/MERGE and INSERT ... VALUES
still produce object-level READS_FROM/WRITES_TO edges (we know *what* a
statement touches even when we don't derive column-level data flow from it) -
see PROMPT.md's "No PL/SQL symbolic execution" non-goal for why this stops
here in v1; the ANTLR-based v2 harvester is the documented path to do better.

Every EXECUTE IMMEDIATE with a non-literal argument becomes an
`unresolved_lineage` row rather than a guessed edge - surfaced via
`oia graph stats` and the `get_unresolved_lineage` agent tool.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import UTC, datetime

import sqlglot
from sqlglot import exp
from sqlglot.lineage import lineage as sqlglot_lineage

from oia.graph.model import Edge, column_node_id, object_node_id
from oia.lineage._common import lineage_leaves, schema_by_owner, summarize_path, table_ref
from oia.lineage.plsql_statements import harvest_statements
from oia.storage.sqlite_store import SqliteStore

logger = logging.getLogger("oia.lineage.plsql")


def _unwrap_table(node) -> exp.Table | None:
    if isinstance(node, exp.Table):
        return node
    inner = getattr(node, "this", None)
    return inner if isinstance(inner, exp.Table) else None


def _statement_filter_expression(parsed: exp.Expression, dialect: str = "oracle") -> str | None:
    """WHERE / JOIN...ON / MERGE...ON conditions on a statement - eligibility
    criteria (e.g. "only completed orders from active customers") that a
    plain column-to-column edge wouldn't otherwise reveal."""
    parts: list[str] = []
    where = parsed.args.get("where")
    if where is not None:
        parts.append(where.sql(dialect=dialect))
    if isinstance(parsed, exp.Merge):
        on = parsed.args.get("on")
        if on is not None:
            parts.append(f"ON {on.sql(dialect=dialect)}")
    for join in parsed.args.get("joins") or []:
        on = join.args.get("on")
        if on is not None:
            parts.append(f"JOIN ON {on.sql(dialect=dialect)}")
    return " AND ".join(parts) if parts else None


def _reads_from_edges(
    select_like, owner: str, unit_id: str, node_ids: set[str], exclude: str | None, filter_expr: str | None = None
) -> list[Edge]:
    edges = []
    seen: set[str] = set()
    for tbl in select_like.find_all(exp.Table):
        s_owner, s_name = table_ref(tbl, owner)
        tid = object_node_id(s_owner, s_name)
        if tid not in node_ids or tid == unit_id or tid == exclude or tid in seen:
            continue
        seen.add(tid)
        edges.append(
            Edge(
                edge_type="READS_FROM",
                src_node_id=unit_id,
                dst_node_id=tid,
                confidence="low",
                method="plsql_static_analysis",
                source_object=unit_id,
                filter_expression=filter_expr,
            )
        )
    return edges


def _insert_select_column_edges(
    insert: exp.Insert,
    owner: str,
    t_owner: str,
    t_name: str,
    target_columns: list[str] | None,
    node_ids: set[str],
    owner_schemas: dict,
    unit_id: str,
) -> list[Edge]:
    select = insert.expression
    if not isinstance(select, exp.Select) or not target_columns:
        return []
    output_cols = list(select.selects)
    if len(target_columns) != len(output_cols):
        return []

    # INSERT ... SELECT doesn't require the select-list to alias its
    # expressions (positional correspondence with the INSERT's own column
    # list is enough) - but sqlglot.lineage.lineage() looks a column up by
    # name in the query, so an unaliased computed expression (e.g. a bare
    # `CASE WHEN ... END`) would resolve to nothing. Force every output
    # expression to carry its real target-column name as an explicit alias
    # in a throwaway copy before calling lineage(), rather than trusting
    # whatever alias (or lack of one) the original SQL happened to have.
    aliased_select = select.copy()
    aliased_select.set(
        "expressions",
        [
            exp.alias_((e.this if isinstance(e, exp.Alias) else e).copy(), target_col, copy=False)
            for target_col, e in zip(target_columns, output_cols, strict=True)
        ],
    )

    schema = dict(owner_schemas.get(owner, {}))
    select_sql = aliased_select.sql(dialect="oracle")
    edges: list[Edge] = []
    for target_col in target_columns:
        dst_col_id = column_node_id(t_owner, t_name, target_col)
        if dst_col_id not in node_ids:
            continue
        try:
            root = sqlglot_lineage(target_col, select_sql, schema=schema, dialect="oracle")
        except Exception as exc:
            logger.debug("INSERT..SELECT column lineage failed for %s: %s", dst_col_id, exc)
            continue
        for leaf, path in lineage_leaves(root):
            src_owner = leaf.source.db or owner
            src_id = column_node_id(src_owner, leaf.source.name, leaf.name.split(".")[-1])
            if src_id not in node_ids or src_id == dst_col_id:
                continue
            transform_expr, filter_expr = summarize_path(path, dialect="oracle")
            edges.append(
                Edge(
                    edge_type="DERIVED_FROM",
                    src_node_id=dst_col_id,
                    dst_node_id=src_id,
                    confidence="low",
                    method="plsql_static_analysis",
                    source_object=unit_id,
                    transform_expression=transform_expr,
                    filter_expression=filter_expr,
                )
            )
    return edges


def _statement_edges(
    parsed: exp.Expression,
    owner: str,
    unit_id: str,
    node_ids: set[str],
    owner_schemas: dict,
    columns_by_object: dict[tuple[str, str], list[str]],
) -> list[Edge]:
    edges: list[Edge] = []

    if isinstance(parsed, exp.Select):
        filter_expr = _statement_filter_expression(parsed)
        edges += _reads_from_edges(parsed, owner, unit_id, node_ids, exclude=None, filter_expr=filter_expr)

    elif isinstance(parsed, exp.Insert):
        target = parsed.this
        target_columns = None
        if isinstance(target, exp.Schema):
            target_table, target_columns = target.this, [c.name for c in target.expressions]
        else:
            target_table = target
        table = _unwrap_table(target_table)
        if table is None:
            return edges
        t_owner, t_name = table_ref(table, owner)
        tid = object_node_id(t_owner, t_name)
        select_filter = _statement_filter_expression(parsed.expression) if isinstance(parsed.expression, exp.Select) else None
        if tid in node_ids:
            edges.append(
                Edge(
                    edge_type="WRITES_TO",
                    src_node_id=unit_id,
                    dst_node_id=tid,
                    confidence="low",
                    method="plsql_static_analysis",
                    source_object=unit_id,
                    filter_expression=select_filter,
                )
            )
        if isinstance(parsed.expression, exp.Select):
            cols = target_columns or columns_by_object.get((t_owner, t_name))
            edges += _insert_select_column_edges(
                parsed, owner, t_owner, t_name, cols, node_ids, owner_schemas, unit_id
            )
            edges += _reads_from_edges(
                parsed.expression, owner, unit_id, node_ids, exclude=tid, filter_expr=select_filter
            )

    elif isinstance(parsed, exp.Update):
        table = _unwrap_table(parsed.this)
        tid = None
        filter_expr = _statement_filter_expression(parsed)
        if table is not None:
            t_owner, t_name = table_ref(table, owner)
            tid = object_node_id(t_owner, t_name)
            if tid in node_ids:
                edges.append(
                    Edge(
                        edge_type="WRITES_TO",
                        src_node_id=unit_id,
                        dst_node_id=tid,
                        confidence="low",
                        method="plsql_static_analysis",
                        source_object=unit_id,
                        filter_expression=filter_expr,
                    )
                )
        edges += _reads_from_edges(parsed, owner, unit_id, node_ids, exclude=tid, filter_expr=filter_expr)

    elif isinstance(parsed, exp.Delete):
        table = _unwrap_table(parsed.this) or _unwrap_table(parsed.args.get("tables"))
        tid = None
        filter_expr = _statement_filter_expression(parsed)
        if table is not None:
            t_owner, t_name = table_ref(table, owner)
            tid = object_node_id(t_owner, t_name)
            if tid in node_ids:
                edges.append(
                    Edge(
                        edge_type="WRITES_TO",
                        src_node_id=unit_id,
                        dst_node_id=tid,
                        confidence="low",
                        method="plsql_static_analysis",
                        source_object=unit_id,
                        filter_expression=filter_expr,
                    )
                )
        edges += _reads_from_edges(parsed, owner, unit_id, node_ids, exclude=tid, filter_expr=filter_expr)

    elif isinstance(parsed, exp.Merge):
        table = _unwrap_table(parsed.this)
        tid = None
        filter_expr = _statement_filter_expression(parsed)
        if table is not None:
            t_owner, t_name = table_ref(table, owner)
            tid = object_node_id(t_owner, t_name)
            if tid in node_ids:
                edges.append(
                    Edge(
                        edge_type="WRITES_TO",
                        src_node_id=unit_id,
                        dst_node_id=tid,
                        confidence="low",
                        method="plsql_static_analysis",
                        source_object=unit_id,
                        filter_expression=filter_expr,
                    )
                )
        edges += _reads_from_edges(parsed, owner, unit_id, node_ids, exclude=tid, filter_expr=filter_expr)

    return edges


def build_plsql_lineage_edges(
    store: SqliteStore, node_ids: set[str], only_objects: set[tuple[str, str]] | None = None
) -> tuple[list[Edge], int]:
    """`only_objects`, when given, restricts (re-)harvesting to that set of (owner,
    object_name) keys - the incremental-refresh path (PROMPT.md 5.1). Skipped units'
    previously-recorded unresolved-lineage rows are carried forward unchanged; their
    edges are the caller's responsibility to carry forward from the prior graph."""
    owner_schemas = schema_by_owner(store)
    columns_by_object: dict[tuple[str, str], list[str]] = defaultdict(list)
    for row in store.query(
        "SELECT owner, object_name, column_name FROM raw_columns ORDER BY owner, object_name, column_id"
    ):
        columns_by_object[(row["owner"], row["object_name"])].append(row["column_name"])

    prior_unresolved: dict[tuple[str, str], list[tuple]] = defaultdict(list)
    for row in store.query("SELECT * FROM unresolved_lineage"):
        prior_unresolved[(row["owner"], row["object_name"])].append(
            (row["owner"], row["object_name"], row["object_type"], row["line"], row["raw_text"], row["detected_at"])
        )

    edges: list[Edge] = []
    unresolved_rows: list[tuple] = []
    parse_errors = 0

    units: list[tuple[str, str, str, str | None]] = [
        (r["owner"], r["object_name"], r["object_type"], r["body"])
        for r in store.query(
            "SELECT owner, object_name, object_type, body FROM raw_source "
            "WHERE object_type IN ('PROCEDURE', 'FUNCTION', 'PACKAGE BODY')"
        )
    ]

    trigger_rows = store.query(
        "SELECT owner, trigger_name, table_owner, table_name, body FROM raw_triggers"
    )
    for row in trigger_rows:
        units.append((row["owner"], row["trigger_name"], "TRIGGER", row["body"]))
        unit_id = object_node_id(row["owner"], row["trigger_name"])
        table_id = object_node_id(row["table_owner"], row["table_name"]) if row["table_owner"] else None
        if unit_id in node_ids and table_id in node_ids:
            edges.append(
                Edge(
                    edge_type="WRITES_TO",
                    src_node_id=unit_id,
                    dst_node_id=table_id,
                    confidence="high",
                    method="ddl_parse",
                    source_object=unit_id,
                    transform_expression="Trigger's own table (ALL_TRIGGERS.TABLE_NAME)",
                )
            )

    for owner, name, _otype, body in units:
        unit_id = object_node_id(owner, name)
        if unit_id not in node_ids:
            continue
        if only_objects is not None and (owner, name) not in only_objects:
            unresolved_rows.extend(prior_unresolved.get((owner, name), []))
            continue
        for stmt in harvest_statements(body):
            if stmt.kind == "dynamic_sql":
                unresolved_rows.append(
                    (owner, name, _otype, stmt.line, stmt.raw_text, datetime.now(UTC).isoformat())
                )
                continue
            try:
                parsed = sqlglot.parse_one(stmt.sql_for_parsing, dialect="oracle")
            except Exception as exc:
                logger.debug("PL/SQL statement parse failed in %s: %s", unit_id, exc)
                parse_errors += 1
                continue
            edges.extend(_statement_edges(parsed, owner, unit_id, node_ids, owner_schemas, columns_by_object))

    store.replace_unresolved_lineage(unresolved_rows)
    return edges, parse_errors
