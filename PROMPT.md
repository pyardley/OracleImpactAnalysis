# Build Prompt: OIA — Oracle Impact Analysis Tool

> This document is a build specification / prompt. Hand this entire file to a coding agent (e.g. a fresh Claude Code session pointed at this repo) to implement the tool. Nothing in this repository has been implemented yet — treat this as the starting brief for a greenfield build.

## 1. Title & Framing

Build **OIA (Oracle Impact Analysis)**, a production-grade command-line tool that performs schema-wide dependency/lineage analysis over an Oracle database and answers natural-language regression-impact questions about it. It must be genuinely useful against a real, messy Oracle schema — not a toy demo — while being honest about the limits of static analysis (see §7, Non-Goals).

All code for this project lives under `C:\Users\PaulYardley\Projects\OracleImpactAnalysis`.

## 2. Goal & Example Questions

OIA must answer natural-language questions such as:

- "Trace the source of `CUSTOMERS.EMAIL` back to its source tables." (upstream column lineage)
- "What reports will be affected by this code change to stored procedure `UPDATE_CUSTOMER_TOTALS`?" (downstream blast-radius / impact analysis)
- "What breaks if I drop column `ORDERS.DISCOUNT_PCT`?"
- "Which procedures write to the `INVENTORY` table?"
- "Is `V_CUSTOMER_SUMMARY` derived from `CUSTOMERS` directly, or through another view?"

These are answered by a Claude-powered agent that calls structured graph-query tools (§5.5) over a dependency/lineage graph built from the target database's own data dictionary and PL/SQL source — never from guesswork.

## 3. Target Environment

Primary development/test target — an Oracle 23ai Free instance:

| Parameter                   | Value                           |
| --------------------------- | ------------------------------- |
| Connection name             | RetailDemo                      |
| Username                    | `retaildemo`                    |
| Hostname                    | `DESKTOP-M4R2VLU`               |
| Port                        | `1521`                          |
| Service name                | `FREEPDB1`                      |
| DSN (for `python-oracledb`) | `DESKTOP-M4R2VLU:1521/FREEPDB1` |

**Do not hardcode the password anywhere in code, config, or documentation.** It is supplied only via the `ORACLE_PASSWORD` environment variable, loaded from a gitignored `.env` file at runtime (see §5.6). `.env.example` in the repo should list the variable names with placeholder values only.

OIA must not be hardwired to RetailDemo specifically — all connection and schema-scope parameters are configurable, so the same tool can point at any Oracle database.

## 4. Locked-In Architecture Decisions

These were decided up front and are non-negotiable constraints for the build, not open design questions:

1. **Language/stack: Python.** Use `python-oracledb` in **thin mode** — no Oracle Instant Client install is available or required.
2. **NL interface: a Claude-powered agent** (Anthropic API, tool-calling), not a local keyword/template matcher. The agent calls graph-query tools and composes a cited answer.
3. **"Report" identification is configurable**, not inferred by a fixed rule. RetailDemo has no built-in reporting layer, so a rule engine (schema-name pattern, object-name prefix/suffix convention, or explicit allowlist) defined in `config.yaml` decides which objects count as "reports."
4. **Interface v1 is CLI-only.** A web UI is explicitly out of scope for v1 but the design must not preclude adding one later (JSON/GraphML export exists specifically to keep that door open).

## 5. Detailed Architecture

### 5.1 Metadata Extraction Layer

Query these Oracle data-dictionary views (default to `ALL_*`, not `DBA_*` — see scope note below):

