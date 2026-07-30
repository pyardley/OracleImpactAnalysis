"""Heuristic statement harvester for PL/SQL bodies (PROMPT.md 5.2, v1 approach).

Not a real PL/SQL parser - a hand-written scanner that masks out string/quoted-
identifier/comment content (so they can't produce false keyword matches or
premature statement splits), then locates SELECT/INSERT/UPDATE/DELETE/MERGE/
EXECUTE IMMEDIATE keyword occurrences and captures each statement up to its own
top-level semicolon. A SQL statement can't itself contain an unparenthesized
semicolon, so this never needs to track BEGIN/END block nesting - nested blocks,
nested subqueries, and MERGE's WHEN-clauses all fall out correctly for free.

This will still mis-split on sufficiently unusual formatting or PL/SQL-only
syntax (%ROWTYPE, FORALL, nested anonymous blocks). Every extraction failure
is meant to surface as a parse-error count downstream (oia.lineage.plsql_lineage),
never a silent omission - see PROMPT.md's "no silent guessing" stance.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_KEYWORD_RE = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|MERGE)\b", re.IGNORECASE)
_EXEC_IMMEDIATE_RE = re.compile(r"\bEXECUTE\s+IMMEDIATE\b", re.IGNORECASE)
_INTO_CLAUSE_RE = re.compile(r"\b(BULK\s+COLLECT\s+INTO|INTO)\s+.*?(?=\bFROM\b)", re.IGNORECASE | re.DOTALL)
_RETURNING_RE = re.compile(r"\bRETURNING\b.*$", re.IGNORECASE | re.DOTALL)
_LEADING_LITERAL_RE = re.compile(r"^\s*'((?:[^']|'')*)'\s*", re.DOTALL)
_SAFE_REMAINDER_RE = re.compile(r"^\s*(USING|INTO|;|$)", re.IGNORECASE)


@dataclass
class Statement:
    kind: str  # "sql" | "dynamic_sql"
    raw_text: str
    sql_for_parsing: str | None  # None for unresolved dynamic SQL
    line: int


def _mask(source: str) -> str:
    """Same-length copy of `source` with string/quoted-identifier/comment content
    blanked out (newlines preserved) so keyword/paren scanning ignores their content."""
    out = list(source)
    i, n = 0, len(source)
    while i < n:
        ch = source[i]
        if ch == "'":
            j = i + 1
            while j < n:
                if source[j] == "'" and j + 1 < n and source[j + 1] == "'":
                    j += 2
                    continue
                if source[j] == "'":
                    j += 1
                    break
                j += 1
            for k in range(i, min(j, n)):
                out[k] = "\n" if source[k] == "\n" else "x"
            i = j
        elif ch == '"':
            j = i + 1
            while j < n and source[j] != '"':
                j += 1
            j = min(j + 1, n)
            for k in range(i, j):
                out[k] = "\n" if source[k] == "\n" else "x"
            i = j
        elif source.startswith("--", i):
            j = source.find("\n", i)
            j = n if j == -1 else j
            for k in range(i, j):
                out[k] = " "
            i = j
        elif source.startswith("/*", i):
            j = source.find("*/", i)
            j = n if j == -1 else j + 2
            for k in range(i, min(j, n)):
                out[k] = "\n" if source[k] == "\n" else " "
            i = j
        else:
            i += 1
    return "".join(out)


def _find_statement_end(masked: str, start: int) -> int:
    depth = 0
    n = len(masked)
    i = start
    while i < n:
        ch = masked[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == ";" and depth <= 0:
            return i
        i += 1
    return n


def _line_of(source: str, pos: int) -> int:
    return source.count("\n", 0, pos) + 1


def _clean_select(sql: str) -> str:
    return _INTO_CLAUSE_RE.sub(" ", sql)


def _clean_dml(sql: str) -> str:
    return _RETURNING_RE.sub("", sql)


def harvest_statements(source: str | None) -> list[Statement]:
    if not source:
        return []
    masked = _mask(source)
    statements: list[Statement] = []
    consumed_until = 0

    matches = sorted(
        [*_KEYWORD_RE.finditer(masked), *_EXEC_IMMEDIATE_RE.finditer(masked)],
        key=lambda m: m.start(),
    )

    for m in matches:
        if m.start() < consumed_until:
            continue  # nested inside a statement we already captured
        start = m.start()
        end = _find_statement_end(masked, start)
        raw_text = source[start:end].strip()
        line = _line_of(source, start)

        if m.re is _EXEC_IMMEDIATE_RE:
            expr_text = source[m.end() : end]
            literal_match = _LEADING_LITERAL_RE.match(expr_text)
            if literal_match:
                remainder = expr_text[literal_match.end() :]
                if not _SAFE_REMAINDER_RE.match(remainder):
                    literal_match = None  # concatenation follows - not statically resolvable
            if literal_match:
                literal_sql = literal_match.group(1).replace("''", "'")
                statements.append(Statement("sql", raw_text, literal_sql, line))
            else:
                statements.append(Statement("dynamic_sql", raw_text, None, line))
        else:
            keyword = m.group(1).upper()
            cleaned = _clean_select(raw_text) if keyword == "SELECT" else _clean_dml(raw_text)
            statements.append(Statement("sql", raw_text, cleaned, line))

        consumed_until = end

    return statements
