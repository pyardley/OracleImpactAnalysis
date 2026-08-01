"""Claude tool definitions (PROMPT.md 5.5) - closures over one loaded graph,
decorated with @beta_tool so the SDK infers each tool's JSON schema from type
hints and this docstring. Every tool returns a JSON string, never prose: the
agent's job is composing a cited answer over this raw, ground-truth data, not
independent SQL reasoning (see oia.agent.loop's system prompt).
"""

from __future__ import annotations

import json

import networkx as nx
from anthropic.lib.tools import beta_tool

from oia.cli.resolve import ResolutionError, resolve_column, resolve_object
from oia.graph.traversal import (
    PathStep,
    affected_objects,
    impact_downstream,
    sources_of,
    trace_upstream,
)


def _step_dict(step: PathStep) -> dict:
    return {
        "edge_type": step.edge_type,
        "src": step.src,
        "dst": step.dst,
        "confidence": step.confidence,
        "method": step.method,
        "source_object": step.source_object,
        "transform_expression": step.transform_expression,
        "filter_expression": step.filter_expression,
    }


def _resolve_object_or_column(g: nx.MultiDiGraph, ref: str) -> str:
    try:
        return resolve_object(g, ref)
    except ResolutionError:
        return resolve_column(g, ref)  # lets ResolutionError propagate if this also fails


def _direct_edges(g: nx.MultiDiGraph, node_id: str) -> tuple[list[dict], list[dict]]:
    """One-hop edges touching node_id, in both directions - includes FK/CALLS
    structural edges that trace_column_lineage deliberately excludes (it only
    follows DERIVED_FROM/READS_FROM). Used so "what does X reference" questions
    are answerable from real graph data instead of the model guessing from
    naming conventions."""
    out_edges = [
        {
            "edge_type": d["edge_type"],
            "points_to": v,
            "confidence": d["confidence"],
            "method": d["method"],
            "transform_expression": d.get("transform_expression"),
            "filter_expression": d.get("filter_expression"),
        }
        for _u, v, d in g.out_edges(node_id, data=True)
    ]
    in_edges = [
        {
            "edge_type": d["edge_type"],
            "referenced_by": u,
            "confidence": d["confidence"],
            "method": d["method"],
            "transform_expression": d.get("transform_expression"),
            "filter_expression": d.get("filter_expression"),
        }
        for u, _v, d in g.in_edges(node_id, data=True)
    ]
    return out_edges, in_edges


