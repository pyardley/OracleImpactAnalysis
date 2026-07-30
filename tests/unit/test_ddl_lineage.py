from oia.graph.model import column_node_id, object_node_id
from oia.lineage.ddl_lineage import build_ddl_lineage_edges

OWNER = "RETAILDEMO"

TABLE_COLUMNS = {
    "CUSTOMERS": ["CUSTOMER_ID", "CUSTOMER_NAME", "REGION_ID"],
    "REGIONS": ["REGION_ID", "REGION_NAME"],
    "V_CUSTOMER_SUMMARY": ["CUSTOMER_ID", "FULL_LABEL"],
}

VIEW_SQL = """
SELECT c.customer_id AS customer_id,
       UPPER(c.customer_name) || '-' || r.region_name AS full_label
FROM customers c
JOIN regions r ON c.region_id = r.region_id
"""


def _seed(store):
    col_rows = []
    node_ids = set()
    for table, cols in TABLE_COLUMNS.items():
        node_ids.add(object_node_id(OWNER, table))
        for col in cols:
            col_rows.append((OWNER, table, col, "VARCHAR2", "Y", 1))
            node_ids.add(column_node_id(OWNER, table, col))
    store.bulk_insert("raw_columns", ["owner", "object_name", "column_name", "data_type", "nullable", "column_id"], col_rows)
    store.bulk_insert("raw_view_text", ["owner", "view_name", "text", "is_mview"], [(OWNER, "V_CUSTOMER_SUMMARY", VIEW_SQL, 0)])
    store.commit()
    return node_ids


def test_ddl_lineage_resolves_simple_passthrough_column(store):
    node_ids = _seed(store)
    edges, parse_errors = build_ddl_lineage_edges(store, node_ids)
    assert parse_errors == 0

    passthrough = [e for e in edges if e.dst_node_id == column_node_id(OWNER, "CUSTOMERS", "CUSTOMER_ID")]
    assert len(passthrough) == 1
    edge = passthrough[0]
    assert edge.src_node_id == column_node_id(OWNER, "V_CUSTOMER_SUMMARY", "CUSTOMER_ID")
    assert edge.edge_type == "DERIVED_FROM"
    assert edge.confidence == "high"
    assert edge.method == "ddl_parse"


def test_ddl_lineage_resolves_multi_source_expression(store):
    node_ids = _seed(store)
    edges, _ = build_ddl_lineage_edges(store, node_ids)

    full_label_id = column_node_id(OWNER, "V_CUSTOMER_SUMMARY", "FULL_LABEL")
    sources = {e.dst_node_id for e in edges if e.src_node_id == full_label_id}
    assert sources == {
        column_node_id(OWNER, "CUSTOMERS", "CUSTOMER_NAME"),
        column_node_id(OWNER, "REGIONS", "REGION_NAME"),
    }
    for e in edges:
        if e.src_node_id == full_label_id:
            assert e.transform_expression  # the actual UPPER(...)/concat expression, not None


def test_ddl_lineage_skips_objects_outside_node_ids(store):
    _seed(store)
    edges, parse_errors = build_ddl_lineage_edges(store, node_ids=set())
    assert edges == []
    assert parse_errors == 0
