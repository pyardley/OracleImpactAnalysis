from __future__ import annotations

import json as json_lib
import sys
from pathlib import Path

import networkx as nx
import typer
from rich.console import Console
from rich.table import Table

if sys.platform == "win32":
    # Claude's answers (and view/PLSQL source text) routinely contain Unicode
    # punctuation (arrows, em-dashes, ...) that the legacy Windows console's
    # codepage (cp1252) can't encode - rich's legacy Win32 renderer crashes on
    # it outright. Force UTF-8 stdout/stderr and skip that renderer entirely;
    # both Windows Terminal and the modern PowerShell/cmd hosts render ANSI fine.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from oia.cli.resolve import ResolutionError, resolve_column, resolve_object
from oia.config.settings import ConfigError, Settings, get_settings
from oia.extraction.oracle_metadata import ExtractionError, extract_metadata
from oia.graph.model import load_graph
from oia.graph.pipeline import build_full_graph
from oia.graph.traversal import (
    affected_objects,
    impact_downstream,
    sources_of,
    trace_upstream,
)
from oia.lineage.overrides import add_override, read_overrides
from oia.storage.sqlite_store import SqliteStore

app = typer.Typer(add_completion=False, help="OIA - Oracle Impact Analysis")
graph_app = typer.Typer(help="Graph maintenance and export")
objects_app = typer.Typer(help="Browse extracted objects")
override_app = typer.Typer(help="Manage manual lineage overrides (config/lineage_overrides.yaml)")
app.add_typer(graph_app, name="graph")
app.add_typer(objects_app, name="objects")
app.add_typer(override_app, name="override")

console = Console(legacy_windows=False)


def _settings() -> Settings:
    try:
        return get_settings()
    except ConfigError as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
        raise typer.Exit(1) from exc


def _store(settings: Settings) -> SqliteStore:
    return SqliteStore(settings.sqlite_path)


def _load_graph(settings: Settings) -> nx.MultiDiGraph:
    store = _store(settings)
    try:
        g = load_graph(store)
    finally:
        store.close()
    if g.number_of_nodes() == 0:
        console.print("[yellow]The graph is empty - run `oia extract` first.[/yellow]")
        raise typer.Exit(1)
    return g


def _step_dict(step) -> dict:
    return {
        "edge_type": step.edge_type,
        "src": step.src,
        "dst": step.dst,
        "confidence": step.confidence,
        "method": step.method,
        "source_object": step.source_object,
        "transform_expression": step.transform_expression,
    }


# ---- extract -----------------------------------------------------------------


@app.command()
def extract(
    incremental: bool = typer.Option(
        False, "--incremental", help="Skip re-parsing lineage for objects whose LAST_DDL_TIME is unchanged"
    ),
    dictionary_scope: str = typer.Option(None, "--dictionary-scope", help="Override config.yaml for this run"),
) -> None:
    """Pull Oracle metadata and (re)build the lineage graph."""
    settings = _settings()
    if dictionary_scope:
        settings.dictionary_scope = dictionary_scope
    store = _store(settings)
    run_id = store.start_run("incremental" if incremental else "full", ",".join(settings.schemas.include) or None)
    try:
        stats = extract_metadata(settings, store)
        graph_stats = build_full_graph(settings, store, incremental=incremental)
    except (ExtractionError, ConfigError) as exc:
        store.finish_run(run_id, 0, 1, 0)
        store.close()
        console.print(f"[red]Extraction failed:[/red] {exc}")
        raise typer.Exit(1) from exc

    store.finish_run(run_id, stats.objects_processed, stats.objects_failed, graph_stats.parse_errors)
    store.close()

    console.print(f"[green]Extracted {stats.objects_processed} objects.[/green]")
    console.print(
        f"Graph: {graph_stats.node_count} nodes, {graph_stats.edge_count} edges, "
        f"{graph_stats.parse_errors} parse errors."
    )
    if incremental:
        console.print(f"  reused (unchanged): {graph_stats.objects_reused} objects")
    for conf, n in sorted((graph_stats.edges_by_confidence or {}).items()):
        console.print(f"  confidence={conf}: {n}")


