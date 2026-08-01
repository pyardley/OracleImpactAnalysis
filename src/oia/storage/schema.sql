-- OIA local SQLite store. This is OIA's own working data (raw extracted
-- metadata + the compiled graph) - never written back to the Oracle
-- database being analyzed, which OIA only ever SELECTs from.

CREATE TABLE IF NOT EXISTS raw_objects (
  owner TEXT NOT NULL,
  object_name TEXT NOT NULL,
  object_type TEXT NOT NULL,
  status TEXT,
  last_ddl_time TEXT,
  PRIMARY KEY (owner, object_name, object_type)
);

CREATE TABLE IF NOT EXISTS raw_columns (
  owner TEXT NOT NULL,
  object_name TEXT NOT NULL,
  column_name TEXT NOT NULL,
  data_type TEXT,
  nullable TEXT,
  column_id INTEGER,
  PRIMARY KEY (owner, object_name, column_name)
);

CREATE TABLE IF NOT EXISTS raw_source (
  owner TEXT NOT NULL,
  object_name TEXT NOT NULL,
  object_type TEXT NOT NULL,
  body TEXT,
  PRIMARY KEY (owner, object_name, object_type)
);

CREATE TABLE IF NOT EXISTS raw_view_text (
  owner TEXT NOT NULL,
  view_name TEXT NOT NULL,
  text TEXT,
  is_mview INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (owner, view_name)
);

CREATE TABLE IF NOT EXISTS raw_dependencies (
  owner TEXT NOT NULL,
  name TEXT NOT NULL,
  type TEXT NOT NULL,
  referenced_owner TEXT,
  referenced_name TEXT NOT NULL,
  referenced_type TEXT
);
CREATE INDEX IF NOT EXISTS idx_raw_deps_src ON raw_dependencies(owner, name, type);

CREATE TABLE IF NOT EXISTS raw_arguments (
  owner TEXT NOT NULL,
  object_name TEXT NOT NULL,
  package_name TEXT,
  argument_name TEXT,
  position INTEGER,
  in_out TEXT,
  data_type TEXT
);

CREATE TABLE IF NOT EXISTS raw_triggers (
  owner TEXT NOT NULL,
  trigger_name TEXT NOT NULL,
  table_owner TEXT,
  table_name TEXT,
  triggering_event TEXT,
  trigger_type TEXT,
  body TEXT,
  PRIMARY KEY (owner, trigger_name)
);

CREATE TABLE IF NOT EXISTS raw_synonyms (
  owner TEXT NOT NULL,
  synonym_name TEXT NOT NULL,
  table_owner TEXT,
  table_name TEXT,
  db_link TEXT,
  PRIMARY KEY (owner, synonym_name)
);

CREATE TABLE IF NOT EXISTS raw_foreign_keys (
  constraint_name TEXT NOT NULL,
  owner TEXT NOT NULL,
  table_name TEXT NOT NULL,
  column_name TEXT NOT NULL,
  r_owner TEXT NOT NULL,
  r_table_name TEXT NOT NULL,
  r_column_name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_comments (
  owner TEXT NOT NULL,
  object_name TEXT NOT NULL,
  column_name TEXT,
  comments TEXT
);

CREATE TABLE IF NOT EXISTS graph_nodes (
  node_id TEXT PRIMARY KEY,
  node_type TEXT NOT NULL,
  owner TEXT NOT NULL,
  object_name TEXT NOT NULL,
  column_name TEXT,
  data_type TEXT,
  is_report INTEGER NOT NULL DEFAULT 0,
  last_ddl_time TEXT,
  metadata TEXT
);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_owner_object ON graph_nodes(owner, object_name);

CREATE TABLE IF NOT EXISTS graph_edges (
  edge_id TEXT PRIMARY KEY,
  edge_type TEXT NOT NULL,
  src_node_id TEXT NOT NULL,
  dst_node_id TEXT NOT NULL,
  confidence TEXT NOT NULL,
  method TEXT NOT NULL,
  source_object TEXT,
  source_line_range TEXT,
  transform_expression TEXT,
  filter_expression TEXT,
  extracted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_graph_edges_src ON graph_edges(src_node_id);
CREATE INDEX IF NOT EXISTS idx_graph_edges_dst ON graph_edges(dst_node_id);

CREATE TABLE IF NOT EXISTS unresolved_lineage (
  owner TEXT NOT NULL,
  object_name TEXT NOT NULL,
  object_type TEXT NOT NULL,
  line INTEGER,
  raw_text TEXT NOT NULL,
  detected_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS extraction_state (
  owner TEXT NOT NULL,
  object_name TEXT NOT NULL,
  object_type TEXT NOT NULL,
  last_ddl_time_seen TEXT,
  last_extracted_at TEXT NOT NULL,
  PRIMARY KEY (owner, object_name, object_type)
);

CREATE TABLE IF NOT EXISTS extraction_runs (
  run_id INTEGER PRIMARY KEY AUTOINCREMENT,
  mode TEXT NOT NULL,
  schema_scope TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  objects_processed INTEGER NOT NULL DEFAULT 0,
  objects_failed INTEGER NOT NULL DEFAULT 0,
  parse_errors_count INTEGER NOT NULL DEFAULT 0
);