| Purpose                       | View(s)                                | Notes                                                                                                                                                                     |
| ----------------------------- | -------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Tables                        | `ALL_TABLES`                           | row-count estimates, partitioning flag                                                                                                                                    |
| Columns                       | `ALL_TAB_COLUMNS`                      | data type, nullability, `COLUMN_ID` ordinal                                                                                                                               |
| Views                         | `ALL_VIEWS`                            | prefer `TEXT_VC` (CLOB, no truncation) over legacy `TEXT`/`TEXT_LENGTH`; fall back to `TEXT` on pre-19c targets                                                           |
| Materialized views            | `ALL_MVIEWS`                           | `QUERY` = defining SELECT; model as a VIEW+TABLE hybrid node                                                                                                              |
| Object registry               | `ALL_OBJECTS`                          | canonical object list; `LAST_DDL_TIME` is the incremental-refresh key; skip/flag `STATUS = 'INVALID'` objects                                                             |
| Object-level dependencies     | `ALL_DEPENDENCIES`                     | `(OWNER,NAME,TYPE) → (REFERENCED_OWNER,REFERENCED_NAME,REFERENCED_TYPE)`; coarse (no column granularity) but cheap and always available even when statement parsing fails |
| PL/SQL source                 | `ALL_SOURCE`                           | one row per line; reconstruct full body per `(OWNER,NAME,TYPE)` ordered by `LINE`                                                                                         |
| Procedure/function signatures | `ALL_ARGUMENTS`, `ALL_PROCEDURES`      | parameter names/types/direction, return type                                                                                                                              |
| Constraints                   | `ALL_CONSTRAINTS`, `ALL_CONS_COLUMNS`  | PK/FK edges — secondary signal, not core lineage                                                                                                                          |
| Triggers                      | `ALL_TRIGGERS`                         | `TRIGGER_BODY` is PL/SQL — feed through the same procedural-lineage pipeline; `TABLE_NAME`/`TABLE_OWNER` gives a free `WRITES_TO` edge                                    |
| Synonyms                      | `ALL_SYNONYMS`                         | **must** be resolved to the real target object before graph-building, or lineage silently breaks at every synonym boundary                                                |
| Comments                      | `ALL_TAB_COMMENTS`, `ALL_COL_COMMENTS` | fed to the Claude agent as grounding context only, not used in traversal                                                                                                  |
| PL/Scope (optional)           | `ALL_IDENTIFIERS`, `ALL_STATEMENTS`    | see below                                                                                                                                                                 |

**`ALL_*` vs `DBA_*` scope:** expose `dictionary_scope: all | dba` in `config.yaml`. `all` (default) works with only object grants the connecting user already has and is the RetailDemo default; `dba` requires `SELECT_CATALOG_ROLE`/explicit grants and is the recommended mode for real multi-schema enterprise deployments. On `ORA-00942` against a `DBA_*` view, catch and re-raise with an actionable error message rather than a raw stack trace.

**Incremental refresh:** track `(owner, object_name, object_type, last_ddl_time_seen, last_extracted_at)` in a SQLite `extraction_state` table. `oia extract --incremental` re-parses only objects whose Oracle `LAST_DDL_TIME` is newer than what's recorded, plus newly-appeared objects, and removes nodes for objects that disappeared. `oia extract --full` rebuilds everything. Every run writes an `extraction_runs` audit row (run_id, mode, schema scope, timestamps, objects_processed, objects_failed, parse_errors_count) that backs `oia graph stats`.

**PL/Scope (optional, `--enable-plscope`):** Oracle's PL/Scope (`ALL_IDENTIFIERS`/`ALL_STATEMENTS`) records compiler-verified identifier usage when a unit is compiled with `PLSCOPE_SETTINGS='IDENTIFIERS:ALL'`. Useful as a cross-validation signal (it comes from Oracle's own parser) to boost confidence on statement-level parses, but it requires **recompiling** the target objects (intrusive, extra privilege) and only reports _that_ an identifier was referenced, not the transform expression connecting inputs to outputs. Not required for v1; document it as an opt-in enhancement.

### 5.2 Column-Level Lineage Extraction (hardest part — read carefully)

Oracle's `ALL_DEPENDENCIES` is object-level only. Real column-level lineage requires parsing SQL and PL/SQL source.

