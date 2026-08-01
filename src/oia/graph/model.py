"""Node/edge schema (PROMPT.md 5.3) and SQLite <-> NetworkX hydration."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

import networkx as nx

from oia.storage.sqlite_store import SqliteStore

NODE_TYPES = {"TABLE", "COLUMN", "VIEW", "MVIEW", "PROCEDURE", "FUNCTION", "PACKAGE", "TRIGGER"}
EDGE_TYPES = {"READS_FROM", "WRITES_TO", "DERIVED_FROM", "CALLS", "REFERENCES"}
CONFIDENCE_LEVELS = {"high", "medium", "low", "manual", "none"}

OBJECT_TYPE_TO_NODE_TYPE = {
    "TABLE": "TABLE",
    "VIEW": "VIEW",
    "MATERIALIZED VIEW": "MVIEW",
    "PROCEDURE": "PROCEDURE",
    "FUNCTION": "FUNCTION",
    "PACKAGE": "PACKAGE",
    "PACKAGE BODY": "PACKAGE",
    "TRIGGER": "TRIGGER",
}


def object_node_id(owner: str, object_name: str) -> str:
    return f"{owner.upper()}.{object_name.upper()}"


def column_node_id(owner: str, object_name: str, column_name: str) -> str:
    return f"{owner.upper()}.{object_name.upper()}.{column_name.upper()}"


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class Node:
    node_id: str
    node_type: str
    owner: str
    object_name: str
    column_name: str | None = None
    data_type: str | None = None
    is_report: bool = False
    last_ddl_time: str | None = None
    metadata: dict = field(default_factory=dict)

    def to_row(self) -> tuple:
        return (
            self.node_id,
            self.node_type,
            self.owner,
            self.object_name,
            self.column_name,
            self.data_type,
            int(self.is_report),
            self.last_ddl_time,
            json.dumps(self.metadata),
        )


@dataclass
class Edge:
    edge_type: str
    src_node_id: str
    dst_node_id: str
    confidence: str
    method: str
    source_object: str | None = None
    source_line_range: tuple[int, int] | None = None
    transform_expression: str | None = None
    filter_expression: str | None = None
    edge_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    extracted_at: str = field(default_factory=now_iso)

    def to_row(self) -> tuple:
        return (
            self.edge_id,
            self.edge_type,
            self.src_node_id,
            self.dst_node_id,
            self.confidence,
            self.method,
            self.source_object,
            json.dumps(list(self.source_line_range)) if self.source_line_range else None,
            self.transform_expression,
            self.filter_expression,
            self.extracted_at,
        )


def save_graph(store: SqliteStore, nodes: list[Node], edges: list[Edge]) -> None:
    node_ids = {n.node_id for n in nodes}
    # An edge to a node we didn't build (out-of-scope owner, unresolved synonym target,
    # etc.) would hydrate into NetworkX as a bare, attribute-less node and break
    # downstream rendering - drop it and let it surface as an honest coverage gap
    # instead (PROMPT.md 7: "multi-schema coverage depends on grants").
    clean_edges = [e for e in edges if e.src_node_id in node_ids and e.dst_node_id in node_ids]
    store.replace_graph((n.to_row() for n in nodes), (e.to_row() for e in clean_edges))


def edge_from_row(row) -> Edge:
    """Reconstructs an Edge exactly as stored (same id/timestamp) - used to carry
    forward edges for objects an incremental extract decided not to re-parse."""
    return Edge(
        edge_id=row["edge_id"],
        edge_type=row["edge_type"],
        src_node_id=row["src_node_id"],
        dst_node_id=row["dst_node_id"],
        confidence=row["confidence"],
        method=row["method"],
        source_object=row["source_object"],
        source_line_range=tuple(json.loads(row["source_line_range"])) if row["source_line_range"] else None,
        transform_expression=row["transform_expression"],
        filter_expression=row["filter_expression"],
        extracted_at=row["extracted_at"],
    )


def load_graph(store: SqliteStore) -> nx.MultiDiGraph:
    g = nx.MultiDiGraph()
    for row in store.all_nodes():
        g.add_node(
            row["node_id"],
            node_type=row["node_type"],
            owner=row["owner"],
            object_name=row["object_name"],
            column_name=row["column_name"],
            data_type=row["data_type"],
            is_report=bool(row["is_report"]),
            last_ddl_time=row["last_ddl_time"],
            metadata=json.loads(row["metadata"]) if row["metadata"] else {},
        )
    for row in store.all_edges():
        g.add_edge(
            row["src_node_id"],
            row["dst_node_id"],
            key=row["edge_id"],
            edge_type=row["edge_type"],
            confidence=row["confidence"],
            method=row["method"],
            source_object=row["source_object"],
            source_line_range=json.loads(row["source_line_range"]) if row["source_line_range"] else None,
            transform_expression=row["transform_expression"],
            filter_expression=row["filter_expression"],
            extracted_at=row["extracted_at"],
        )
    return g
