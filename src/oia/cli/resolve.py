"""Resolves user-typed references ("CUSTOMERS.EMAIL", "RETAILDEMO.CUSTOMERS") to
real graph node ids, filling in the owner when the user omits it.
"""

from __future__ import annotations

import networkx as nx


class ResolutionError(ValueError):
    pass


def resolve_column(g: nx.MultiDiGraph, ref: str) -> str:
    parts = ref.split(".")
    if len(parts) == 3:
        node_id = ".".join(p.upper() for p in parts)
        if node_id not in g:
            raise ResolutionError(f"No such column: {ref}")
        return node_id
    if len(parts) != 2:
        raise ResolutionError(f"Expected TABLE.COLUMN or OWNER.TABLE.COLUMN, got {ref!r}")
    table, column = (p.upper() for p in parts)
    matches = [
        n
        for n, d in g.nodes(data=True)
        if d.get("node_type") == "COLUMN" and d.get("object_name") == table and d.get("column_name") == column
    ]
    if not matches:
        raise ResolutionError(f"No such column: {ref}")
    if len(matches) > 1:
        owners = ", ".join(sorted(m.split(".")[0] for m in matches))
        raise ResolutionError(f"{ref} is ambiguous across owners [{owners}] - use OWNER.{ref}")
    return matches[0]


def resolve_object(g: nx.MultiDiGraph, ref: str) -> str:
    parts = ref.split(".")
    if len(parts) == 2:
        node_id = ".".join(p.upper() for p in parts)
        if node_id not in g:
            raise ResolutionError(f"No such object: {ref}")
        return node_id
    if len(parts) != 1:
        raise ResolutionError(f"Expected OBJECT_NAME or OWNER.OBJECT_NAME, got {ref!r}")
    name = parts[0].upper()
    matches = [n for n, d in g.nodes(data=True) if d.get("node_type") != "COLUMN" and d.get("object_name") == name]
    if not matches:
        raise ResolutionError(f"No such object: {ref}")
    if len(matches) > 1:
        owners = ", ".join(sorted(m.split(".")[0] for m in matches))
        raise ResolutionError(f"{ref} is ambiguous across owners [{owners}] - use OWNER.{ref}")
    return matches[0]
