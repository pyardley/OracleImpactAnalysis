from oia.graph.model import column_node_id, object_node_id
from oia.lineage.plsql_lineage import build_plsql_lineage_edges

OWNER = "RETAILDEMO"

PROC_BODY = """
PROCEDURE SYNC_STAGING (p_table_name IN VARCHAR2) IS
BEGIN
  INSERT INTO staging_customer_segment (customer_id, customer_name, segment, region_id)
  SELECT c.customer_id, c.customer_name, c.segment, c.region_id
  FROM customers c
  WHERE c.is_active = 1;

  UPDATE customers SET segment = 'VIP' WHERE customer_id = 1;

  EXECUTE IMMEDIATE 'TRUNCATE TABLE ' || p_table_name;
END SYNC_STAGING;
"""

CUSTOMER_COLUMNS = ["CUSTOMER_ID", "CUSTOMER_NAME", "SEGMENT", "REGION_ID", "IS_ACTIVE"]
STAGING_COLUMNS = ["CUSTOMER_ID", "CUSTOMER_NAME", "SEGMENT", "REGION_ID"]


def _seed(store):
    node_ids = {
        object_node_id(OWNER, "CUSTOMERS"),
        object_node_id(OWNER, "STAGING_CUSTOMER_SEGMENT"),
        object_node_id(OWNER, "SYNC_STAGING"),
    }
    col_rows = []
    for col in CUSTOMER_COLUMNS:
        col_rows.append((OWNER, "CUSTOMERS", col, "VARCHAR2", "Y", 1))
        node_ids.add(column_node_id(OWNER, "CUSTOMERS", col))
    for col in STAGING_COLUMNS:
        col_rows.append((OWNER, "STAGING_CUSTOMER_SEGMENT", col, "VARCHAR2", "Y", 1))
        node_ids.add(column_node_id(OWNER, "STAGING_CUSTOMER_SEGMENT", col))
    store.bulk_insert(
        "raw_columns", ["owner", "object_name", "column_name", "data_type", "nullable", "column_id"], col_rows
    )
    store.bulk_insert(
        "raw_source", ["owner", "object_name", "object_type", "body"], [(OWNER, "SYNC_STAGING", "PROCEDURE", PROC_BODY)]
    )
    store.commit()
    return node_ids


def test_insert_select_produces_column_level_lineage(store):
    node_ids = _seed(store)
    edges, _parse_errors = build_plsql_lineage_edges(store, node_ids)

    derived = [e for e in edges if e.edge_type == "DERIVED_FROM"]
    pairs = {(e.src_node_id, e.dst_node_id) for e in derived}
    assert (
        column_node_id(OWNER, "STAGING_CUSTOMER_SEGMENT", "CUSTOMER_ID"),
        column_node_id(OWNER, "CUSTOMERS", "CUSTOMER_ID"),
    ) in pairs
    for e in derived:
        assert e.confidence == "low"
        assert e.method == "plsql_static_analysis"


def test_insert_and_update_produce_writes_to_edges(store):
    node_ids = _seed(store)
    edges, _ = build_plsql_lineage_edges(store, node_ids)

    writes = {e.dst_node_id for e in edges if e.edge_type == "WRITES_TO"}
    assert object_node_id(OWNER, "STAGING_CUSTOMER_SEGMENT") in writes
    assert object_node_id(OWNER, "CUSTOMERS") in writes
    for e in edges:
        if e.edge_type == "WRITES_TO":
            assert e.src_node_id == object_node_id(OWNER, "SYNC_STAGING")


def test_dynamic_sql_recorded_as_unresolved_not_guessed(store):
    node_ids = _seed(store)
    build_plsql_lineage_edges(store, node_ids)

    rows = store.unresolved_lineage()
    assert len(rows) == 1
    assert rows[0]["object_name"] == "SYNC_STAGING"
    assert "EXECUTE IMMEDIATE" in rows[0]["raw_text"].upper()


FILTER_PROC_BODY = """
PROCEDURE SCAN_ACTIVE_ORDERS IS
  v_id NUMBER;
BEGIN
  SELECT c.customer_id INTO v_id
  FROM customers c
  JOIN orders o ON o.customer_id = c.customer_id AND o.status = 'Completed'
  WHERE c.is_active = 1;
END SCAN_ACTIVE_ORDERS;
"""


