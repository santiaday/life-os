"""Write-audit trail.

Every mutation Claude makes to the warehouse lands in `mcp_write_audit`
(migration 0033) — the generic db_* tools, the semantic write tools, and the
possession module alike. Having one implementation here rather than a copy
per service is what keeps that promise true: a service that hand-rolls its own
INSERT eventually drifts, and the drift is invisible until you go looking for
a write that was never recorded.

Auditing never raises. A failed audit must not fail the write that prompted
it — the row is already committed by then, and turning a logging problem into
a user-visible error would be the worse outcome.
"""

from __future__ import annotations

import json
from typing import Any

from lifeos_core.db import conn
from lifeos_core.logging import get_logger

log = get_logger(__name__)

_PARAMS_MAX_CHARS = 4000


def params_summary(params: Any) -> Any:
    """Trim params for the audit log so a bulk insert doesn't store megabytes."""
    if params is None:
        return None
    try:
        s = json.dumps(params, default=str)
    except (TypeError, ValueError):
        s = str(params)
    return (s[:_PARAMS_MAX_CHARS] + "…") if len(s) > _PARAMS_MAX_CHARS else s


def record_write(
    *,
    tool: str,
    statement: str,
    operation: str | None = None,
    target_table: str | None = None,
    params: Any = None,
    affected_rows: int | None = None,
    dry_run: bool = False,
    committed: bool = True,
    ok: bool = True,
    error: str | None = None,
    result_sample: list | None = None,
) -> None:
    """Record one write attempt. Runs in its own transaction so it persists
    even when the statement it describes was rolled back."""
    try:
        with conn() as c:
            with c.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO mcp_write_audit
                      (tool, operation, target_table, statement, params,
                       affected_rows, dry_run, committed, ok, error, result_sample)
                    VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s::jsonb)
                    """,
                    [
                        tool, operation, target_table, statement,
                        # Always valid JSON — a truncated raw string would fail ::jsonb.
                        json.dumps({"params": params_summary(params)}),
                        affected_rows, dry_run, committed, ok, error,
                        json.dumps(result_sample, default=str)
                        if result_sample is not None else None,
                    ],
                )
            c.commit()
    except Exception as e:
        log.warning("audit.write_failed", error=str(e), tool=tool)
