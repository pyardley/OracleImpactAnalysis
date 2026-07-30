"""Manual lineage override loader/writer - config/lineage_overrides.yaml
(PROMPT.md 5.2 escape hatch for anything static analysis can't resolve).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import yaml

from oia.config.settings import Settings
from oia.graph.model import Edge, column_node_id, object_node_id

logger = logging.getLogger("oia.lineage.overrides")


def _overrides_path(settings: Settings):
    return settings.project_root / "config" / "lineage_overrides.yaml"


def _node_id_for(ref: str) -> str:
    parts = ref.split(".")
    if len(parts) == 3:
        return column_node_id(*parts)
    if len(parts) == 2:
        return object_node_id(*parts)
    raise ValueError(f"Invalid override reference {ref!r}; expected OWNER.OBJECT or OWNER.OBJECT.COLUMN")


def read_overrides(settings: Settings) -> list[dict]:
    path = _overrides_path(settings)
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return raw.get("overrides", []) or []


def add_override(settings: Settings, src: str, dst: str, edge_type: str, note: str, author: str) -> None:
    _node_id_for(src)  # validates format early, before writing
    _node_id_for(dst)
    entries = read_overrides(settings)
    entries.append(
        {
            "src": src,
            "dst": dst,
            "edge_type": edge_type,
            "note": note,
            "author": author,
            "added_at": datetime.now(UTC).isoformat(),
        }
    )
    path = _overrides_path(settings)
    path.write_text(yaml.safe_dump({"overrides": entries}, sort_keys=False), encoding="utf-8")


def load_override_edges(settings: Settings, node_ids: set[str]) -> list[Edge]:
    edges: list[Edge] = []
    for entry in read_overrides(settings):
        try:
            src_id = _node_id_for(entry["src"])
            dst_id = _node_id_for(entry["dst"])
        except (KeyError, ValueError) as exc:
            logger.warning("Skipping malformed lineage override %r: %s", entry, exc)
            continue
        if src_id not in node_ids or dst_id not in node_ids:
            logger.warning(
                "Skipping lineage override %s -> %s: node(s) not found in the current graph", src_id, dst_id
            )
            continue
        if not entry.get("note") or not entry.get("author"):
            logger.warning("Skipping lineage override %s -> %s: missing required note/author", src_id, dst_id)
            continue
        edges.append(
            Edge(
                edge_type=entry.get("edge_type", "DERIVED_FROM"),
                src_node_id=src_id,
                dst_node_id=dst_id,
                confidence="manual",
                method="manual_override",
                source_object=None,
                transform_expression=f"{entry['note']} (added by {entry['author']})",
            )
        )
    return edges