def test_join_on_and_where_conditions_captured_as_filter_expression(store):
    """Regression test mirroring the real ChurnRisk procedure's pattern:
    an eligibility condition embedded in a JOIN...ON clause (not just a plain
    WHERE) must still surface as filter_expression on the resulting
    object-level READS_FROM edges - this is exactly how
    'only Completed orders' was previously invisible in OIA's graph.
    """
    node_ids = {
        object_node_id(OWNER, "CUSTOMERS"),
        object_node_id(OWNER, "ORDERS"),
        object_node_id(OWNER, "SCAN_ACTIVE_ORDERS"),
    }
    store.bulk_insert(
        "raw_source",
        ["owner", "object_name", "object_type", "body"],
        [(OWNER, "SCAN_ACTIVE_ORDERS", "PROCEDURE", FILTER_PROC_BODY)],
    )
    store.commit()

    edges, _parse_errors = build_plsql_lineage_edges(store, node_ids)
    reads = {e.dst_node_id: e for e in edges if e.edge_type == "READS_FROM"}

    assert object_node_id(OWNER, "CUSTOMERS") in reads
    assert object_node_id(OWNER, "ORDERS") in reads
    for edge in reads.values():
        assert edge.filter_expression is not None
        assert "IS_ACTIVE" in edge.filter_expression.upper()
        assert "STATUS" in edge.filter_expression.upper()
        assert "COMPLETED" in edge.filter_expression.upper()


MARGIN_PROC_BODY = """
PROCEDURE BUILD_MARGIN_REPORT IS
BEGIN
  INSERT INTO report_product_performance (product_id, total_revenue, total_cost, margin_pct)
  SELECT product_id, total_revenue, total_cost,
         CASE WHEN total_revenue = 0 THEN NULL ELSE ROUND((total_revenue - total_cost) / total_revenue * 100, 2) END
  FROM staging_product_agg;
END BUILD_MARGIN_REPORT;
"""


def test_insert_select_resolves_unaliased_computed_expression(store):
    """Regression test: an INSERT...SELECT target column whose corresponding
    SELECT-list expression has no explicit alias (relying on the INSERT's own
    column list for its name, which Oracle allows) must still resolve -
    sqlglot.lineage.lineage() looks columns up by name, and an unaliased
    CASE/arithmetic expression's alias_or_name is empty, so naively using it
    as the lookup key silently drops the edge instead of erroring.
    """
    node_ids = {
        object_node_id(OWNER, "STAGING_PRODUCT_AGG"),
        object_node_id(OWNER, "REPORT_PRODUCT_PERFORMANCE"),
        object_node_id(OWNER, "BUILD_MARGIN_REPORT"),
    }
    staging_cols = ["PRODUCT_ID", "TOTAL_REVENUE", "TOTAL_COST"]
    report_cols = ["PRODUCT_ID", "TOTAL_REVENUE", "TOTAL_COST", "MARGIN_PCT"]
    col_rows = []
    for col in staging_cols:
        col_rows.append((OWNER, "STAGING_PRODUCT_AGG", col, "NUMBER", "Y", 1))
        node_ids.add(column_node_id(OWNER, "STAGING_PRODUCT_AGG", col))
    for col in report_cols:
        col_rows.append((OWNER, "REPORT_PRODUCT_PERFORMANCE", col, "NUMBER", "Y", 1))
        node_ids.add(column_node_id(OWNER, "REPORT_PRODUCT_PERFORMANCE", col))
    store.bulk_insert(
        "raw_columns", ["owner", "object_name", "column_name", "data_type", "nullable", "column_id"], col_rows
    )
    store.bulk_insert(
        "raw_source",
        ["owner", "object_name", "object_type", "body"],
        [(OWNER, "BUILD_MARGIN_REPORT", "PROCEDURE", MARGIN_PROC_BODY)],
    )
    store.commit()

    edges, _parse_errors = build_plsql_lineage_edges(store, node_ids)
    derived = {(e.src_node_id, e.dst_node_id) for e in edges if e.edge_type == "DERIVED_FROM"}

    margin_pct_id = column_node_id(OWNER, "REPORT_PRODUCT_PERFORMANCE", "MARGIN_PCT")
    sources = {dst for src, dst in derived if src == margin_pct_id}
    assert sources == {
        column_node_id(OWNER, "STAGING_PRODUCT_AGG", "TOTAL_REVENUE"),
        column_node_id(OWNER, "STAGING_PRODUCT_AGG", "TOTAL_COST"),
    }


