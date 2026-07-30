# OIA — Oracle Impact Analysis

A CLI tool that builds a lineage/dependency graph from an Oracle database's own
metadata (data dictionary, view DDL, PL/SQL source) and answers questions like:

- *"Trace the source of `CUSTOMERS.EMAIL` back to its source tables."*
- *"What reports will be affected by a code change to stored procedure `X`?"*
- *"What breaks if I drop column `ORDERS.DISCOUNT_PCT`?"*

It works two ways: structured commands (`oia trace`, `oia impact`, ...) for
scripting/CI, and a natural-language mode (`oia ask`) backed by a Claude agent
that calls those same commands as tools and cites its sources.

The full design rationale — architecture, confidence model, non-goals — lives in
[PROMPT.md](PROMPT.md). This README is the practical how-to-run-it guide.

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) for dependency management
- An Oracle database reachable over the network (no Oracle Instant Client
  needed — `oracledb`'s thin mode talks to Oracle directly)
- An [Anthropic API key](https://console.anthropic.com/) — only required for `oia ask`

## Setup

```bash
cd OracleImpactAnalysis
uv sync
```

Copy the env template and fill in real values:

```bash
cp .env.example .env
```

```dotenv
# .env (gitignored — never commit this file)
ORACLE_USER=retaildemo
ORACLE_PASSWORD=your-password-here
ORACLE_DSN=host:1521/SERVICE_NAME

ANTHROPIC_API_KEY=sk-ant-...        # only needed for `oia ask`
```

Everything else — which schemas to include, how "reports" are identified, which
Anthropic model to use — is configured in `config/config.yaml` (checked into
git, no secrets). See [Configuration](#configuration) below.

## Quick start

Pull the schema and build the lineage graph:

```console
$ uv run oia extract
Extracted 19 objects.
Graph: 147 nodes, 14 edges, 0 parse errors.
  confidence=manual: 1
  confidence=medium: 13
```

Trace a column back to its source:

```console
$ uv run oia trace CUSTOMERS.REGIONID
Tracing RETAILDEMO.CUSTOMERS.REGIONID upstream (max-depth=10):

  No lineage found - this looks like a base/source column (nothing derives it).
```

Ask what breaks if a table changes:

```console
$ uv run oia impact REGIONS
Impact of changing RETAILDEMO.REGIONS (max-depth=10):

+-----------------------------------------+
| Object                | Type  | Report? |
|-----------------------+-------+---------|
| RETAILDEMO.CUSTOMERS  | TABLE |         |
| RETAILDEMO.EMPLOYEES  | TABLE |         |
| RETAILDEMO.WAREHOUSES | TABLE |         |
+-----------------------------------------+
```

That result comes from real foreign-key relationships in the schema
(`CUSTOMERS.REGIONID → REGIONS.REGIONID`, etc.) — object-level impact like this
is always available, even before any view/procedure lineage has been parsed.

Or just ask in plain English (requires `ANTHROPIC_API_KEY`):

```console
$ uv run oia ask "What would be affected if I changed the REGIONS table?"
```

## CLI reference

### `oia extract`

Pulls Oracle metadata into the local SQLite store (`data/oia.sqlite`) and
(re)builds the graph: object nodes/columns, FK relationships,
`ALL_DEPENDENCIES`-derived edges, view/PL-SQL column lineage, and manual
overrides.

```
oia extract [--full | --incremental] [--dictionary-scope all|dba]
```

- `--full` (default) rebuilds everything.
- `--incremental` skips re-parsing lineage for objects whose `LAST_DDL_TIME`
  hasn't changed since the last run, reusing their previously-computed edges —
  useful once you're pointed at a large schema where reparsing every
  procedure on every run gets slow.

### `oia trace <table>.<column>`

Upstream lineage: walks a column back to its base-table sources.

```
oia trace <TABLE.COLUMN | OWNER.TABLE.COLUMN> [--max-depth N] [--format text|json]
```

```console
$ uv run oia trace STAGINGCUSTOMERSEGMENT.CUSTOMERID
Tracing RETAILDEMO.STAGINGCUSTOMERSEGMENT.CUSTOMERID upstream (max-depth=10):

  RETAILDEMO.STAGINGCUSTOMERSEGMENT.CUSTOMERID ->
RETAILDEMO.CUSTOMERS.CUSTOMERID  (manual, manual_override)

Base sources: RETAILDEMO.CUSTOMERS.CUSTOMERID
```

(That particular edge came from a manual override — see
[`oia override`](#oia-override) below.)

### `oia impact <object>`

Downstream blast radius: everything that reads from, writes to, calls, or is
derived from the given table/view/procedure/column — object-level results are
always complete; column-level results appear wherever lineage parsing
succeeded for that path.

```
oia impact <OBJECT | OWNER.OBJECT | OWNER.OBJECT.COLUMN> [--max-depth N] [--reports-only] [--format text|table|json]
```

### `oia graph stats` / `oia graph export`

```console
$ uv run oia graph stats
Last run: #1 (full) started 2026-07-30 13:36:23, finished 2026-07-30 13:36:45
  objects_processed=19 objects_failed=0 parse_errors=0

Nodes: 147
  COLUMN: 128
  TABLE: 19

Edges: 14
  DERIVED_FROM: 1
  REFERENCES: 13
Confidence breakdown:
  manual: 1
  medium: 13

Unresolved lineage (dynamic SQL / gap markers): 0
```

`graph export` writes the full graph to JSON or GraphML for external tools
(Gephi, yEd, ...):

```
oia graph export [--format json|graphml] [--output PATH]
```

### `oia objects list` / `oia objects search`

Browse what got extracted:

```console
$ uv run oia objects search report
+----------------------------------------------------+
| Owner      | Name                          | Type  |
|------------+-------------------------------+-------|
| RETAILDEMO | REPORT_CUSTOMERCHURNRISK      | TABLE |
| RETAILDEMO | REPORT_EMPLOYEECOMMISSION     | TABLE |
| RETAILDEMO | REPORT_INVENTORYREPLENISHMENT | TABLE |
| RETAILDEMO | REPORT_MONTHLYSALESBYREGION   | TABLE |
| RETAILDEMO | REPORT_PRODUCTPERFORMANCE     | TABLE |
+----------------------------------------------------+
```

```
oia objects list [--type TABLE|VIEW|PROCEDURE|...] [--owner OWNER]
```

### `oia override`

The manual escape hatch for lineage static analysis can't resolve (dynamic SQL
targets, external ETL writes, ...) — see `config/lineage_overrides.yaml`.

```
oia override add --src OWNER.OBJECT[.COLUMN] --dst OWNER.OBJECT[.COLUMN] \
                  --edge-type DERIVED_FROM --note "why" --author "you"
oia override list
```

Overrides are picked up on the next `oia extract`.

### `oia ask "<question>"`

Natural-language questions, answered by a Claude agent that calls the same
graph queries as tools and grounds every claim in their (cited, confidence-
scored) results — see [PROMPT.md §5.5](PROMPT.md#55-claude-nl-agent-integration)
for the anti-hallucination design. Requires `ANTHROPIC_API_KEY` in `.env`.

```
oia ask "<question>" [--model MODEL] [--effort low|medium|high] [--json]
```

## Configuration

### `config/config.yaml`

```yaml
dictionary_scope: all        # or "dba" for cross-schema visibility (needs SELECT_CATALOG_ROLE)
schemas:
  include: []                 # empty = the connecting user's own schema only
  exclude: [SYS, SYSTEM, ...] # noise schemas to always skip
report_rules:
  name_prefixes: ["RPT_", "REPORT_"]   # tune this to your environment's naming convention
  name_suffixes: ["_REPORT"]
  allowlist: []                          # explicit OWNER.OBJECT_NAME entries
agent:
  model: claude-haiku-4-5-20251001   # cheap/fast; use claude-sonnet-5 or claude-opus-5 for harder questions
  effort: medium
graph:
  sqlite_path: data/oia.sqlite
```

There's no built-in notion of a "report" in a raw Oracle schema, so
`report_rules` is how you tell OIA which objects count as reports for impact
analysis — adjust it to match whatever naming convention (or BI tool export)
your real environment uses.

### `config/lineage_overrides.yaml`

Human-editable, version-controlled manual lineage entries — see
[`oia override`](#oia-override) above.

## Testing

```bash
uv run pytest                 # unit tests only (fixture-based, no DB needed)
uv run pytest -m integration  # + a live, strictly read-only smoke test against
                               # whatever .env points at
```

## How it works (short version)

1. **Extract** — pulls `ALL_OBJECTS`, `ALL_TAB_COLUMNS`, `ALL_VIEWS`,
   `ALL_DEPENDENCIES`, `ALL_SOURCE`, `ALL_TRIGGERS`, `ALL_CONSTRAINTS`, etc.
   into a local SQLite file — strictly `SELECT`, never DDL/DML against your
   database.
2. **Build the graph** — object/column nodes plus edges from FK constraints,
   `ALL_DEPENDENCIES`, view DDL parsed with [sqlglot](https://github.com/tobymao/sqlglot),
   and PL/SQL procedure/trigger bodies parsed with a heuristic statement
   harvester + sqlglot. Every edge carries a `confidence`
   (`high`/`medium`/`low`/`manual`/`none`) and `method` — nothing is guessed;
   dynamic SQL that can't be statically resolved becomes an explicit gap
   marker instead of a fabricated edge.
3. **Query** — `trace`/`impact` are graph traversals (via
   [NetworkX](https://networkx.org/)) over that data, degrading gracefully
   from column-level to object-level wherever parsing didn't reach.
4. **Ask** — a Claude agent calls the same traversals as tools and composes a
   cited answer, never asserting a relationship the graph itself didn't produce.

See [PROMPT.md](PROMPT.md) for the full architecture spec, confidence model,
phased delivery plan, and explicit non-goals.

## Project layout

```
config/                  config.yaml, lineage_overrides.yaml
src/oia/
  extraction/             Oracle data-dictionary extraction
  lineage/                sqlglot-based DDL + PL/SQL lineage parsing
  graph/                  node/edge model, object-graph builder, traversal
  agent/                  Claude tool definitions + agent loop
  cli/                    Typer CLI commands
  storage/                SQLite schema + store
tests/
  unit/                   fixture-based, no DB required
  integration/            opt-in, live, read-only
```
