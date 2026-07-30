"""One-off deployment script: pushes demo_data/oracle_plsql/*.sql into the live
RetailDemo database, in dependency order (functions -> views -> procedures ->
triggers). Not part of the oia package - this is demo-environment setup, run
manually with `uv run python demo_data/oracle_plsql/deploy.py`.

`CREATE OR REPLACE FUNCTION/PROCEDURE/TRIGGER` succeeds at the DDL level even
when the PL/SQL body fails to compile (the object is just created INVALID) -
so this checks USER_ERRORS after each one instead of trusting a lack of
exception.
"""

from __future__ import annotations

import sys
from pathlib import Path

import oracledb

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from oia.config import get_settings  # noqa: E402

FILES = ["01_functions.sql", "02_views.sql", "03_procedures.sql", "04_triggers.sql"]


def split_statements(sql_text: str) -> list[str]:
    chunks, current = [], []
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


def object_name_and_type(stmt: str) -> tuple[str, str] | None:
    words = stmt.split()
    upper = [w.upper() for w in words]
    for kind in ("FUNCTION", "PROCEDURE", "TRIGGER", "VIEW"):
        if kind in upper[:4]:
            idx = upper.index(kind)
            name = words[idx + 1].split("(")[0].strip().upper()
            return name, kind
    return None


def check_errors(cur, name: str, kind: str) -> list[str]:
    cur.execute(
        "SELECT line, position, text FROM user_errors WHERE name = :name AND type = :kind ORDER BY sequence",
        name=name, kind=kind,
    )
    return [f"    line {line}, col {pos}: {text.strip()}" for line, pos, text in cur.fetchall()]


def main() -> None:
    settings = get_settings()
    conn = oracledb.connect(user=settings.oracle_user, password=settings.oracle_password, dsn=settings.oracle_dsn)
    cur = conn.cursor()

    base = Path(__file__).parent
    total, failed = 0, 0

    for filename in FILES:
        print(f"--- {filename} ---")
        sql_text = (base / filename).read_text(encoding="utf-8")
        for stmt in split_statements(sql_text):
            info = object_name_and_type(stmt)
            # A trailing ";" is required PL/SQL-unit syntax for FUNCTION/PROCEDURE/
            # TRIGGER ("END name;") but is invalid on a plain VIEW's DDL statement
            # when submitted directly (not through SQL*Plus, which strips it).
            if info and info[1] == "VIEW" and stmt.rstrip().endswith(";"):
                stmt = stmt.rstrip()[:-1]
            label = f"{info[1]} {info[0]}" if info else stmt.splitlines()[0][:60]
            total += 1
            try:
                cur.execute(stmt)
            except oracledb.DatabaseError as exc:
                print(f"[FAIL] {label}: {exc}")
                failed += 1
                continue

            if info and info[1] in ("FUNCTION", "PROCEDURE", "TRIGGER"):
                errors = check_errors(cur, info[0], info[1])
                if errors:
                    print(f"[INVALID] {label}")
                    for e in errors:
                        print(e)
                    failed += 1
                    continue
            print(f"[OK] {label}")

    conn.commit()
    conn.close()
    print(f"\n{total - failed}/{total} objects deployed successfully.")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
