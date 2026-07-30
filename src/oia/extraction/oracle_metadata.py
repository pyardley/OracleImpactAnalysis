"""Extracts Oracle data-dictionary metadata (thin-mode, strictly read-only SELECTs)
into the local SQLite store. See PROMPT.md section 5.1 for the view list and rationale.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass

import oracledb

from oia.config.settings import Settings
from oia.storage.sqlite_store import SqliteStore

logger = logging.getLogger("oia.extraction")

OBJECT_TYPES_OF_INTEREST = (
    "TABLE",
    "VIEW",
    "MATERIALIZED VIEW",
    "PROCEDURE",
    "FUNCTION",
    "PACKAGE",
    "PACKAGE BODY",
    "TRIGGER",
    "SYNONYM",
)

SOURCE_TYPES = ("PROCEDURE", "FUNCTION", "PACKAGE", "PACKAGE BODY")

OwnerFilter = Callable[[str | None], bool]


class ExtractionError(RuntimeError):
    """Raised for Oracle connectivity/privilege problems, with an actionable message."""


@dataclass
class ExtractionStats:
    objects_processed: int = 0
    objects_failed: int = 0


def connect(settings: Settings) -> oracledb.Connection:
    try:
        return oracledb.connect(
            user=settings.oracle_user,
            password=settings.oracle_password,
            dsn=settings.oracle_dsn,
        )
    except oracledb.DatabaseError as exc:
        raise ExtractionError(
            f"Could not connect to Oracle at dsn={settings.oracle_dsn!r} as user "
            f"{settings.oracle_user!r}: {exc}"
        ) from exc


def _dict_view(settings: Settings, name: str) -> str:
    prefix = "DBA" if settings.dictionary_scope == "dba" else "ALL"
    return f"{prefix}_{name}"


def _run(conn, sql: str, params: tuple = ()) -> list[tuple]:
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        return cur.fetchall()
    except oracledb.DatabaseError as exc:
        (error_obj,) = exc.args
        code = getattr(error_obj, "code", None)
        if code == 942:
            raise ExtractionError(
                f"ORA-00942 (table or view does not exist) running: {sql.strip().splitlines()[0]}... "
                "The connecting user likely lacks SELECT_CATALOG_ROLE / the DBA_* grant needed for "
                "dictionary_scope: dba. Either grant it, or set dictionary_scope: all in config.yaml."
            ) from exc
        raise


def _clob(value) -> str | None:
    if value is None:
        return None
    if hasattr(value, "read"):
        return value.read()
    return str(value)


def make_owner_filter(settings: Settings, default_owner: str) -> OwnerFilter:
    """Empty `schemas.include` defaults to the connecting user's own schema - not
    "everything visible" - because ALL_* views for a real Oracle instance also surface
    thousands of PUBLIC synonyms and vendor-schema objects (e.g. Database Vault's
    DVSYS/DVF) that the connecting user merely has grants on, not objects relevant to
    this schema's lineage. Widen scope explicitly via schemas.include for multi-schema use.
    """
    include = {s.upper() for s in settings.schemas.include} or {default_owner.upper()}
    exclude = {s.upper() for s in settings.schemas.exclude}

    def allowed(owner: str | None) -> bool:
        owner_u = (owner or "").upper()
        if not owner_u or owner_u in exclude:
            return False
        return owner_u in include

    return allowed


def _in_clause(n: int, start: int = 1) -> str:
    return ", ".join(f":{i}" for i in range(start, start + n))


def _extract_objects(conn, settings: Settings, store: SqliteStore, allowed: OwnerFilter) -> list[tuple]:
    view = _dict_view(settings, "OBJECTS")
    sql = (
        f"SELECT owner, object_name, object_type, status, last_ddl_time "
        f"FROM {view} WHERE object_type IN ({_in_clause(len(OBJECT_TYPES_OF_INTEREST))})"
    )
    rows = _run(conn, sql, OBJECT_TYPES_OF_INTEREST)
    kept = [
        (owner, name, otype, status, str(ddl) if ddl else None)
        for owner, name, otype, status, ddl in rows
        if allowed(owner)
    ]
    store.bulk_insert("raw_objects", ["owner", "object_name", "object_type", "status", "last_ddl_time"], kept)
    return kept


def _extract_columns(conn, settings: Settings, store: SqliteStore, allowed: OwnerFilter) -> None:
    view = _dict_view(settings, "TAB_COLUMNS")
    sql = f"SELECT owner, table_name, column_name, data_type, nullable, column_id FROM {view}"
    rows = _run(conn, sql)
    kept = [r for r in rows if allowed(r[0])]
    store.bulk_insert(
        "raw_columns", ["owner", "object_name", "column_name", "data_type", "nullable", "column_id"], kept
    )


def _extract_views(conn, settings: Settings, store: SqliteStore, allowed: OwnerFilter) -> None:
    view = _dict_view(settings, "VIEWS")
    try:
        rows = _run(conn, f"SELECT owner, view_name, text_vc FROM {view}")
    except oracledb.DatabaseError as exc:
        (error_obj,) = exc.args
        if getattr(error_obj, "code", None) != 904:  # TEXT_VC not present pre-12.2
            raise
        logger.info("TEXT_VC not available on this Oracle version; falling back to TEXT")
        rows = _run(conn, f"SELECT owner, view_name, text FROM {view}")

    kept = [(o, n, _clob(t), 0) for o, n, t in rows if allowed(o)]
    store.bulk_insert("raw_view_text", ["owner", "view_name", "text", "is_mview"], kept)

    mview = _dict_view(settings, "MVIEWS")
    mrows = _run(conn, f"SELECT owner, mview_name, query FROM {mview}")
    mkept = [(o, n, _clob(q), 1) for o, n, q in mrows if allowed(o)]
    store.bulk_insert("raw_view_text", ["owner", "view_name", "text", "is_mview"], mkept)


def _extract_dependencies(conn, settings: Settings, store: SqliteStore, allowed: OwnerFilter) -> None:
    view = _dict_view(settings, "DEPENDENCIES")
    sql = (
        f"SELECT owner, name, type, referenced_owner, referenced_name, referenced_type "
        f"FROM {view} WHERE referenced_type IS NOT NULL"
    )
    rows = _run(conn, sql)
    kept = [r for r in rows if allowed(r[0])]
    store.bulk_insert(
        "raw_dependencies",
        ["owner", "name", "type", "referenced_owner", "referenced_name", "referenced_type"],
        kept,
    )


def _extract_source(conn, settings: Settings, store: SqliteStore, allowed: OwnerFilter) -> None:
    view = _dict_view(settings, "SOURCE")
    sql = (
        f"SELECT owner, name, type, line, text FROM {view} "
        f"WHERE type IN ({_in_clause(len(SOURCE_TYPES))}) ORDER BY owner, name, type, line"
    )
    rows = _run(conn, sql, SOURCE_TYPES)

    bodies: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    for owner, name, otype, _line, text in rows:
        if not allowed(owner):
            continue
        bodies[(owner, name, otype)].append(text or "")

    kept = [(o, n, t, "".join(lines)) for (o, n, t), lines in bodies.items()]
    store.bulk_insert("raw_source", ["owner", "object_name", "object_type", "body"], kept)


def _extract_arguments(conn, settings: Settings, store: SqliteStore, allowed: OwnerFilter) -> None:
    view = _dict_view(settings, "ARGUMENTS")
    sql = f"SELECT owner, object_name, package_name, argument_name, position, in_out, data_type FROM {view}"
    rows = _run(conn, sql)
    kept = [r for r in rows if allowed(r[0])]
    store.bulk_insert(
        "raw_arguments",
        ["owner", "object_name", "package_name", "argument_name", "position", "in_out", "data_type"],
        kept,
    )


def _extract_triggers(conn, settings: Settings, store: SqliteStore, allowed: OwnerFilter) -> None:
    view = _dict_view(settings, "TRIGGERS")
    sql = (
        f"SELECT owner, trigger_name, table_owner, table_name, triggering_event, "
        f"trigger_type, trigger_body FROM {view}"
    )
    rows = _run(conn, sql)
    kept = [
        (o, n, towner, tname, event, ttype, _clob(body))
        for o, n, towner, tname, event, ttype, body in rows
        if allowed(o)
    ]
    store.bulk_insert(
        "raw_triggers",
        ["owner", "trigger_name", "table_owner", "table_name", "triggering_event", "trigger_type", "body"],
        kept,
    )


def _extract_synonyms(conn, settings: Settings, store: SqliteStore, allowed: OwnerFilter) -> None:
    view = _dict_view(settings, "SYNONYMS")
    sql = f"SELECT owner, synonym_name, table_owner, table_name, db_link FROM {view}"
    rows = _run(conn, sql)
    # ALL_SYNONYMS includes thousands of PUBLIC synonyms for system packages;
    # keep only synonyms pointing at objects within our extraction scope.
    kept = [r for r in rows if allowed(r[2])]
    store.bulk_insert("raw_synonyms", ["owner", "synonym_name", "table_owner", "table_name", "db_link"], kept)


def _extract_foreign_keys(conn, settings: Settings, store: SqliteStore, allowed: OwnerFilter) -> None:
    """FK relationships - a secondary, structural signal (not data lineage) per
    PROMPT.md 5.1: 'PK/FK edges are a useful secondary signal for report/table
    relevance, not core lineage.' Column-level via a self-join on constraint position.
    """
    cons = _dict_view(settings, "CONSTRAINTS")
    cons_cols = _dict_view(settings, "CONS_COLUMNS")
    sql = f"""
        SELECT c.constraint_name, fk.owner, fk.table_name, fk.column_name,
               pk.owner, pk.table_name, pk.column_name
        FROM {cons} c
        JOIN {cons_cols} fk ON c.owner = fk.owner AND c.constraint_name = fk.constraint_name
        JOIN {cons_cols} pk ON c.r_owner = pk.owner AND c.r_constraint_name = pk.constraint_name
                            AND fk.position = pk.position
        WHERE c.constraint_type = 'R'
    """
    rows = _run(conn, sql)
    kept = [r for r in rows if allowed(r[1]) and allowed(r[4])]
    store.bulk_insert(
        "raw_foreign_keys",
        ["constraint_name", "owner", "table_name", "column_name", "r_owner", "r_table_name", "r_column_name"],
        kept,
    )


def _extract_comments(conn, settings: Settings, store: SqliteStore, allowed: OwnerFilter) -> None:
    tview = _dict_view(settings, "TAB_COMMENTS")
    trows = _run(conn, f"SELECT owner, table_name, comments FROM {tview} WHERE comments IS NOT NULL")
    kept = [(o, n, None, c) for o, n, c in trows if allowed(o)]

    cview = _dict_view(settings, "COL_COMMENTS")
    crows = _run(conn, f"SELECT owner, table_name, column_name, comments FROM {cview} WHERE comments IS NOT NULL")
    kept += [(o, n, col, c) for o, n, col, c in crows if allowed(o)]

    store.bulk_insert("raw_comments", ["owner", "object_name", "column_name", "comments"], kept)


def extract_metadata(settings: Settings, store: SqliteStore) -> ExtractionStats:
    """Runs a full, read-only extraction of the configured Oracle schema(s) into SQLite."""
    conn = connect(settings)
    try:
        allowed = make_owner_filter(settings, default_owner=conn.username)
        store.clear_raw_tables()
        objects = _extract_objects(conn, settings, store, allowed)
        _extract_columns(conn, settings, store, allowed)
        _extract_views(conn, settings, store, allowed)
        _extract_dependencies(conn, settings, store, allowed)
        _extract_source(conn, settings, store, allowed)
        _extract_arguments(conn, settings, store, allowed)
        _extract_triggers(conn, settings, store, allowed)
        _extract_synonyms(conn, settings, store, allowed)
        _extract_foreign_keys(conn, settings, store, allowed)
        _extract_comments(conn, settings, store, allowed)
        store.commit()
        return ExtractionStats(objects_processed=len(objects), objects_failed=0)
    finally:
        conn.close()