CTE_AGG_PROC_BODY = """
PROCEDURE BUILD_CUSTOMER_TOTALS IS
BEGIN
  INSERT INTO report_customer_totals (customer_id, total_net_amount)
  WITH agg AS (
      SELECT customer_id, SUM(net_amount) AS total_net_amount
      FROM active_orders
      WHERE is_eligible = 1
      GROUP BY customer_id
  )
  SELECT customer_id, total_net_amount FROM agg;
END BUILD_CUSTOMER_TOTALS;
"""


def test_insert_select_captures_real_transform_through_a_cte(store):
    """Regression test: a leaf's own sqlglot Node.expression is just its
    FROM-clause table reference (e.g. "ACTIVE_ORDERS AS ACTIVE_ORDERS"), not
    the computation that produced it - the real transform (here SUM(...))
    lives on the leaf's parent node, one hop closer to the root. Using the
    leaf's own expression silently records garbled table-alias text instead
    of the aggregate/function that's actually the point of a lineage tool.
    Also verifies the WHERE clause gating which rows count is captured as
    `filter_expression` - eligibility criteria a plain column-to-column edge
    wouldn't otherwise reveal.
    """
    node_ids = {
        object_node_id(OWNER, "ACTIVE_ORDERS"),
        object_node_id(OWNER, "REPORT_CUSTOMER_TOTALS"),
        object_node_id(OWNER, "BUILD_CUSTOMER_TOTALS"),
    }
    col_rows = [
        (OWNER, "ACTIVE_ORDERS", "CUSTOMER_ID", "NUMBER", "Y", 1),
        (OWNER, "ACTIVE_ORDERS", "NET_AMOUNT", "NUMBER", "Y", 2),
        (OWNER, "REPORT_CUSTOMER_TOTALS", "CUSTOMER_ID", "NUMBER", "Y", 1),
        (OWNER, "REPORT_CUSTOMER_TOTALS", "TOTAL_NET_AMOUNT", "NUMBER", "Y", 2),
    ]
    for row in col_rows:
        node_ids.add(column_node_id(OWNER, row[1], row[2]))
    store.bulk_insert(
        "raw_columns", ["owner", "object_name", "column_name", "data_type", "nullable", "column_id"], col_rows
    )
    store.bulk_insert(
        "raw_source",
        ["owner", "object_name", "object_type", "body"],
        [(OWNER, "BUILD_CUSTOMER_TOTALS", "PROCEDURE", CTE_AGG_PROC_BODY)],
    )
    store.commit()

    edges, _parse_errors = build_plsql_lineage_edges(store, node_ids)
    total_id = column_node_id(OWNER, "REPORT_CUSTOMER_TOTALS", "TOTAL_NET_AMOUNT")
    matches = [
        e for e in edges
        if e.edge_type == "DERIVED_FROM"
        and e.src_node_id == total_id
        and e.dst_node_id == column_node_id(OWNER, "ACTIVE_ORDERS", "NET_AMOUNT")
    ]
    assert len(matches) == 1
    transform = matches[0].transform_expression
    assert transform is not None
    assert "SUM" in transform.upper()
    assert "NET_AMOUNT" in transform.upper()
    # the bug produced the FROM-clause table reference instead
    assert "ACTIVE_ORDERS AS ACTIVE_ORDERS" != transform.upper().replace('"', "")

    filter_expr = matches[0].filter_expression
    assert filter_expr is not None
    assert "IS_ELIGIBLE" in filter_expr.upper()


def test_trigger_gets_free_writes_to_edge(store):
    node_ids = {object_node_id(OWNER, "AUDITLOG"), object_node_id(OWNER, "TRG_AUDIT_ORDERS")}
    store.bulk_insert(
        "raw_triggers",
        ["owner", "trigger_name", "table_owner", "table_name", "triggering_event", "trigger_type", "body"],
        [(OWNER, "TRG_AUDIT_ORDERS", OWNER, "AUDITLOG", "INSERT", "AFTER EACH ROW", "BEGIN NULL; END;")],
    )
    store.commit()
    edges, _ = build_plsql_lineage_edges(store, node_ids)
    trigger_edges = [e for e in edges if e.edge_type == "WRITES_TO"]
    assert len(trigger_edges) == 1
    assert trigger_edges[0].src_node_id == object_node_id(OWNER, "TRG_AUDIT_ORDERS")
    assert trigger_edges[0].dst_node_id == object_node_id(OWNER, "AUDITLOG")
    assert trigger_edges[0].confidence == "high"