# ---- trace / impact ------------------------------------------------------------


@app.command()
def trace(
    ref: str = typer.Argument(..., help="TABLE.COLUMN or OWNER.TABLE.COLUMN"),
    max_depth: int = typer.Option(10, "--max-depth"),
    fmt: str = typer.Option("text", "--format", help="text | json"),
) -> None:
    """Trace a column back to its base-table sources (upstream lineage)."""
    settings = _settings()
    g = _load_graph(settings)
    try:
        node_id = resolve_column(g, ref)
    except ResolutionError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    result = trace_upstream(g, node_id, max_depth=max_depth)
    sources = sources_of(result)

    if fmt == "json":
        payload = {
            "start": node_id,
            "sources": sources,
            "paths": {k: [_step_dict(s) for s in v] for k, v in result.visited.items()},
            "incomplete": result.incomplete,
            "incomplete_reasons": result.incomplete_reasons,
            "cycles": result.cycles,
            "truncated_at_max_depth": result.frontier_cut_off,
        }
        console.print_json(json_lib.dumps(payload))
        return

    console.print(f"Tracing [bold]{node_id}[/bold] upstream (max-depth={max_depth}):\n")
    if not result.visited:
        console.print("  No lineage found - this looks like a base/source column (nothing derives it).")
    else:
        for path in result.visited.values():
            chain = " -> ".join([node_id, *[s.dst for s in path]])
            last = path[-1]
            console.print(f"  {chain}  [dim]({last.confidence}, {last.method})[/dim]")
        console.print(f"\n[green]Base sources:[/green] {', '.join(sources) if sources else '(none found)'}")
    if result.frontier_cut_off:
        console.print(f"\n[yellow]Truncated at --max-depth={max_depth}:[/yellow] {result.frontier_cut_off}")
    if result.incomplete:
        console.print("\n[yellow]May be incomplete - crosses low/none-confidence edges:[/yellow]")
        for reason in result.incomplete_reasons:
            console.print(f"  - {reason}")
    if result.cycles:
        console.print("\n[yellow]Cycle(s) detected:[/yellow]")
        for c in result.cycles:
            console.print("  " + " -> ".join(c))


@app.command()
def impact(
    ref: str = typer.Argument(..., help="OBJECT_NAME, OWNER.OBJECT_NAME, or OWNER.OBJECT.COLUMN"),
    max_depth: int = typer.Option(10, "--max-depth"),
    reports_only: bool = typer.Option(False, "--reports-only"),
    fmt: str = typer.Option("text", "--format", help="text | table | json"),
) -> None:
    """Find everything downstream of a table/view/procedure/column (blast radius)."""
    settings = _settings()
    g = _load_graph(settings)
    try:
        node_id = resolve_object(g, ref)
    except ResolutionError as obj_err:
        try:
            node_id = resolve_column(g, ref)
        except ResolutionError:
            console.print(f"[red]{obj_err}[/red]")
            raise typer.Exit(1) from obj_err

    result = impact_downstream(g, node_id, max_depth=max_depth)
    objects = affected_objects(g, result)
    if reports_only:
        objects = [o for o in objects if g.nodes.get(o, {}).get("is_report")]

    if fmt == "json":
        payload = {
            "start": node_id,
            "affected_objects": objects,
            "affected_nodes": list(result.visited.keys()),
            "paths": {k: [_step_dict(s) for s in v] for k, v in result.visited.items()},
            "incomplete": result.incomplete,
            "incomplete_reasons": result.incomplete_reasons,
            "cycles": result.cycles,
            "truncated_at_max_depth": result.frontier_cut_off,
        }
        console.print_json(json_lib.dumps(payload))
        return

    console.print(f"Impact of changing [bold]{node_id}[/bold] (max-depth={max_depth}):\n")
    if not objects:
        console.print("  Nothing else in the graph appears to depend on this.")
    else:
        table = Table()
        table.add_column("Object")
        table.add_column("Type")
        table.add_column("Report?")
        for obj in objects:
            data = g.nodes.get(obj, {})
            table.add_row(obj, data.get("node_type", "?"), "yes" if data.get("is_report") else "")
        console.print(table)
    if result.frontier_cut_off:
        console.print(f"\n[yellow]Truncated at --max-depth={max_depth}:[/yellow] {result.frontier_cut_off}")
    if result.incomplete:
        console.print("\n[yellow]May be incomplete - crosses low/none-confidence edges:[/yellow]")
        for reason in result.incomplete_reasons:
            console.print(f"  - {reason}")
    if result.cycles:
        console.print("\n[yellow]Cycle(s) detected:[/yellow]")
        for c in result.cycles:
            console.print("  " + " -> ".join(c))


