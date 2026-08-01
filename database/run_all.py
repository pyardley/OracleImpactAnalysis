"""Master orchestrator: builds the entire RetailDemo schema from scratch -
tables, functions, views, procedures, triggers - then loads seed data and
populates the derived report tables by calling the same procedures that
maintain them in normal operation.

SAFETY: this is a from-scratch builder, not a sync tool. It refuses to run
if any target table already exists, unless --force is passed. --force DROPS
every object this script knows about (tables CASCADE CONSTRAINTS, views,
procedures, functions - triggers go with their table) before rebuilding -
that is a genuinely destructive, irreversible action against whatever
schema ORACLE_DSN/ORACLE_USER in .env currently points at. Point this at an
empty schema for normal use; only pass --force when you deliberately want
to wipe and rebuild the current one.

Usage:
    uv run python database/run_all.py                 # build into an empty schema
    uv run python database/run_all.py --force          # DROP existing objects first, then rebuild
    uv run python database/run_all.py --skip-data      # objects only, no data or derived tables
    uv run python database/run_all.py --skip-derived   # objects + base data, skip report procs
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import oracledb

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oia.config import get_settings  # noqa: E402

BASE = Path(__file__).parent
DATA_DIR = BASE / "data"

OBJECT_FILES = ["01_tables.sql", "02_functions.sql", "03_views.sql", "04_procedures.sql", "05_triggers.sql"]

DATA_FILES = [
    "01_regions.sql", "02_productcategories.sql", "03_customers.sql", "04_employees.sql",
    "05_products.sql", "06_warehouses.sql", "07_inventory.sql", "08_orders.sql",
    "09_orderlines.sql", "10_payments.sql", "11_returns.sql",
]

# Reverse of creation/FK-dependency order, so drops never hit a still-referenced parent.
TABLES_DROP_ORDER = [
    "STAGINGCUSTOMERSEGMENT", "STAGINGCOMPLETEDORDERLINES", "REPORT_PRODUCTPERFORMANCE",
    "REPORT_MONTHLYSALESBYREGION", "REPORT_INVENTORYREPLENISHMENT", "REPORT_EMPLOYEECOMMISSION",
    "REPORT_CUSTOMERCHURNRISK", "AUDITLOG", "RETURNS", "PAYMENTS", "ORDERLINES", "ORDERS",
    "INVENTORY", "WAREHOUSES", "PRODUCTS", "EMPLOYEES", "CUSTOMERS", "PRODUCTCATEGORIES", "REGIONS",
]
PROCEDURES = [
    "USP_BUILDREPORT_PRODUCTPERFORMANCE", "USP_BUILDREPORT_MONTHLYSALESBYREGION",
    "USP_BUILDREPORT_INVENTORYREPLENISHMENT", "USP_BUILDREPORT_EMPLOYEECOMMISSION",
    "USP_BUILDREPORT_CUSTOMERCHURNRISK", "USP_STAGECOMPLETEDORDERLINES", "USP_LOOKUPCUSTOMERSEGMENT",
]
VIEWS = ["VW_RETURNSDETAIL", "VW_EMPLOYEEREGIONMAP", "VW_ACTIVEINVENTORYSTATUS",
         "VW_CUSTOMERORDERSUMMARY", "VW_ORDERLINEDETAIL"]
FUNCTIONS = ["FN_FISCALPERIOD", "FN_NETLINEAMOUNT"]

TABLES_WITH_TRIGGERS = ["CUSTOMERS", "ORDERS", "ORDERLINES", "RETURNS"]


# ---- SQL file parsing (shared by all object files - see database/*.sql headers) -----


def split_sql_blocks(sql_text: str) -> list[str]:
    """Splits on lines that are exactly "/" - the delimiter every file in
    this directory uses between statements (matches the SQL*Plus/sqlcl
    convention, so these files also run standalone in any Oracle client)."""
    chunks: list[str] = []
    current: list[str] = []
    for line in sql_text.splitlines():
        if line.strip() == "/":
            stmt = "\n".join(current).strip()
            if stmt:
                chunks.append(stmt)
            current = []
        else:
            current.append(line)
    tail = "\n".join(current).strip()
    if tail:
        chunks.append(tail)
    return chunks


def _clean_statement(stmt: str) -> str:
    """CREATE VIEW/TABLE statements need their trailing ';' stripped before
    cursor.execute() (plain DDL - the ';' is a SQL*Plus-only convention);
    CREATE FUNCTION/PROCEDURE/TRIGGER's own 'END name;' must keep it - it's
    real PL/SQL block syntax there, not a statement terminator. Anonymous
    PL/SQL blocks (BEGIN...END;) also keep it for the same reason. Lines
    that are pure SQL*Plus/sqlcl client directives (SET SERVEROUTPUT ON,
    etc.) are meaningless to a direct DB API call and are dropped.

    Every file in this directory routinely has a `--` header/explanatory
    comment directly before a statement (see e.g. 02_functions.sql), so the
    "what kind of statement is this" check must skip past leading comment
    lines rather than testing the raw start of `stmt` - otherwise a
    comment-prefixed FUNCTION/PROCEDURE/TRIGGER gets misclassified as plain
    DDL and has its required terminating ';' incorrectly stripped."""
    lines = [ln for ln in stmt.splitlines() if not ln.strip().upper().startswith("SET ")]
    stmt = "\n".join(lines).strip()
    is_plsql_unit = _significant_prefix(stmt).upper().lstrip().startswith(
        ("CREATE OR REPLACE FUNCTION", "CREATE OR REPLACE PROCEDURE", "CREATE OR REPLACE TRIGGER", "BEGIN")
    )
    if not is_plsql_unit and stmt.rstrip().endswith(";"):
        return stmt.rstrip()[:-1]
    return stmt


def _significant_prefix(stmt: str) -> str:
    """`stmt` with leading blank/comment-only lines skipped - used to detect
    what kind of statement this is regardless of a header comment coming first."""
    lines = stmt.splitlines()
    for i, line in enumerate(lines):
        s = line.strip()
        if s and not s.startswith("--"):
            return "\n".join(lines[i:])
    return stmt


def object_label(stmt: str) -> str:
    significant = _significant_prefix(stmt)
    words = significant.split()
    upper = [w.upper() for w in words]
    for kind in ("FUNCTION", "PROCEDURE", "TRIGGER", "VIEW", "TABLE"):
        if kind in upper[:4]:
            idx = upper.index(kind)
            name = words[idx + 1].split("(")[0].strip().upper()
            return f"{kind} {name}"
    if upper[:1] == ["BEGIN"]:
        return "PL/SQL block"
    return (significant or stmt).splitlines()[0][:60]


# ---- execution ------------------------------------------------------------------


def run_object_file(cur, path: Path) -> None:
    print(f"--- {path.name} ---")
    for raw_stmt in split_sql_blocks(path.read_text(encoding="utf-8")):
        stmt = _clean_statement(raw_stmt)
        if not stmt:
            continue
        label = object_label(stmt)
        cur.execute(stmt)
        print(f"  [OK] {label}")


def run_data_file(cur, path: Path) -> int:
    count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("--"):
            continue
        cur.execute(line[:-1] if line.endswith(";") else line)
        count += 1
    return count


def _ignore_missing(cur, sql: str, *, missing_codes: set[int]) -> None:
    try:
        cur.execute(sql)
    except oracledb.DatabaseError as exc:
        (error_obj,) = exc.args
        if getattr(error_obj, "code", None) not in missing_codes:
            raise


def drop_all(cur) -> None:
    print("--- Dropping existing objects (--force) ---")
    for name in PROCEDURES + FUNCTIONS:
        _ignore_missing(cur, f"DROP PROCEDURE {name}", missing_codes={4043})
    for name in FUNCTIONS:
        _ignore_missing(cur, f"DROP FUNCTION {name}", missing_codes={4043})
    for name in VIEWS:
        _ignore_missing(cur, f"DROP VIEW {name}", missing_codes={942, 4043})
    for name in TABLES_DROP_ORDER:
        _ignore_missing(cur, f"DROP TABLE {name} CASCADE CONSTRAINTS PURGE", missing_codes={942})
    print("  done")


def existing_tables(cur) -> list[str]:
    cur.execute("SELECT table_name FROM user_tables WHERE table_name IN ({})".format(
        ",".join(f"'{t}'" for t in TABLES_DROP_ORDER)
    ))
    return [r[0] for r in cur.fetchall()]


def disable_fk_constraints(cur) -> list[tuple[str, str]]:
    cur.execute("SELECT table_name, constraint_name FROM user_constraints WHERE constraint_type = 'R'")
    fks = cur.fetchall()
    for table, con in fks:
        cur.execute(f"ALTER TABLE {table} DISABLE CONSTRAINT {con}")
    return fks


def enable_fk_constraints(cur, fks: list[tuple[str, str]]) -> None:
    for table, con in fks:
        cur.execute(f"ALTER TABLE {table} ENABLE CONSTRAINT {con}")


def set_triggers(cur, enabled: bool) -> None:
    verb = "ENABLE" if enabled else "DISABLE"
    for table in TABLES_WITH_TRIGGERS:
        cur.execute(f"ALTER TABLE {table} {verb} ALL TRIGGERS")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force", action="store_true", help="DROP existing objects first (destructive)")
    parser.add_argument("--skip-data", action="store_true", help="Create objects only, skip data loading entirely")
    parser.add_argument("--skip-derived", action="store_true", help="Skip populating STAGING*/REPORT_* tables")
    args = parser.parse_args()

    settings = get_settings(project_root=BASE.parent)
    conn = oracledb.connect(user=settings.oracle_user, password=settings.oracle_password, dsn=settings.oracle_dsn)
    cur = conn.cursor()

    present = existing_tables(cur)
    if present and not args.force:
        print(f"Refusing to run: {len(present)} target table(s) already exist in this schema "
              f"(e.g. {present[0]}). Pass --force to DROP and rebuild, or point ORACLE_DSN/"
              f"ORACLE_USER at an empty schema.")
        conn.close()
        sys.exit(1)

    if present and args.force:
        drop_all(cur)
        conn.commit()

    for filename in OBJECT_FILES:
        run_object_file(cur, BASE / filename)
    conn.commit()
    print("\nAll objects created.\n")

    if args.skip_data:
        conn.close()
        print("--skip-data: done (objects only).")
        return

    print("--- Disabling triggers and FK constraints for bulk load ---")
    set_triggers(cur, enabled=False)
    fks = disable_fk_constraints(cur)
    conn.commit()

    total_rows = 0
    for filename in DATA_FILES:
        path = DATA_DIR / filename
        n = run_data_file(cur, path)
        conn.commit()
        print(f"  {filename}: {n} rows")
        total_rows += n
    print(f"Loaded {total_rows} rows across {len(DATA_FILES)} tables.\n")

    print("--- Re-enabling FK constraints (validates data) and triggers ---")
    enable_fk_constraints(cur, fks)
    set_triggers(cur, enabled=True)
    conn.commit()
    print("  done\n")

    if args.skip_derived:
        conn.close()
        print("--skip-derived: done (objects + base data, no report tables populated).")
        return

    print("--- Populating derived (staging + report) tables ---")
    run_object_file(cur, BASE / "06_populate_derived.sql")
    conn.commit()
    conn.close()
    print("\nDone: schema, code objects, base data, and derived report tables all built.")


if __name__ == "__main__":
    main()
