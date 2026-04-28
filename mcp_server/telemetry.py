"""Tool-call telemetry.

Wraps every MCP tool with a decorator that:
  1. Times the call.
  2. Captures whether it succeeded, how many rows it returned, whether the
     result was truncated, and any error type/message.
  3. Hashes scalar args (dates, ints, bools, short strings) to a small JSONB
     summary. Skips long free-text args (SQL, notes) to keep the log compact
     and to avoid accidentally storing PII at scale.
  4. Inserts a row into mcp_tool_log via a dedicated short-lived connection
     so a slow tool query can't be made worse by a contended logging insert.

If LIFEOS_OTLP_ENDPOINT is set (e.g. a Langfuse OTLP collector), the same
event is also emitted as an OpenTelemetry span so traces show up in
Langfuse / Grafana Cloud / any other OTLP backend.

The Postgres-side log is the source of truth — OTLP is opt-in.
"""

from __future__ import annotations

import functools
import threading
import time
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from psycopg.types.json import Jsonb

from lifeos_core.db import conn
from lifeos_core.logging import get_logger
from lifeos_core.settings import settings

log = get_logger(__name__)

# Args longer than this aren't recorded verbatim. (We keep their length so
# you can still spot "this tool got a 90-line SQL query.")
_MAX_ARG_VALUE_LEN = 120

# Background flush queue so logging never blocks a tool response.
_LOG_LOCK = threading.Lock()
_PENDING: list[dict] = []
_FLUSHER_STARTED = False


# ---- OTLP (optional) -------------------------------------------------------
_OTEL_TRACER = None
_OTEL_INIT_TRIED = False


def _otel_tracer():
    """Lazily initialize an OTLP HTTP exporter pointing at whatever endpoint
    is configured in LIFEOS_OTLP_ENDPOINT. Compatible with Langfuse Cloud
    (free tier) and Grafana Cloud (free tier). Returns None if either the
    endpoint isn't set or the OTel SDK isn't installed."""
    global _OTEL_TRACER, _OTEL_INIT_TRIED
    if _OTEL_INIT_TRIED:
        return _OTEL_TRACER
    _OTEL_INIT_TRIED = True

    endpoint = settings.LIFEOS_OTLP_ENDPOINT
    if not endpoint:
        return None
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        headers: dict[str, str] = {}
        # Langfuse / Grafana Cloud both use Basic auth via OTLP. If the user
        # set LIFEOS_OTLP_HEADERS, parse "k=v,k2=v2".
        raw = settings.LIFEOS_OTLP_HEADERS
        if raw:
            for pair in raw.split(","):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    headers[k.strip()] = v.strip()

        provider = TracerProvider(resource=Resource.create({
            "service.name": settings.LIFEOS_SERVICE_NAME,
        }))
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, headers=headers))
        )
        trace.set_tracer_provider(provider)
        _OTEL_TRACER = trace.get_tracer("life-os.mcp")
        log.info("telemetry.otlp_enabled", endpoint=endpoint)
    except Exception as e:
        log.warning("telemetry.otlp_init_failed", error=str(e))
        _OTEL_TRACER = None
    return _OTEL_TRACER


# ---- arg summarization -----------------------------------------------------
def _summarize_args(args: tuple, kwargs: dict) -> dict:
    """Compact, JSON-safe representation of the tool's call args. Long strings
    (SQL queries, free-text notes) get truncated with their original length
    preserved so we can spot slow LLM-generated queries."""
    out: dict[str, Any] = {}
    if args:
        out["_positional"] = [_summarize_value(v) for v in args]
    for k, v in kwargs.items():
        out[k] = _summarize_value(v)
    return out


def _summarize_value(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, float)):
        return v
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if isinstance(v, str):
        if len(v) > _MAX_ARG_VALUE_LEN:
            return {"_truncated": True, "len": len(v),
                    "head": v[:_MAX_ARG_VALUE_LEN]}
        return v
    if isinstance(v, (list, tuple)):
        if len(v) > 10:
            return {"_list_len": len(v), "head": [_summarize_value(x) for x in v[:5]]}
        return [_summarize_value(x) for x in v]
    if isinstance(v, dict):
        return {k: _summarize_value(val) for k, val in list(v.items())[:20]}
    return str(v)[:_MAX_ARG_VALUE_LEN]