# ---- graph ---------------------------------------------------------------------


@graph_app.command("stats")
def graph_stats() -> None:
    settings = _settings()
    store = _store(settings)
    run = store.latest_run()
    nodes = store.all_nodes()
    edges = store.all_edges()
    unresolved = store.unresolved_lineage()
    store.close()

    if run:
        console.print(
            f"Last run: #{run['run_id']} ({run['mode']}) started {run['started_at']}, "
            f"finished {run['finished_at']}"
        )
        console.print(
            f"  objects_processed={run['objects_processed']} objects_failed={run['objects_failed']} "
            f"parse_errors={run['parse_errors_count']}"
        )

    by_type: dict[str, int] = {}
    for n in nodes:
        by_type[n["node_type"]] = by_type.get(n["node_type"], 0) + 1
    console.print(f"\nNodes: {len(nodes)}")
    for t, n in sorted(by_type.items()):
        console.print(f"  {t}: {n}")

    by_conf: dict[str, int] = {}
    by_edge_type: dict[str, int] = {}
    for e in edges:
        by_conf[e["confidence"]] = by_conf.get(e["confidence"], 0) + 1
        by_edge_type[e["edge_type"]] = by_edge_type.get(e["edge_type"], 0) + 1
    console.print(f"\nEdges: {len(edges)}")
    for t, n in sorted(by_edge_type.items()):
        console.print(f"  {t}: {n}")
    console.print("Confidence breakdown:")
    for c, n in sorted(by_conf.items()):
        console.print(f"  {c}: {n}")

    console.print(f"\nUnresolved lineage (dynamic SQL / gap markers): {len(unresolved)}")
    for row in unresolved[:20]:
        console.print(f"  {row['owner']}.{row['object_name']} line {row['line']}: {row['raw_text'][:80]}")
    if len(unresolved) > 20:
        console.print(f"  ... and {len(unresolved) - 20} more")


@graph_app.command("export")
def graph_export(
    fmt: str = typer.Option("json", "--format", help="json | graphml"),
    output: Path = typer.Option(None, "--output"),
) -> None:
    settings = _settings()
    g = _load_graph(settings)
    out_path = output or Path(f"oia_graph.{('graphml' if fmt == 'graphml' else 'json')}")
    if fmt == "graphml":
        # GraphML can't serialize dict-valued attributes or None - flatten first.
        flat = nx.MultiDiGraph()
        for n, d in g.nodes(data=True):
            flat.add_node(n, **{k: ("" if v is None else str(v)) for k, v in d.items() if k != "metadata"})
        for u, v, d in g.edges(data=True):
            flat.add_edge(u, v, **{k: ("" if val is None else str(val)) for k, val in d.items()})
        nx.write_graphml(flat, out_path)
    else:
        data = nx.node_link_data(g, edges="edges")
        out_path.write_text(json_lib.dumps(data, indent=2, default=str), encoding="utf-8")
    console.print(f"Wrote {out_path}")


# ---- objects ---------------------------------------------------------------------