- **Primary parser: `sqlglot`** (its `sqlglot.lineage` module builds a column-level lineage DAG for a single `SELECT`, correctly handling CTEs, subqueries, joins, and set operations; supports an `oracle` dialect). Use it for both view/mview DDL and for individual statements harvested out of PL/SQL bodies. Wrap every parse in try/except — Oracle-specific syntax edge cases will fail to parse sometimes, and a failure must degrade to an "unparsed" gap marker, never a crash.
- **`sqllineage`** and **`sql-metadata`** were evaluated and are **not** the primary engine — `sqllineage` is built for standalone SQL/ETL scripts, not procedural PL/SQL (our hardest case); `sql-metadata` is a lighter reference extractor, usable only as a last-resort fallback that degrades to an object-level "referenced somewhere" edge when `sqlglot` fails outright.
- **PL/SQL statement harvesting (procedural bodies contain embedded SQL, not standalone SQL):**
  - **v1 (build this first):** a hand-written statement-boundary extractor over reconstructed `ALL_SOURCE` text that tracks nested `BEGIN...END`/`CASE...END`/quoted-string/quoted-identifier state (so it doesn't split inside strings or nested blocks) and yields candidate `SELECT|INSERT|UPDATE|DELETE|MERGE` statements plus `EXECUTE IMMEDIATE <expr>` occurrences, each handed to `sqlglot.parse_one(sql, dialect="oracle")`. This is explicitly heuristic — it will mis-split on unusual formatting or PL/SQL-only syntax (`%ROWTYPE`, `BULK COLLECT INTO`, `FORALL`, nested anonymous blocks). Every failure becomes a flagged `unparsed_statement` record, never a silent omission.
  - **v2 (stretch, phase 4):** a real parser generated from the actively-maintained **`antlr/grammars-v4`** PL/SQL grammar via the `antlr4`/`antlr4-python3-runtime` toolchain, replacing the regex harvester with a proper parse tree. Do **not** rely on the older prebuilt `antlr-plsql` PyPI package — it's stale; generate fresh from grammars-v4.
- **Dynamic SQL is not statically resolvable and must not be guessed.** Any `EXECUTE IMMEDIATE`/`DBMS_SQL` call whose argument isn't a single string literal is recorded as an explicit `unresolved_dynamic_sql` marker (confidence `none`), storing the raw expression and line number, surfaced via `oia graph stats` and the `get_unresolved_lineage` agent tool.

**Confidence/provenance model** — every lineage edge carries:

```
confidence: high | medium | low | manual | none
  high   = direct static parse of plain SQL/view DDL
  medium = resolved via heuristics (e.g. SELECT * expansion against known schema)
  low    = heuristic PL/SQL statement extraction, ambiguous join
  manual = human-supplied override
  none   = dynamic SQL / unresolved — a gap marker, not a real derivation claim
method: ddl_parse | plsql_static_analysis | plscope_xref | manual_override | unresolved_dynamic_sql
source_object, source_line_range, transform_expression   # actual SQL text producing the derivation
```

**Manual override escape hatch:** `config/lineage_overrides.yaml` — human-editable, version-controlled — declares edges parsing can't produce (e.g. the real target of a dynamic `EXECUTE IMMEDIATE`, or lineage into a table populated by an external ETL job invisible to Oracle). Each entry requires `note` + `author`; loaded at build time with `confidence: manual`. Manage via `oia override add|list`.

**Design stance: no silent guessing, anywhere.** Where parsing can't establish real confidence, the graph gets an honest gap marker instead of a fabricated edge. This property is what makes the agent's citations (§5.5) trustworthy.

### 5.3 Graph Storage & Model

**SQLite + NetworkX — no graph-database server for v1.**

- **SQLite** is the durable store: raw extracted metadata, compiled `graph_nodes`/`graph_edges` tables, `extraction_state`, `extraction_runs`, loaded `lineage_overrides`. Zero install friction, inspectable, safe for single-writer/multi-reader CLI use, trivially backed up as one file.
- **NetworkX** (`MultiDiGraph` — multiple typed/differently-confidenced edges can exist between the same two nodes) is the in-memory traversal engine, hydrated from SQLite at the start of a query/agent session. Comfortably handles tens of thousands of nodes / low hundreds of thousands of edges at CLI/agent latency.
- **Export:** `networkx.node_link_data()` → JSON, `networkx.write_graphml()` → GraphML (Gephi/yEd or downstream tooling).
- **Future Neo4j swap path:** because SQLite's `graph_nodes`/`graph_edges` tables already _are_ the canonical typed graph model, a later swap is mechanical — a batch exporter issuing `MERGE`/`CREATE` Cypher or generating `neo4j-admin import` CSVs. No redesign required.

Node schema:

```
node_id: "RETAILDEMO.CUSTOMERS.EMAIL"     # stable key: owner.object[.column]
node_type: "COLUMN"                        # TABLE | COLUMN | VIEW | MVIEW | PROCEDURE | FUNCTION | PACKAGE | TRIGGER | REPORT
owner: "RETAILDEMO"
object_name: "CUSTOMERS"
column_name: "EMAIL"                       # null for non-column nodes
data_type: "VARCHAR2(255)"
is_report: false                           # set by the report-rule engine at build time
last_ddl_time: "2026-06-01T10:22:00"
metadata: { "comments": "...", "nullable": true }
```

Edge schema:

```
edge_id: "e_8f21..."
edge_type: "DERIVED_FROM"        # READS_FROM | WRITES_TO | DERIVED_FROM | CALLS | REFERENCES
src_node_id: "RETAILDEMO.V_CUSTOMER_SUMMARY.FULL_NAME"
dst_node_id: "RETAILDEMO.CUSTOMERS.FIRST_NAME"
confidence: "high"
method: "ddl_parse"
source_object: "RETAILDEMO.V_CUSTOMER_SUMMARY"
source_line_range: [1, 1]
transform_expression: "UPPER(c.first_name) || ' ' || c.last_name"
extracted_at: "2026-07-29T08:00:00Z"
```

`CALLS` edges (procedure/function/package invocation) are object-level only in v1. `WRITES_TO` comes from DML targets in procedural/trigger bodies. `REFERENCES` is the catch-all fallback from `ALL_DEPENDENCIES` alone, always available even when statement parsing fails entirely.

### 5.4 Impact Analysis & Trace-to-Source Algorithms

- **Upstream trace** ("trace to source"): BFS/DFS backward along `DERIVED_FROM`/`READS_FROM` edges from a `COLUMN` node, terminating at nodes with no further incoming derivation edges (base table columns) or a configurable `--max-depth`. Track a visited-set per traversal to detect cycles (self-referencing MViews, recursive procedures) and report them explicitly rather than looping or silently truncating.
- **Downstream impact** ("blast radius"): forward traversal from a changed table/column/procedure, resolving multi-hop views-of-views and procedure-calls-procedure chains. Always produce **both** granularities: object-level (always available — only needs `ALL_DEPENDENCIES`) and column-level (available wherever DDL/PL-SQL parsing succeeded), so results degrade gracefully rather than going empty when parsing has gaps.
- **Report identification:** after computing the raw downstream node set, tag nodes using the `config.yaml` rule engine (schema-name regex / object-name prefix-suffix / explicit allowlist). Compute `is_report` once at graph-build time (stored as a node property), not per query, so `--reports-only` and `list_reports_affected` are cheap filters.
- **Confidence-aware traversal:** any path crossing a `low`/`none`-confidence edge (including `unresolved_dynamic_sql`) must flag that branch of the result as incomplete (e.g. "impact analysis may be incomplete beyond X — unresolved dynamic SQL in PROC_Y at line 42"), propagated into both CLI text output and the structured results the agent consumes.
- **Path/citation output:** every trace/impact result returns the actual edge paths (ordered edge lists, with `transform_expression`/`source_object`/`confidence`) per affected node — this is the raw material for both `oia trace/impact --format text` and the agent's citations.

### 5.5 Claude NL Agent Integration

- **Model:** default `claude-sonnet-5`, with `agent.effort` configurable in `config.yaml` (default `medium`, `--effort high` available on `oia ask` for hard multi-hop questions). Keep `claude-opus-5` as a documented one-line config swap for teams wanting the higher intelligence ceiling — never hardcode the model. (Re-check current model catalog/pricing if this build happens much later than mid-2026.)
- **Agent loop:** use the Python SDK's Tool Runner (`client.beta.messages.tool_runner(...)`) to avoid hand-rolled loop boilerplate; stream via `client.messages.stream()` so `oia ask` can show live per-tool-call progress. Citation-grounding is enforced by what tools return (structured JSON with explicit paths/confidence, never prose) plus a strict system prompt — not by intercepting the loop.
- **Tool definitions:**

```
search_objects(name_pattern: str, object_types: list[str] | None = None) -> list[ObjectSummary]
    # resolves fuzzy NL references ("the customer table") to real owner.object_name entries

get_object_metadata(object_name: str, object_type: str | None = None) -> ObjectMetadata
    # columns, data types, comments, source reference — grounding, not lineage

trace_column_lineage(table: str, column: str, max_depth: int = 10) -> LineageTraceResult
    # upstream trace; returns source columns + full edge paths + confidence flags

impact_of_change(object_name: str, object_type: str | None = None,
                  max_depth: int = 10, reports_only: bool = False) -> ImpactResult
    # downstream blast radius; object-level always populated, column-level where available

list_reports_affected(object_name: str) -> list[ReportSummary]
    # convenience filter over impact_of_change for is_report=true nodes

get_unresolved_lineage(object_name: str) -> list[UnresolvedEdge]
    # surfaces dynamic-SQL / low-confidence gaps so Claude can caveat its answer

export_graph(format: str, scope: str | None = None) -> str
    # produces a downloadable subgraph artifact path
```

Every result-bearing tool returns structured JSON with raw edge paths (`src`, `dst`, `edge_type`, `confidence`, `source_object`, `source_line_range`, `transform_expression`) — never prose summaries. Seven tools is small enough to load all up front; don't add deferred tool-loading complexity unless the tool surface grows materially later.

- **Grounding / anti-hallucination:** the system prompt must instruct Claude to only assert a relationship that appears in a tool result; always cite `owner.object.column` and its confidence; explicitly caveat any conclusion crossing a `low`/`none`-confidence or `unresolved_dynamic_sql` edge; and never infer lineage from column-name similarity or general domain knowledge. Because the graph itself never fabricates edges (§5.2) and tools return raw provenance, the agent's job is composition over ground-truth data, not independent SQL reasoning.
- **Prompt caching:** cache the system prompt plus a compact schema-catalog summary (one line per object: name + purpose, not full DDL) as an ephemeral `cache_control` system block. Do not cache per-turn tool results. Verify hits via `usage.cache_read_input_tokens` during development.

### 5.6 Config & Secrets Handling

- **`.env`** (gitignored; `.env.example` checked in with placeholders only) — `ORACLE_USER`, `ORACLE_PASSWORD`, `ORACLE_DSN`, `ANTHROPIC_API_KEY`. Loaded via `python-dotenv` at CLI startup. Fail fast with an actionable message if a required var is missing; never log values.
- **`config.yaml`** (checked in, no secrets) — `dictionary_scope: all|dba`, `schemas: {include: [...], exclude: [...]}`, `report_rules: {schema_patterns: [...], name_prefixes: [...], name_suffixes: [...], allowlist: [...]}`, `agent: {model, effort, max_tokens}`, `graph: {sqlite_path}`, `logging: {level}`.
- `oracledb.connect()` (thin mode) reads exclusively from loaded env vars; credentials never appear in logs, error messages, or the SQLite store.

### 5.7 CLI Command Surface

```
oia extract [--full | --incremental] [--schema NAME ...] [--dictionary-scope all|dba]
oia ask "<question>" [--model ...] [--effort low|medium|high] [--json]
oia trace <table>.<column> [--max-depth N] [--format text|table|json|graph]
oia impact <object_name> [--type table|view|procedure|...] [--max-depth N] [--reports-only] [--format text|table|json]
oia graph export [--format json|graphml] [--scope object_name] [--output path]
oia graph stats                       # node/edge counts, confidence breakdown, unresolved-edge count
oia objects list|search <pattern>
oia override add|list                 # manages config/lineage_overrides.yaml
```

Output formats: `text` (human-readable narrative), `table` (columnar via `rich`), `json` (machine-readable, scripting/tests), `graph` (dot/graphml, direct visualization).

### 5.8 Project Structure

**Dependency management: `uv` + `pyproject.toml`** (fast, single-binary, handles venv + lockfile + script running with minimal ceremony — lower friction than Poetry for a project a coding agent bootstraps from a spec document).

```
OracleImpactAnalysis/
├── pyproject.toml
├── .env.example
├── config/
│   ├── config.yaml
│   └── lineage_overrides.yaml
├── src/
│   └── oia/
│       ├── __init__.py
│       ├── cli/                     # Typer/Click command definitions
│       ├── config/                  # env + config.yaml loading, validation
│       ├── extraction/
│       │   ├── oracle_metadata.py   # ALL_*/DBA_* extraction via python-oracledb thin mode
│       │   └── incremental.py       # LAST_DDL_TIME-based diffing
│       ├── lineage/
│       │   ├── ddl_lineage.py       # sqlglot-based view/mview DDL lineage
│       │   ├── plsql_statements.py  # PL/SQL statement harvester (v1 regex, v2 ANTLR)
│       │   ├── plsql_lineage.py     # harvested statements -> sqlglot -> edges
│       │   └── overrides.py         # manual override loader/merger
│       ├── graph/
│       │   ├── model.py             # node/edge schema, SQLite <-> NetworkX hydration
│       │   ├── traversal.py         # upstream trace / downstream impact algorithms
│       │   └── report_rules.py      # configurable report-identification rule engine
│       ├── agent/
│       │   ├── tools.py             # Claude tool definitions + implementations
│       │   ├── loop.py              # Tool Runner setup, system prompt, streaming
│       │   └── grounding.py         # citation formatting from raw edge paths
│       └── storage/
│           └── sqlite_store.py      # schema DDL, migrations, extraction_runs/state tables
└── tests/
    ├── fixtures/
    │   ├── ddl/*.sql                # sample view/table DDL
    │   └── plsql/*.sql              # sample procedures/functions/triggers + known-good expected lineage JSON
    ├── unit/                        # parser tests against fixtures — no DB required
    └── integration/                 # @pytest.mark.integration — real RetailDemo, strictly read-only
```

**Testing strategy:** `pytest`. Unit tests for `ddl_lineage`/`plsql_lineage` run against fixture SQL/PLSQL files with hand-verified golden-expected-lineage JSON — no database dependency, fast, and the primary regression net for the highest-risk parsing code. Integration tests are marked and skipped by default, require `ORACLE_DSN` etc. pointed at a real instance, perform **SELECT only** against data-dictionary views (never DDL/DML against the target database), and are safe to run against RetailDemo using the given read-only-by-convention `retaildemo` user.

## 6. Phased Delivery Plan

| Phase | Deliverable                                                                                                          | Done when                                                                                                                                         |
| ----- | -------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| 0     | Scaffolding: `pyproject.toml`, config/env loading, connectivity smoke test                                           | `uv run oia --help` works and a smoke-test connects to RetailDemo in thin mode                                                                    |
| 1     | Metadata extraction (`ALL_*` → SQLite) + object-level graph from `ALL_DEPENDENCIES`                                  | `oia extract --full` completes against RetailDemo with an `extraction_runs` row and zero unhandled exceptions                                     |
| 2     | CLI skeleton: `oia impact`/`oia trace` at object-level only, `oia graph export`/`stats`                              | `oia trace CUSTOMERS.EMAIL` and `oia impact <a_real_procedure>` return object-level results                                                       |
| 3     | Column-level view lineage via `sqlglot`                                                                              | `oia trace`/`oia impact` upgrade to column granularity for view-derived paths, verified against a handful of RetailDemo views                     |
| 4     | PL/SQL procedural lineage (v1 harvester + confidence model + `unresolved_dynamic_sql` + overrides; ANTLR v2 stretch) | A real RetailDemo procedure's writes show up as `WRITES_TO` edges with correct confidence; any dynamic SQL in it is flagged, not silently dropped |
| 5     | Claude NL agent: tools, Tool Runner loop, prompt caching, grounding                                                  | `oia ask` correctly answers both motivating example questions from §2 with cited, confidence-flagged paths                                        |
| 6     | Report-rule polish, synonym resolution, cycle-handling, views-of-views correctness                                   | Synonyms resolve correctly; a deliberately cyclic/multi-hop fixture traces correctly without infinite loop                                        |
| 7     | Full unit + integration test suite, docs, packaging (pip-installable/pipx)                                           | `pytest` unit suite green; README covers setup/usage; `pipx install .` works                                                                      |

## 7. Explicit Non-Goals / Limitations

State these honestly in the tool's own docs/output, don't paper over them:

1. **No perfect lineage through highly dynamic SQL.** String-concatenated `EXECUTE IMMEDIATE`/`DBMS_SQL` targets are surfaced as unresolved gaps, never guessed.
2. **No visibility into ETL/data movement outside the database** (external Informatica/SSIS/application writes not expressed as PL/SQL visible to Oracle). Must be captured via manual overrides.
3. **No PL/SQL symbolic execution.** Control flow (loops, conditionals, variable reassignment across statements) is not simulated — each embedded SQL statement is analyzed independently.
4. **Batch/point-in-time snapshot, not real-time.** Lineage reflects the last `oia extract` run.
5. **No web UI in v1** — deliberately deferred; JSON/GraphML export keeps the door open.
6. **Oracle-23ai-syntax assumptions** (e.g. `ALL_VIEWS.TEXT_VC`); older Oracle versions may need version-gating.
7. **Multi-schema coverage depends on grants.** The tool can't reason about objects it has no dictionary visibility into.
8. **Confidence is heuristic, not proof.** Even "high confidence" static lineage can be wrong if synonyms or dynamically-resolved object names obscure the real target.

## 8. Definition of Done (for the eventual build)

- [ ] `oia extract` runs cleanly against RetailDemo end-to-end with no unhandled exceptions.
- [ ] `oia trace`/`oia impact` produce correct results at minimum at object-level, and column-level wherever parsing succeeded.
- [ ] `oia ask` correctly and citably answers both motivating example questions from §2.
- [ ] Unit test suite (fixture-based, no DB) is green.
- [ ] Secrets (`ORACLE_PASSWORD`, `ANTHROPIC_API_KEY`) are never logged, never written to SQLite, never committed — verified by grepping the repo and logs.
- [ ] `oia graph stats` accurately reports unresolved/low-confidence edge counts so users know what the tool couldn't determine.
