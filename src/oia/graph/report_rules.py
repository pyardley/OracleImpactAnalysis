"""Configurable report-identification rule engine (PROMPT.md 5.1/5.4).

RetailDemo has no built-in reporting layer, so "is this a report" is decided
by config.yaml rules rather than a fixed heuristic - see config/config.yaml
for the rules actually in effect (schema pattern / name prefix-suffix /
explicit allowlist, OR'd together).
"""

from __future__ import annotations

import re

from oia.config.settings import ReportRules


def is_report(owner: str, object_name: str, rules: ReportRules) -> bool:
    allowlist = {a.upper() for a in rules.allowlist}
    if f"{owner}.{object_name}".upper() in allowlist:
        return True

    name_u = object_name.upper()
    if any(name_u.startswith(p.upper()) for p in rules.name_prefixes):
        return True
    if any(name_u.endswith(s.upper()) for s in rules.name_suffixes):
        return True
    return any(re.search(pattern, owner, re.IGNORECASE) for pattern in rules.schema_patterns)