@objects_app.command("list")
def objects_list(object_type: str = typer.Option(None, "--type"), owner: str = typer.Option(None, "--owner")) -> None:
    settings = _settings()
    store = _store(settings)
    sql = "SELECT owner, object_name, object_type, status, last_ddl_time FROM raw_objects WHERE 1=1"
    params: list[str] = []
    if object_type:
        sql += " AND object_type = ?"
        params.append(object_type.upper())
    if owner:
        sql += " AND owner = ?"
        params.append(owner.upper())
    sql += " ORDER BY owner, object_type, object_name"
    rows = store.query(sql, params)
    store.close()

    table = Table()
    for col in ("Owner", "Name", "Type", "Status", "Last DDL"):
        table.add_column(col)
    for r in rows:
        table.add_row(r["owner"], r["object_name"], r["object_type"], r["status"] or "", r["last_ddl_time"] or "")
    console.print(table)


@objects_app.command("search")
def objects_search(pattern: str) -> None:
    settings = _settings()
    store = _store(settings)
    rows = store.query(
        "SELECT owner, object_name, object_type FROM raw_objects WHERE UPPER(object_name) LIKE ? ORDER BY owner, object_name",
        (f"%{pattern.upper()}%",),
    )
    store.close()
    table = Table()
    for col in ("Owner", "Name", "Type"):
        table.add_column(col)
    for r in rows:
        table.add_row(r["owner"], r["object_name"], r["object_type"])
    console.print(table)


# ---- override ---------------------------------------------------------------------


@override_app.command("list")
def override_list() -> None:
    settings = _settings()
    entries = read_overrides(settings)
    if not entries:
        console.print("No manual lineage overrides defined.")
        return
    for e in entries:
        console.print(
            f"[bold]{e.get('src', '')}[/bold] -({e.get('edge_type', '')})-> [bold]{e.get('dst', '')}[/bold]"
        )
        console.print(f"  note: {e.get('note', '')}")
        console.print(f"  author: {e.get('author', '')}  added: {e.get('added_at', '')}\n")


@override_app.command("add")
def override_add(
    src: str = typer.Option(..., help="OWNER.OBJECT or OWNER.OBJECT.COLUMN"),
    dst: str = typer.Option(..., help="OWNER.OBJECT or OWNER.OBJECT.COLUMN"),
    edge_type: str = typer.Option("DERIVED_FROM", "--edge-type"),
    note: str = typer.Option(..., help="Why this can't be statically resolved"),
    author: str = typer.Option(..., help="Who's asserting this"),
) -> None:
    settings = _settings()
    try:
        add_override(settings, src, dst, edge_type, note, author)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"[green]Added override {src} -> {dst}.[/green] Run `oia extract` to rebuild the graph with it.")


# ---- ask -----------------------------------------------------------------------


@app.command()
def ask(
    question: str = typer.Argument(...),
    model: str = typer.Option(None, "--model"),
    effort: str = typer.Option(None, "--effort"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Ask a natural-language lineage/impact question, answered by a Claude agent
    grounded in the graph (see oia.agent)."""
    from oia.agent.loop import run_agent

    settings = _settings()
    if not settings.anthropic_api_key:
        console.print(
            "[red]ANTHROPIC_API_KEY is not set.[/red] Add it to .env to use `oia ask` "
            "(see .env.example)."
        )
        raise typer.Exit(1)
    if model:
        settings.agent.model = model
    if effort:
        settings.agent.effort = effort

    store = _store(settings)
    try:
        g = load_graph(store)
    finally:
        store.close()
    if g.number_of_nodes() == 0:
        console.print("[yellow]The graph is empty - run `oia extract` first.[/yellow]")
        raise typer.Exit(1)

    answer = run_agent(settings, g, question, console=console, stream=not json_out)
    if json_out:
        console.print_json(json_lib.dumps({"question": question, "answer": answer}))


if __name__ == "__main__":
    app()