# ---- background flusher ----------------------------------------------------
def _start_flusher_once() -> None:
    """One daemon thread per process that drains _PENDING into Postgres every
    500ms. Keeps tool responses off the critical path of a DB insert."""
    global _FLUSHER_STARTED
    if _FLUSHER_STARTED:
        return
    _FLUSHER_STARTED = True

    def loop() -> None:
        while True:
            time.sleep(0.5)
            _flush()

    t = threading.Thread(target=loop, name="mcp-tool-log-flusher", daemon=True)
    t.start()


def _flush() -> None:
    if not _PENDING:
        return
    with _LOG_LOCK:
        batch = list(_PENDING)
        _PENDING.clear()
    if not batch:
        return
    try:
        with conn() as c, c.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO mcp_tool_log
                  (started_at, tool_name, duration_ms, ok, row_count, truncated,
                   error_type, error_message, args_summary, caller)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        r["started_at"], r["tool_name"], r["duration_ms"], r["ok"],
                        r.get("row_count"), r.get("truncated"),
                        r.get("error_type"), r.get("error_message"),
                        Jsonb(r.get("args_summary") or {}),
                        r.get("caller", "mcp"),
                    )
                    for r in batch
                ],
            )
            c.commit()
    except Exception as e:
        # Logging must never break the request. If the DB is unhappy, just
        # drop the batch and continue.
        log.warning("telemetry.flush_failed", error=str(e), dropped=len(batch))


def _enqueue(record: dict) -> None:
    _start_flusher_once()
    with _LOG_LOCK:
        _PENDING.append(record)


# ---- the decorator ---------------------------------------------------------
def trace_tool(name: str | None = None) -> Callable:
    """Wrap a tool function so every call is timed and logged."""
    def deco(fn: Callable) -> Callable:
        tool_name = name or fn.__name__

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            started = datetime.utcnow()
            t0 = time.perf_counter()
            tracer = _otel_tracer()
            span_cm = tracer.start_as_current_span(f"mcp.{tool_name}") if tracer else None
            span = span_cm.__enter__() if span_cm is not None else None

            err_type: str | None = None
            err_msg: str | None = None
            ok = True
            row_count: int | None = None
            truncated: bool | None = None
            try:
                result = fn(*args, **kwargs)
                if isinstance(result, dict):
                    if result.get("ok") is False:
                        ok = False
                        err_type = result.get("error_type")
                        err_msg = result.get("error")
                    else:
                        row_count = result.get("row_count")
                        truncated = result.get("truncated")
                return result
            except BaseException as e:
                ok = False
                err_type = type(e).__name__
                err_msg = str(e)
                raise
            finally:
                duration_ms = int((time.perf_counter() - t0) * 1000)
                if span is not None:
                    span.set_attribute("mcp.tool", tool_name)
                    span.set_attribute("mcp.duration_ms", duration_ms)
                    span.set_attribute("mcp.ok", ok)
                    if row_count is not None:
                        span.set_attribute("mcp.row_count", row_count)
                    if err_type:
                        span.set_attribute("mcp.error_type", err_type)
                if span_cm is not None:
                    span_cm.__exit__(None, None, None)
                _enqueue({
                    "started_at": started,
                    "tool_name": tool_name,
                    "duration_ms": duration_ms,
                    "ok": ok,
                    "row_count": row_count,
                    "truncated": truncated,
                    "error_type": err_type,
                    "error_message": (err_msg or "")[:500] if err_msg else None,
                    "args_summary": _summarize_args(args, kwargs),
                    "caller": "mcp",
                })

        return wrapper

    return deco


# ---- public stats helpers --------------------------------------------------
def recent_tool_perf(window_minutes: int = 1440) -> list[dict]:
    """Surface the rolling perf view to MCP. Used by the get_tool_stats tool
    so users (and Claude) can self-diagnose slow paths without leaving the
    chat."""
    with conn() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT tool_name,
                   COUNT(*)                                                       AS n,
                   COUNT(*) FILTER (WHERE NOT ok)                                 AS errors,
                   ROUND(AVG(duration_ms))::INT                                   AS mean_ms,
                   PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY duration_ms)::INT AS p50_ms,
                   PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY duration_ms)::INT AS p95_ms,
                   MAX(duration_ms)                                               AS max_ms
            FROM mcp_tool_log
            WHERE started_at >= now() - (%s || ' minutes')::INTERVAL
            GROUP BY tool_name
            ORDER BY n DESC
            """,
            [str(window_minutes)],
        )
        return [dict(r) for r in cur.fetchall()]


def force_flush() -> int:
    """Drain the pending queue immediately. Used by tests so the asserts can
    see the rows just written. Returns the number of rows flushed."""
    with _LOG_LOCK:
        n = len(_PENDING)
    _flush()
    return n
