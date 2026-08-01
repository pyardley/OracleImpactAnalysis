"""SQLite-backed store for OIA's own working data.

This is a local artifact of the tool (raw extracted metadata + the compiled
graph), never written back to the Oracle database being analyzed - OIA only
ever SELECTs from Oracle. See PROMPT.md section 5.3 for the rationale.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Self

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"

RAW_TABLES = (
    "raw_objects",
    "raw_columns",
    "raw_source",
    "raw_view_text",
    "raw_dependencies",
    "raw_arguments",
    "raw_triggers",
    "raw_synonyms",
    "raw_foreign_keys",
    "raw_comments",
)


class SqliteStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._ensure_schema()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    def _ensure_schema(self) -> None:
        self.conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Additive, idempotent column migrations for existing databases -
        `CREATE TABLE IF NOT EXISTS` alone won't add new columns to a table
        that already exists from an older schema version."""
        cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(graph_edges)")}
        if "filter_expression" not in cols:
            self.conn.execute("ALTER TABLE graph_edges ADD COLUMN filter_expression TEXT")

    # ---- raw extraction tables -------------------------------------------------

    def clear_raw_tables(self) -> None:
        for table in RAW_TABLES:
            self.conn.execute(f"DELETE FROM {table}")

    def bulk_insert(self, table: str, columns: Sequence[str], rows: Iterable[Sequence]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        placeholders = ", ".join("?" for _ in columns)
        col_list = ", ".join(columns)
        self.conn.executemany(
            f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})", rows
        )
        return len(rows)

    def query(self, sql: str, params: Sequence = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def commit(self) -> None:
        self.conn.commit()

    # ---- extraction_state / extraction_runs ------------------------------------

    def get_extraction_state(self) -> dict[tuple[str, str, str], str | None]:
        rows = self.conn.execute(
            "SELECT owner, object_name, object_type, last_ddl_time_seen FROM extraction_state"
        ).fetchall()
        return {(r["owner"], r["object_name"], r["object_type"]): r["last_ddl_time_seen"] for r in rows}

    def set_extraction_state(self, rows: Iterable[tuple[str, str, str, str | None, str]]) -> None:
        self.conn.executemany(
            """
            INSERT INTO extraction_state (owner, object_name, object_type, last_ddl_time_seen, last_extracted_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(owner, object_name, object_type)
            DO UPDATE SET last_ddl_time_seen = excluded.last_ddl_time_seen,
                          last_extracted_at = excluded.last_extracted_at
            """,
            list(rows),
        )

    def prune_extraction_state(self, live_keys: set[tuple[str, str, str]]) -> None:
        rows = self.conn.execute(
            "SELECT owner, object_name, object_type FROM extraction_state"
        ).fetchall()
        stale = [
            (r["owner"], r["object_name"], r["object_type"])
            for r in rows
            if (r["owner"], r["object_name"], r["object_type"]) not in live_keys
        ]
        self.conn.executemany(
            "DELETE FROM extraction_state WHERE owner = ? AND object_name = ? AND object_type = ?",
            stale,
        )

    def start_run(self, mode: str, schema_scope: str | None) -> int:
        cur = self.conn.execute(
            "INSERT INTO extraction_runs (mode, schema_scope, started_at) VALUES (?, ?, datetime('now'))",
            (mode, schema_scope),
        )
        self.conn.commit()
        return cur.lastrowid

    def finish_run(
        self, run_id: int, objects_processed: int, objects_failed: int, parse_errors_count: int
    ) -> None:
        self.conn.execute(
            """
            UPDATE extraction_runs
            SET finished_at = datetime('now'),
                objects_processed = ?,
                objects_failed = ?,
                parse_errors_count = ?
            WHERE run_id = ?
            """,
            (objects_processed, objects_failed, parse_errors_count, run_id),
        )
        self.conn.commit()

    def latest_run(self) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM extraction_runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()

    # ---- graph ------------------------------------------------------------------

    def replace_graph(self, nodes: Iterable[Sequence], edges: Iterable[Sequence]) -> None:
        self.conn.execute("DELETE FROM graph_nodes")
        self.conn.execute("DELETE FROM graph_edges")
        self.bulk_insert(
            "graph_nodes",
            ["node_id", "node_type", "owner", "object_name", "column_name", "data_type", "is_report", "last_ddl_time", "metadata"],
            nodes,
        )
        self.bulk_insert(
            "graph_edges",
            [
                "edge_id", "edge_type", "src_node_id", "dst_node_id", "confidence", "method",
                "source_object", "source_line_range", "transform_expression", "filter_expression", "extracted_at",
            ],
            edges,
        )
        self.conn.commit()

    def all_nodes(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM graph_nodes").fetchall()

    def all_edges(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM graph_edges").fetchall()

    # ---- unresolved lineage (dynamic SQL / parse-failure gap markers) -----------

    def replace_unresolved_lineage(self, rows: Iterable[Sequence]) -> None:
        self.conn.execute("DELETE FROM unresolved_lineage")
        self.bulk_insert(
            "unresolved_lineage", ["owner", "object_name", "object_type", "line", "raw_text", "detected_at"], rows
        )
        self.conn.commit()

    def unresolved_lineage(self, object_name: str | None = None) -> list[sqlite3.Row]:
        if object_name:
            return self.conn.execute(
                "SELECT * FROM unresolved_lineage WHERE object_name = ?", (object_name,)
            ).fetchall()
        return self.conn.execute("SELECT * FROM unresolved_lineage").fetchall()
