"""Builds the object-level graph (nodes for every extracted object/column, plus
REFERENCES/CALLS edges from ALL_DEPENDENCIES and FK constraints) from the raw
tables populated by oia.extraction.oracle_metadata. This is the graph tier
that's always available even when DDL/PL-SQL lineage parsing (oia.lineage.*)
finds nothing to parse - see PROMPT.md 5.1/5.4.
"""

from __future__ import annotations

from collections import defaultdict

from oia.config.settings import Settings
from oia.graph.model import (
    OBJECT_TYPE_TO_NODE_TYPE,
    Edge,
    Node,
    column_node_id,
    object_node_id,
)
from oia.graph.report_rules import is_report
from oia.storage.sqlite_store import SqliteStore

# ALL_DEPENDENCIES referenced_type values that represent "this object calls that
# program unit" rather than "this object reads/references that data object".
CALLABLE_TYPES = {"PROCEDURE", "FUNCTION", "PACKAGE", "PACKAGE BODY"}


def resolve_synonyms(store: SqliteStore) -> dict[tuple[str, str], tuple[str, str]]:
    """Maps (owner, synonym_name) -> the real (owner, object_name) it points at,
    resolving synonym chains and falling back to PUBLIC synonyms. Objects that
    aren't synonyms are simply absent from this map (callers should treat a miss
    as "not a synonym, use the name as-is").
    """
    rows = store.query("SELECT owner, synonym_name, table_owner, table_name FROM raw_synonyms")
    direct: dict[tuple[str, str], tuple[str, str]] = {}
    public: dict[str, tuple[str, str]] = {}
    for r in rows:
        target = (r["table_owner"], r["table_name"])
        if target[0] is None or target[1] is None:
            continue
        if r["owner"] == "PUBLIC":
            public[r["synonym_name"]] = target
        else:
            direct[(r["owner"], r["synonym_name"])] = target

    resolved: dict[tuple[str, str], tuple[str, str]] = {}
    for key in list(direct) + [("PUBLIC", n) for n in public]:
        _owner, name = key
        seen = set()
        current = direct.get(key) or public.get(name)
        while current and current not in seen:
            seen.add(current)
            nxt = direct.get(current) or public.get(current[1])
            if nxt is None:
                break
            current = nxt
        if current:
            resolved[key] = current
    return resolved


def _resolve(owner: str, name: str, synonyms: dict[tuple[str, str], tuple[str, str]]) -> tuple[str, str]:
    if (owner, name) in synonyms:
        return synonyms[(owner, name)]
    if ("PUBLIC", name) in synonyms:
        return synonyms[("PUBLIC", name)]
    return owner, name


def build_object_graph(store: SqliteStore, settings: Settings) -> tuple[list[Node], list[Edge]]:
    nodes: list[Node] = []
    node_ids: set[str] = set()
    edges: list[Edge] = []

    obj_rows = store.query("SELECT owner, object_name, object_type, status, last_ddl_time FROM raw_objects")
    known_objects: dict[tuple[str, str], str] = {}  # (owner, name) -> object_type

    arg_rows = store.query(
        "SELECT owner, object_name, package_name, argument_name, position, in_out, data_type "
        "FROM raw_arguments ORDER BY owner, object_name, package_name, position"
    )
    args_by_object: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in arg_rows:
        key = (r["owner"], r["package_name"] or r["object_name"])
        args_by_object[key].append(
            {
                "name": r["argument_name"],
                "position": r["position"],
                "in_out": r["in_out"],
                "data_type": r["data_type"],
            }
        )

    comment_rows = store.query("SELECT owner, object_name, column_name, comments FROM raw_comments")
    table_comments: dict[tuple[str, str], str] = {}
    column_comments: dict[tuple[str, str, str], str] = {}
    for r in comment_rows:
        if r["column_name"] is None:
            table_comments[(r["owner"], r["object_name"])] = r["comments"]
        else:
            column_comments[(r["owner"], r["object_name"], r["column_name"])] = r["comments"]

    for row in obj_rows:
        owner, name, otype = row["owner"], row["object_name"], row["object_type"]
        node_type = OBJECT_TYPE_TO_NODE_TYPE.get(otype)
        known_objects[(owner, name)] = otype
        if node_type is None:  # SYNONYM etc. - not represented as a node
            continue
        nid = object_node_id(owner, name)
        if nid in node_ids:  # PACKAGE + PACKAGE BODY collapse onto one node
            continue
        node_ids.add(nid)
        metadata = {}
        if (owner, name) in table_comments:
            metadata["comments"] = table_comments[(owner, name)]
        if (owner, name) in args_by_object:
            metadata["arguments"] = args_by_object[(owner, name)]
        nodes.append(
            Node(
                node_id=nid,
                node_type=node_type,
                owner=owner,
                object_name=name,
                is_report=is_report(owner, name, settings.report_rules),
                last_ddl_time=row["last_ddl_time"],
                metadata=metadata,
            )
        )

    col_rows = store.query("SELECT owner, object_name, column_name, data_type, nullable FROM raw_columns")
    for row in col_rows:
        owner, oname, cname = row["owner"], row["object_name"], row["column_name"]
        parent_id = object_node_id(owner, oname)
        if parent_id not in node_ids:  # column of an object outside our node set
            continue
        cid = column_node_id(owner, oname, cname)
        node_ids.add(cid)
        nodes.append(
            Node(
                node_id=cid,
                node_type="COLUMN",
                owner=owner,
                object_name=oname,
                column_name=cname,
                data_type=row["data_type"],
                metadata={
                    "nullable": row["nullable"] == "Y",
                    "comments": column_comments.get((owner, oname, cname)),
                },
            )
        )

    synonyms = resolve_synonyms(store)

    dep_rows = store.query(
        "SELECT owner, name, type, referenced_owner, referenced_name, referenced_type FROM raw_dependencies"
    )
    for row in dep_rows:
        src_owner, src_name = row["owner"], row["name"]
        dst_owner, dst_name = _resolve(row["referenced_owner"] or src_owner, row["referenced_name"], synonyms)
        src_id, dst_id = object_node_id(src_owner, src_name), object_node_id(dst_owner, dst_name)
        if src_id not in node_ids or dst_id not in node_ids or src_id == dst_id:
            continue
        edge_type = "CALLS" if row["referenced_type"] in CALLABLE_TYPES else "REFERENCES"
        edges.append(
            Edge(
                edge_type=edge_type,
                src_node_id=src_id,
                dst_node_id=dst_id,
                confidence="high",
                method="ddl_parse" if edge_type == "REFERENCES" else "plsql_static_analysis",
                source_object=src_id,
            )
        )

    fk_rows = store.query(
        "SELECT constraint_name, owner, table_name, column_name, r_owner, r_table_name, r_column_name "
        "FROM raw_foreign_keys"
    )
    for row in fk_rows:
        src_id = column_node_id(row["owner"], row["table_name"], row["column_name"])
        dst_id = column_node_id(row["r_owner"], row["r_table_name"], row["r_column_name"])
        if src_id not in node_ids or dst_id not in node_ids:
            continue
        edges.append(
            Edge(
                edge_type="REFERENCES",
                src_node_id=src_id,
                dst_node_id=dst_id,
                confidence="medium",
                method="fk_constraint",
                source_object=object_node_id(row["owner"], row["table_name"]),
                transform_expression=f"FOREIGN KEY {row['constraint_name']} (structural relationship, not data lineage)",
            )
        )

    return nodes, edges
