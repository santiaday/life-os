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


# ---------------------------------------------------------------------------
# Write-path safety. The generic write tools (execute_sql / db_*) run against
# the ADMIN role, which can do anything — so these guards prevent *accidental*
# catastrophe, not unauthorized access. The real backstop is that execute_sql
# runs every statement inside a transaction and ROLLs BACK unless the affected
# row count is within bounds (or explicitly confirmed); these string checks are
# the fast first line of defense on top of that.
# ---------------------------------------------------------------------------

class WriteSafetyError(ValueError):
    """Raised when a write statement trips a safety guard."""


_WHERE_RE = re.compile(r"\bWHERE\b", re.IGNORECASE)
_DESTRUCTIVE_RE = re.compile(
    r"\bDROP\b|\bTRUNCATE\b|\bREVOKE\b|\bALTER\b[\s\S]*?\bDROP\b",
    re.IGNORECASE,
)
# Never allowed through the tool, even with confirm_destructive — unrecoverable
# or would disable the audit trail / migration ledger itself.
_CATASTROPHIC_RE = re.compile(
    r"\bDROP\s+(DATABASE|SCHEMA|ROLE|USER|TABLESPACE|OWNED|SUBSCRIPTION)\b"
    r"|\b(?:DROP\s+TABLE|TRUNCATE)\b[\s\S]*?\b(mcp_write_audit|schema_migrations)\b",
    re.IGNORECASE,
)
_LEADING_VERB_RE = re.compile(r"^\s*([A-Za-z]+)")
_TARGET_TABLE_RE = re.compile(
    r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM|TRUNCATE(?:\s+TABLE)?|"
    r"CREATE\s+(?:TABLE|VIEW|MATERIALIZED\s+VIEW)|ALTER\s+TABLE|DROP\s+TABLE|MERGE\s+INTO)"
    r"\s+(?:IF\s+(?:NOT\s+)?EXISTS\s+)?([A-Za-z_][\w.\"]*)",
    re.IGNORECASE,
)

_VERB_TO_OP = {
    "INSERT": "INSERT", "UPDATE": "UPDATE", "DELETE": "DELETE",
    "MERGE": "UPSERT", "TRUNCATE": "TRUNCATE", "DROP": "DDL_DROP",
    "ALTER": "DDL_ALTER", "CREATE": "DDL_CREATE", "GRANT": "DCL",
    "REVOKE": "DCL", "SELECT": "SELECT", "VALUES": "SELECT",
    "COPY": "COPY", "VACUUM": "MAINT", "ANALYZE": "MAINT",
    "REINDEX": "MAINT", "REFRESH": "MAINT", "COMMENT": "DDL_COMMENT",
}


def count_statements(query: str) -> int:
    """Number of non-empty statements (string literals / comments stripped)."""
    stripped = normalize_for_check(query)
    return len([seg for seg in _STMT_END_RE.split(stripped) if seg.strip()])


def _strip_parens(s: str) -> str:
    """Remove balanced parenthesised groups (subqueries, function args) so a
    WHERE that lives only inside one isn't mistaken for a top-level WHERE."""
    out: list[str] = []
    depth = 0
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def statement_has_where(query: str) -> bool:
    """Does the statement have a TOP-LEVEL WHERE? Used to flag whole-table
    UPDATE/DELETE. We strip parenthesised groups first so a WHERE that only
    appears inside a subquery (e.g. UPDATE t SET x=(SELECT .. WHERE ..)) does
    NOT count — that statement still rewrites every row of t. The execute_sql
    affected-row backstop is the second line of defense behind this."""
    return bool(_WHERE_RE.search(_strip_parens(normalize_for_check(query))))


def classify_statement(query: str) -> str:
    """Best-effort operation label (INSERT|UPDATE|DELETE|UPSERT|SELECT|DDL_*|...).

    For a data-modifying CTE (WITH ... DELETE/UPDATE/INSERT) we return the
    modifying verb, not SELECT, so the write guards still apply."""
    norm = normalize_for_check(query).strip()
    m = _LEADING_VERB_RE.match(norm)
    verb = (m.group(1).upper() if m else "")
    if verb == "WITH":
        upper = norm.upper()
        for kw in ("DELETE", "UPDATE", "INSERT", "MERGE"):
            if re.search(rf"\b{kw}\b", upper):
                return _VERB_TO_OP[kw]
        return "SELECT"
    return _VERB_TO_OP.get(verb, "OTHER")


def extract_target_table(query: str) -> str | None:
    """Best-effort target table for the write-audit log (not security-critical)."""
    m = _TARGET_TABLE_RE.search(normalize_for_check(query))
    return m.group(1).strip('"') if m else None


def is_destructive(query: str) -> bool:
    """DROP / TRUNCATE / REVOKE / ALTER ... DROP — schema- or permission-level
    loss that should require an explicit confirm."""
    return bool(_DESTRUCTIVE_RE.search(normalize_for_check(query)))


def validate_write(
    query: str,
    *,
    allow_no_where: bool = False,
    confirm_destructive: bool = False,
) -> None:
    """Raise WriteSafetyError if a write statement trips a guard.

    - Single statement only (send one at a time).
    - Catastrophic ops (DROP DATABASE/SCHEMA/ROLE, or dropping/truncating the
      audit & migration ledgers) are refused outright.
    - UPDATE/DELETE without WHERE requires allow_no_where=True.
    - DROP/TRUNCATE/ALTER-DROP/REVOKE require confirm_destructive=True.
    """
    if not query or not query.strip():
        raise WriteSafetyError("empty statement.")
    if count_statements(query) > 1:
        raise WriteSafetyError(
            "multiple statements in one call are not allowed — send one statement "
            "at a time so each is audited and bounded independently."
        )
    if _CATASTROPHIC_RE.search(normalize_for_check(query)):
        raise WriteSafetyError(
            "refused: catastrophic operation (DROP DATABASE/SCHEMA/ROLE, or "
            "dropping/truncating mcp_write_audit / schema_migrations)."
        )
    op = classify_statement(query)
    if op in ("UPDATE", "DELETE") and not allow_no_where and not statement_has_where(query):
        raise WriteSafetyError(
            f"{op} has no WHERE clause and would affect the ENTIRE table. "
            "If that's intended, pass allow_no_where=true."
        )
    if is_destructive(query) and not confirm_destructive:
        raise WriteSafetyError(
            "destructive operation (DROP / TRUNCATE / ALTER ... DROP / REVOKE) "
            "requires confirm_destructive=true."
        )
