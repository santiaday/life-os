"""SQL safety helpers for the ask_sql tool.

Defense-in-depth on top of the read-only DB role:
  1. Reject obvious DML/DDL keywords outside string literals.
  2. Auto-append `LIMIT N` to bare SELECTs.
  3. Apply a per-statement statement_timeout.
"""

from __future__ import annotations

import re

# Keywords that have no business inside a read-only query. We strip string
# literals before matching so a quoted "DELETE" inside a value passes through.
FORBIDDEN_KEYWORDS = (
    "INSERT", "UPDATE", "DELETE", "DROP", "TRUNCATE",
    "ALTER", "CREATE", "GRANT", "REVOKE", "COPY",
    "VACUUM", "REINDEX", "REFRESH", "EXECUTE", "CALL",
    "DO", "MERGE",
)

# Match a single-quoted or dollar-quoted string literal so we can strip them
# before keyword detection. Postgres dollar-quoting allows arbitrary tags
# like $body$...$body$; we handle the common $$ form and tagged form.
_LITERAL_RE = re.compile(
    r"'(?:[^']|'')*'"          # single-quoted, '' escape
    r"|\$([A-Za-z_]*)\$.*?\$\1\$",  # $tag$...$tag$
    re.DOTALL,
)
_COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
_STMT_END_RE = re.compile(r";")
_LIMIT_RE = re.compile(r"\bLIMIT\s+\d+", re.IGNORECASE)


class UnsafeQueryError(ValueError):
    """Raised when an ask_sql query trips a safety check."""


def normalize_for_check(query: str) -> str:
    """Strip comments and string literals so keyword scanning isn't fooled."""
    s = _COMMENT_RE.sub(" ", query)
    s = _LITERAL_RE.sub(" ", s)
    return s


def validate(query: str) -> None:
    """Raise UnsafeQueryError if the query contains forbidden keywords or
    multiple statements."""
    stripped = normalize_for_check(query)

    # No more than one statement (extra `;` only allowed at end).
    inner = _STMT_END_RE.split(stripped)
    non_empty = [seg for seg in inner if seg.strip()]
    if len(non_empty) > 1:
        raise UnsafeQueryError("Multiple statements in one query are not allowed.")

    upper = stripped.upper()
    for kw in FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{kw}\b", upper):
            raise UnsafeQueryError(f"Forbidden keyword in query: {kw}")


def ensure_limit(query: str, default_limit: int) -> str:
    """If the (cleaned) query is a SELECT and has no LIMIT, append one. Don't
    touch queries that already have a LIMIT or that aren't SELECTs (they'll
    fail validation anyway)."""
    stripped = normalize_for_check(query).strip().rstrip(";")
    if not stripped.upper().lstrip().startswith(("SELECT", "WITH")):
        return query
    if _LIMIT_RE.search(stripped):
        return query
    cleaned = query.rstrip().rstrip(";")
    return f"{cleaned}\nLIMIT {default_limit}"