def build_tools(g: nx.MultiDiGraph, unresolved: list[dict], sources: dict[str, str] | None = None) -> list:
    """Builds the tool set bound to one hydrated graph + unresolved-lineage
    list + raw object source text (`sources`, keyed by node id - see
    oia.graph.sources.load_object_sources)."""
    sources = sources or {}

    @beta_tool
    def search_objects(name_pattern: str, object_types: list[str] | None = None) -> str:
        """Fuzzy-search extracted database objects by a name substring, optionally
        filtered by type. Use this first to resolve a natural-language reference
        (e.g. "the customer table") to a real OWNER.OBJECT_NAME before calling
        the other tools.

        Args:
            name_pattern: Substring to search for in object names (case-insensitive).
            object_types: Optional filter, e.g. ["TABLE", "VIEW", "PROCEDURE"].
        """
        pat = name_pattern.upper()
        types = {t.upper() for t in object_types} if object_types else None
        matches = [
            {
                "node_id": n,
                "owner": d["owner"],
                "object_name": d["object_name"],
                "node_type": d["node_type"],
                "is_report": d.get("is_report", False),
            }
            for n, d in g.nodes(data=True)
            if d.get("node_type") != "COLUMN"
            and pat in d["object_name"].upper()
            and (not types or d["node_type"] in types)
        ]
        return json.dumps({"matches": matches[:50], "total_matches": len(matches)})

    @beta_tool
    def get_object_metadata(object_name: str) -> str:
        """Get columns, data types, comments, and direct (one-hop) relationships
        for a table/view/procedure/etc - including foreign keys and CALLS edges,
        which trace_column_lineage deliberately excludes since those are
        structural, not data-derivation. This is the tool for "what does column
        X reference" or "what calls this procedure" questions; use
        trace_column_lineage/impact_of_change for lineage and multi-hop impact.

        Args:
            object_name: OBJECT_NAME or OWNER.OBJECT_NAME.
        """
        try:
            node_id = resolve_object(g, object_name)
        except ResolutionError as exc:
            return json.dumps({"error": str(exc)})
        data = g.nodes[node_id]
        column_nodes = [
            (n, d)
            for n, d in g.nodes(data=True)
            if d.get("node_type") == "COLUMN"
            and d["owner"] == data["owner"]
            and d["object_name"] == data["object_name"]
        ]
        columns = []
        for n, d in column_nodes:
            col_out, col_in = _direct_edges(g, n)
            columns.append(
                {
                    "column_name": d["column_name"],
                    "data_type": d["data_type"],
                    **d.get("metadata", {}),
                    "references": col_out,
                    "referenced_by": col_in,
                }
            )
        obj_out, obj_in = _direct_edges(g, node_id)
        return json.dumps(
            {
                "node_id": node_id,
                "node_type": data["node_type"],
                "is_report": data.get("is_report", False),
                "metadata": data.get("metadata", {}),
                "references": obj_out,
                "referenced_by": obj_in,
                "columns": columns,
            }
        )

    @beta_tool
    def trace_column_lineage(table: str, column: str, max_depth: int = 10) -> str:
        """Trace a column back to its base-table sources (upstream lineage).
        Returns the full edge paths with confidence and transform expressions -
        always cite these paths, and caveat any path whose confidence is "low"
        or "none" (dynamic SQL / heuristic parsing) instead of asserting it plainly.

        Args:
            table: Table or view name (owner prefix optional).
            column: Column name.
            max_depth: Maximum number of hops to trace upstream.
        """
        try:
            node_id = resolve_column(g, f"{table}.{column}")
        except ResolutionError as exc:
            return json.dumps({"error": str(exc)})
        result = trace_upstream(g, node_id, max_depth=max_depth)
        return json.dumps(
            {
                "start": node_id,
                "base_sources": sources_of(result),
                "paths": {k: [_step_dict(s) for s in v] for k, v in result.visited.items()},
                "incomplete": result.incomplete,
                "incomplete_reasons": result.incomplete_reasons,
                "cycles_detected": result.cycles,
                "truncated_at_max_depth": result.frontier_cut_off,
            }
        )

    @beta_tool
    def impact_of_change(object_name: str, max_depth: int = 10, reports_only: bool = False) -> str:
        """Find everything downstream of a table/view/procedure/column - the blast
        radius of changing it. Object-level results are always complete (derived
        from Oracle's own dependency dictionary); column-level results additionally
        appear wherever DDL/PL-SQL lineage parsing succeeded for that path.

        Args:
            object_name: OBJECT_NAME, OWNER.OBJECT_NAME, or OWNER.OBJECT.COLUMN.
            max_depth: Maximum number of hops to traverse downstream.
            reports_only: If true, only return objects tagged as reports.
        """
        try:
            node_id = _resolve_object_or_column(g, object_name)
        except ResolutionError as exc:
            return json.dumps({"error": str(exc)})
        result = impact_downstream(g, node_id, max_depth=max_depth)
        objects = affected_objects(g, result)
        if reports_only:
            objects = [o for o in objects if g.nodes.get(o, {}).get("is_report")]
        return json.dumps(
            {
                "start": node_id,
                "affected_objects": objects,
                "paths": {k: [_step_dict(s) for s in v] for k, v in result.visited.items()},
                "incomplete": result.incomplete,
                "incomplete_reasons": result.incomplete_reasons,
                "cycles_detected": result.cycles,
                "truncated_at_max_depth": result.frontier_cut_off,
            }
        )

    @beta_tool
    def list_reports_affected(object_name: str, max_depth: int = 10) -> str:
        """Convenience filter over impact_of_change: only the report-tagged
        objects affected by changing object_name.

        Args:
            object_name: OBJECT_NAME, OWNER.OBJECT_NAME, or OWNER.OBJECT.COLUMN.
            max_depth: Maximum number of hops to traverse downstream.
        """
        try:
            node_id = _resolve_object_or_column(g, object_name)
        except ResolutionError as exc:
            return json.dumps({"error": str(exc)})
        result = impact_downstream(g, node_id, max_depth=max_depth)
        reports = [o for o in affected_objects(g, result) if g.nodes.get(o, {}).get("is_report")]
        return json.dumps({"start": node_id, "reports": reports})

    @beta_tool
    def get_object_source(object_name: str) -> str:
        """Get the actual PL/SQL source code (procedure/function/trigger body,
        or the defining SELECT for a view). This is ground truth, not a
        parser's approximation - use it to confirm or quote the *exact*
        formula/logic instead of guessing from a function's name or from a
        transform_expression snippet, and to see multi-statement structure
        (e.g. staged CTEs, WHERE-clause eligibility rules) that no single
        lineage edge captures on its own. Not every object has source (e.g. a
        base TABLE has none).

        Args:
            object_name: OBJECT_NAME or OWNER.OBJECT_NAME.
        """
        try:
            node_id = resolve_object(g, object_name)
        except ResolutionError as exc:
            return json.dumps({"error": str(exc)})
        source = sources.get(node_id)
        if source is None:
            return json.dumps({"node_id": node_id, "source": None, "note": "No source text for this object type."})
        return json.dumps({"node_id": node_id, "source": source})

    @beta_tool
    def get_unresolved_lineage(object_name: str) -> str:
        """List dynamic-SQL / parse-failure gap markers recorded for an object -
        things OIA could not statically resolve. If a trace or impact result
        passes through this object, caveat your answer using what's returned here.

        Args:
            object_name: Object name (owner prefix optional).
        """
        name = object_name.split(".")[-1].upper()
        rows = [r for r in unresolved if r["object_name"].upper() == name]
        return json.dumps({"unresolved": rows})

    return [
        search_objects,
        get_object_metadata,
        get_object_source,
        trace_column_lineage,
        impact_of_change,
        list_reports_affected,
        get_unresolved_lineage,
    ]
