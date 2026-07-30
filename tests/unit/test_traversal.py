import networkx as nx
import pytest

from oia.graph.model import column_node_id, object_node_id
from oia.graph.traversal import (
    affected_objects,
    impact_downstream,
    sources_of,
    trace_upstream,
)

OWNER = "RETAILDEMO"


def _add_object(g, name, node_type="TABLE", is_report=False):
    nid = object_node_id(OWNER, name)
    g.add_node(nid, node_type=node_type, owner=OWNER, object_name=name, column_name=None, is_report=is_report)
    return nid


def _add_column(g, obj, col, data_type="VARCHAR2"):
    nid = column_node_id(OWNER, obj, col)
    g.add_node(nid, node_type="COLUMN", owner=OWNER, object_name=obj, column_name=col, data_type=data_type)
    return nid


def _edge(g, edge_type, src, dst, confidence="high", method="ddl_parse"):
    g.add_edge(src, dst, key=f"{src}->{dst}:{edge_type}", edge_type=edge_type, confidence=confidence, method=method, source_object=src, transform_expression=None)


@pytest.fixture
def g():
    return nx.MultiDiGraph()


def test_trace_upstream_simple_chain(g):
    _add_object(g, "T")
    a = _add_column(g, "V2", "A")
    b = _add_column(g, "V1", "B")
    c = _add_column(g, "T", "C")
    _edge(g, "DERIVED_FROM", a, b)
    _edge(g, "DERIVED_FROM", b, c)

    result = trace_upstream(g, a)
    assert sources_of(result) == [c]
    assert not result.incomplete
    assert result.cycles == []


def test_trace_upstream_base_column_has_no_lineage(g):
    c = _add_column(g, "T", "C")
    result = trace_upstream(g, c)
    assert result.visited == {}
    assert sources_of(result) == [c]


def test_trace_upstream_flags_low_confidence_as_incomplete(g):
    a = _add_column(g, "V", "A")
    b = _add_column(g, "T", "B")
    _edge(g, "DERIVED_FROM", a, b, confidence="low", method="plsql_static_analysis")

    result = trace_upstream(g, a)
    assert result.incomplete
    assert len(result.incomplete_reasons) == 1


def test_trace_upstream_detects_cycle_without_hanging(g):
    a = _add_column(g, "A", "X")
    b = _add_column(g, "B", "X")
    _edge(g, "DERIVED_FROM", a, b)
    _edge(g, "DERIVED_FROM", b, a)  # closes the loop

    result = trace_upstream(g, a, max_depth=20)
    assert result.cycles, "expected a detected cycle"


def test_trace_upstream_respects_max_depth(g):
    nodes = [_add_column(g, f"T{i}", "X") for i in range(6)]
    for i in range(5):
        _edge(g, "DERIVED_FROM", nodes[i], nodes[i + 1])

    result = trace_upstream(g, nodes[0], max_depth=2)
    assert len(result.visited) == 2
    assert result.frontier_cut_off  # truncated, not silently treated as exhaustive


def test_impact_downstream_object_and_column_level_together(g):
    t = _add_object(g, "T")
    t_a = _add_column(g, "T", "A")
    _add_object(g, "V")
    v_x = _add_column(g, "V", "X")
    _edge(g, "DERIVED_FROM", v_x, t_a, confidence="high")  # V.X derived from T.A
    _edge(g, "REFERENCES", "OTHER.PROC", t, confidence="high")  # a caller referencing T directly

    result = impact_downstream(g, t)
    objects = affected_objects(g, result)
    assert object_node_id(OWNER, "V") in objects


def test_impact_downstream_reports_only_filter(g):
    t = _add_object(g, "T")
    t_a = _add_column(g, "T", "A")
    _add_object(g, "REPORT_X", is_report=True)
    rep_col = _add_column(g, "REPORT_X", "A")
    _edge(g, "DERIVED_FROM", rep_col, t_a)

    result = impact_downstream(g, t)
    objects = affected_objects(g, result)
    reports = [o for o in objects if g.nodes[o].get("is_report")]
    assert reports == [object_node_id(OWNER, "REPORT_X")]


def test_impact_downstream_unknown_node_returns_empty(g):
    result = impact_downstream(g, "NOPE.NOPE")
    assert result.start_nodes == []
    assert result.visited == {}
