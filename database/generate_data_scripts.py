"""Regenerates database/data/*.sql from the live database's current contents.

Run this against a reference/golden RetailDemo instance whenever the seed
data snapshot needs refreshing - it is strictly read-only against the source
database (SELECT only). Usage: `uv run python database/generate_data_scripts.py`
(reads connection details from .env via oia's own config loader).

Only the 11 true "base" tables are dumped here. AUDITLOG is deliberately
excluded - it's a pure event log, and a freshly-built environment should
start with an empty audit trail, not seeded fake history (it re-populates
itself naturally once TRG_CUSTOMERS_AUDIT/TRG_ORDERS_AUDIT fire on real
activity). REPORT_*/STAGING* tables are excluded because they're wholly
derived - see 06_populate_derived.sql, which regenerates them by calling the
same stored procedures that maintain them in normal operation.
"""

from __future__ import annotations

import decimal
import sys
from pathlib import Path

import oracledb

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oia.config import get_settings  # noqa: E402

BASE = Path(__file__).parent
DATA_DIR = BASE / "data"

# (file_name, table, order_by) - dependency order, purely for readability/diffing;
# run_all.py disables FK constraints during load so insert order isn't load-bearing.
TABLES = [
    ("01_regions.sql", "REGIONS", "REGIONID"),
    ("02_productcategories.sql", "PRODUCTCATEGORIES", "CATEGORYID"),
    ("03_customers.sql", "CUSTOMERS", "CUSTOMERID"),
    ("04_employees.sql", "EMPLOYEES", "EMPLOYEEID"),
    ("05_products.sql", "PRODUCTS", "PRODUCTID"),
    ("06_warehouses.sql", "WAREHOUSES", "WAREHOUSEID"),
    ("07_inventory.sql", "INVENTORY", "PRODUCTID, WAREHOUSEID"),
    ("08_orders.sql", "ORDERS", "ORDERID"),
    ("09_orderlines.sql", "ORDERLINES", "ORDERLINEID"),
    ("10_payments.sql", "PAYMENTS", "PAYMENTID"),
    ("11_returns.sql", "RETURNS", "RETURNID"),
]


def _output_type_handler(cursor, metadata):
    # NUMBER must come back as exact Decimal, not float - float would
    # introduce round-off artifacts (e.g. 19.99 -> 19.989999999999998) into
    # generated literals for money/percentage columns.
    if metadata.type_code is oracledb.DB_TYPE_NUMBER:
        return cursor.var(decimal.Decimal, arraysize=cursor.arraysize)
    return None


def _literal(value, type_code) -> str:
    if value is None:
        return "NULL"
    if type_code is oracledb.DB_TYPE_NUMBER:
        return str(value)
    if type_code is oracledb.DB_TYPE_DATE:
        return "TO_DATE('{}', 'YYYY-MM-DD HH24:MI:SS')".format(value.strftime("%Y-%m-%d %H:%M:%S"))
    if type_code in (oracledb.DB_TYPE_TIMESTAMP, oracledb.DB_TYPE_TIMESTAMP_TZ, oracledb.DB_TYPE_TIMESTAMP_LTZ):
        millis = value.microsecond // 1000
        return "TO_TIMESTAMP('{}.{:03d}', 'YYYY-MM-DD HH24:MI:SS.FF3')".format(
            value.strftime("%Y-%m-%d %H:%M:%S"), millis
        )
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


def generate_table(cur, file_name: str, table: str, order_by: str) -> int:
    cur.execute(f"SELECT * FROM {table} ORDER BY {order_by}")  # noqa: S608 - table names are our own constant list
    columns = [d[0] for d in cur.description]
    type_codes = [d[1] for d in cur.description]
    col_list = ", ".join(columns)

    lines = [
        f"-- {table} - generated from the live database by generate_data_scripts.py.",
        "-- Do not hand-edit; regenerate instead so it stays a faithful snapshot.",
        "",
    ]
    count = 0
    for row in cur:
        values = ", ".join(_literal(v, t) for v, t in zip(row, type_codes, strict=True))
        lines.append(f"INSERT INTO {table} ({col_list}) VALUES ({values});")
        count += 1

    (DATA_DIR / file_name).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return count


def main() -> None:
    settings = get_settings()
    conn = oracledb.connect(user=settings.oracle_user, password=settings.oracle_password, dsn=settings.oracle_dsn)
    conn.outputtypehandler = _output_type_handler
    cur = conn.cursor()

    DATA_DIR.mkdir(exist_ok=True)
    total = 0
    for file_name, table, order_by in TABLES:
        n = generate_table(cur, file_name, table, order_by)
        print(f"{file_name}: {n} rows")
        total += n
    conn.close()
    print(f"\n{total} rows written across {len(TABLES)} files in {DATA_DIR}")


if __name__ == "__main__":
    main()
