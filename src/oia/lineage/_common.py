"""Shared helpers for DDL and PL/SQL lineage parsing (both built on sqlglot)."""

from __future__ import annotations

from collections import defaultdict

from sqlglot import exp

from oia.storage.sqlite_store import SqliteStore


def schema_by_owner(store: SqliteStore) -> dict[str, dict[str, dict[str, str]]]:
    """owner -> table/view -> column -> data_type, for sqlglot's `schema=` kwarg."""
    schema: dict[str, dict[str, dict[str, str]]] = defaultdict(lambda: defaultdict(dict))
    for row in store.query("SELECT owner, object_name, column_name, data_type FROM raw_columns"):
        schema[row["owner"]][row["object_name"]][row["column_name"]] = row["data_type"] or "VARCHAR2"
    return schema


def lineage_leaves(node) -> list[tuple]:
    """Walks a sqlglot.lineage.Node tree down to its base-table references,
    returning (leaf, path) pairs - `path` is every node from the root
    (inclusive) down to the leaf (inclusive), in root-to-leaf order. Pass
    `path` to `summarize_path()` to recover the real transform/filter logic:
    a leaf's own `.expression` is just the FROM-clause table reference that
    produced it (e.g. "ORDERS O" for a bare `FROM Orders o`), not a
    computation - the real computation(s) live on ancestor nodes.
    """
    out = []
    stack = [(node, [node])]
    while stack:
        current, path = stack.pop()
        if current.downstream:
            stack.extend((child, [*path, child]) for child in current.downstream)
        elif type(current.source).__name__ == "Table":
            out.append((current, path))
    return out


def summarize_path(path: list, dialect: str = "oracle") -> tuple[str | None, str | None]:
    """Given a root-to-leaf lineage path (from `lineage_leaves`), returns
    (transform_expression, filter_expression):

    - transform_expression: every non-trivial computed expression along the
      path (innermost/closest-to-the-leaf first), e.g. a per-row function
      call followed by the aggregate applied to it one CTE level out. Bare
      passthrough aliasing (`SELECT x.total AS total`) is skipped as noise.
    - filter_expression: every WHERE clause and JOIN...ON condition found at
      any stage along the path - eligibility/join criteria that a column's
      value implicitly depends on (e.g. "only completed orders from active
      customers count towards this total") but that no column-to-column
      DERIVED_FROM edge on its own would otherwise reveal.

    The leaf itself (path[-1]) is excluded from both - see `lineage_leaves`.
    """
    transforms: list[str] = []
    filters: list[str] = []
    for n in path[:-1]:
        if n.expression is not None:
            unwrapped = n.expression.this if isinstance(n.expression, exp.Alias) else n.expression
            if not isinstance(unwrapped, exp.Column):  # bare passthrough - not a real computation
                sql = n.expression.sql(dialect=dialect)
                if sql not in transforms:
                    transforms.append(sql)
        if isinstance(n.source, exp.Select):
            where = n.source.args.get("where")
            if where is not None:
                sql = where.sql(dialect=dialect)
                if sql not in filters:
                    filters.append(sql)
            for join in n.source.args.get("joins") or []:
                on = join.args.get("on")
                if on is not None:
                    sql = f"JOIN ON {on.sql(dialect=dialect)}"
                    if sql not in filters:
                        filters.append(sql)
    transform_text = " <- ".join(reversed(transforms)) if transforms else None
    filter_text = " AND ".join(filters) if filters else None
    return transform_text, filter_text


def table_ref(table_expr, default_owner: str) -> tuple[str, str]:
    owner = getattr(table_expr, "db", "") or default_owner
    name = table_expr.name
    return owner, name
