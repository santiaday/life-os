"""MCP HTTP server.

Uses the Anthropic MCP Python SDK's streamable-HTTP transport, mounted under
a FastAPI app that adds:
  - Bearer-token auth (except for /health)
  - GET /health  (returns ingest freshness per source — Phase 8 surface)

Each tool is registered via FastMCP's decorator, which auto-derives the JSON
input schema from the function signature. The implementations live in
mcp_server.tools so they can be unit-tested without an MCP runtime.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date

from fastapi import Depends, FastAPI, Request
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from lifeos_core.db import close_pools, conn
from lifeos_core.logging import configure_logging, get_logger
from lifeos_core.settings import settings
from mcp_server import tools as T
from mcp_server import write_tools as W
from mcp_server.auth import (
    MCP_MOUNT,
    PUBLIC_PATHS,
    extract_and_validate_token,
    is_public,
    require_bearer,
)

log = get_logger(__name__)

mcp = FastMCP(
    name="life-os",
    instructions=(
        "Personal life-data tools for Santi: Whoop, Google Calendar, "
        "Cronometer, and Copilot Money in a single warehouse. "
        "Always call get_schema_docs first when answering an analytical "
        "question. Prefer mart_daily for daily-grain queries. Use ask_sql "
        "only when no semantic tool fits."
    ),
    # The streamable-HTTP route lives at the *root* of the inner ASGI app so
    # FastAPI can mount it at /mcp without requiring the awkward
    # /mcp/mcp double-prefix.
    streamable_http_path="/",
    stateless_http=True,
    # FastMCP defaults Host-header allowlist to 127.0.0.1 only as DNS-rebind
    # protection. We're behind Caddy which forwards the real Host, so we
    # explicitly allow our public hostname.
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            "lifeos.ledion.io",
            "127.0.0.1",
            "localhost",
        ],
        allowed_origins=[
            "https://lifeos.ledion.io",
            "https://claude.ai",
            "https://*.claude.ai",
        ],
    ),
)


# ---- tool registrations ----------------------------------------------------
# Each wrapper has explicit type hints so FastMCP can derive a clean JSON
# schema and Claude can pick correct argument types. Bodies just delegate to
# the implementations in tools.py.

@mcp.tool(description=T.TOOLS["get_schema_docs"]["description"])
def get_schema_docs(table_name: str | None = None) -> dict:
    return T.get_schema_docs(table_name)


@mcp.tool(description=T.TOOLS["get_daily_summary"]["description"])
def get_daily_summary(
    start_date: date,
    end_date: date,
    columns: list[str] | None = None,
) -> dict:
    return T.get_daily_summary(start_date, end_date, columns)


@mcp.tool(description=T.TOOLS["get_recovery_trend"]["description"])
def get_recovery_trend(
    start_date: date,
    end_date: date,
    smoothing: int | None = None,
) -> dict:
    return T.get_recovery_trend(start_date, end_date, smoothing)


@mcp.tool(description=T.TOOLS["get_sleep_summary"]["description"])
def get_sleep_summary(
    start_date: date,
    end_date: date,
    include_naps: bool = False,
) -> dict:
    return T.get_sleep_summary(start_date, end_date, include_naps)


@mcp.tool(description=T.TOOLS["get_workouts"]["description"])
def get_workouts(
    start_date: date,
    end_date: date,
    sport_name: str | None = None,
) -> dict:
    return T.get_workouts(start_date, end_date, sport_name)


@mcp.tool(description=T.TOOLS["get_food_log"]["description"])
def get_food_log(
    start_date: date,
    end_date: date,
    meal_window: str | None = None,
    search: str | None = None,
) -> dict:
    return T.get_food_log(start_date, end_date, meal_window, search)


@mcp.tool(description=T.TOOLS["get_meal_summary"]["description"])
def get_meal_summary(
    start_date: date,
    end_date: date,
    meal_window: str | None = None,
) -> dict:
    return T.get_meal_summary(start_date, end_date, meal_window)


@mcp.tool(description=T.TOOLS["get_calendar_load"]["description"])
def get_calendar_load(start_date: date, end_date: date) -> dict:
    return T.get_calendar_load(start_date, end_date)


@mcp.tool(description=T.TOOLS["get_calendar_events"]["description"])
def get_calendar_events(
    start_date: date,
    end_date: date,
    classification: str | None = None,
    search: str | None = None,
) -> dict:
    return T.get_calendar_events(start_date, end_date, classification, search)


@mcp.tool(description=T.TOOLS["get_spending"]["description"])
def get_spending(
    start_date: date,
    end_date: date,
    category: str | None = None,
    group_by: str = "day",
) -> dict:
    return T.get_spending(start_date, end_date, category, group_by)


@mcp.tool(description=T.TOOLS["get_transactions"]["description"])
def get_transactions(
    start_date: date,
    end_date: date,
    category: str | None = None,
    merchant: str | None = None,
    min_amount: float | None = None,
    tag: str | None = None,
    has_no_tags: bool = False,
    untagged_for_couples: bool = False,
) -> dict:
    return T.get_transactions(
        start_date, end_date, category, merchant, min_amount,
        tag=tag, has_no_tags=has_no_tags,
        untagged_for_couples=untagged_for_couples,
    )


@mcp.tool(description=T.TOOLS["get_biometrics"]["description"])
def get_biometrics(
    metric: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
) -> dict:
    return T.get_biometrics(metric, start_date, end_date)


@mcp.tool(description=T.TOOLS["correlate_metrics"]["description"])
def correlate_metrics(
    metric_a: str,
    metric_b: str,
    start_date: date,
    end_date: date,
    lag_days: int = 0,
    method: str = "pearson",
) -> dict:
    return T.correlate_metrics(metric_a, metric_b, start_date, end_date, lag_days, method)


@mcp.tool(description=T.TOOLS["ask_sql"]["description"])
def ask_sql(query: str, max_rows: int = 200) -> dict:
    return T.ask_sql(query, max_rows)


# ---- write / refresh tools ------------------------------------------------
@mcp.tool(description=(
    "Pull fresh data from one or all sources, then rebuild the mart. Call "
    "this at the START of a chat session if the user is asking about recent "
    "data so analysis isn't on stale numbers. Default source='all' refreshes "
    "Whoop, Calendar, Cronometer, Copilot, then mart. Pass a single source "
    "name (whoop|calendar|cronometer|copilot|mart) to scope it."
))
def refresh_data(source: str = "all") -> dict:
    return W.refresh_data(source)


@mcp.tool(description=(
    "Universal Copilot transaction edit. Pass any combination of fields; "
    "None means leave unchanged, '' clears a string field. Available fields: "
    "category_id, user_notes, name, amount, date (YYYY-MM-DD), tip_amount, "
    "is_reviewed, copilot_type, hidden, tag_ids (REPLACES tag set). "
    "For couples-tag flow use set_couple_tag instead. For recurring stream "
    "linking use add_to_recurring / exclude_from_recurring. Local fact row "
    "is re-fetched after mutation so reads see fresh state."
))
def update_transaction(
    transaction_id: str,
    category_id: str | None = None,
    user_notes: str | None = None,
    name: str | None = None,
    amount: float | None = None,
    date: str | None = None,
    tip_amount: float | None = None,
    is_reviewed: bool | None = None,
    copilot_type: str | None = None,
    hidden: bool | None = None,
    tag_ids: list[str] | None = None,
) -> dict:
    return W.update_transaction(
        transaction_id, category_id, user_notes, name, amount, date,
        tip_amount, is_reviewed, copilot_type, hidden, tag_ids,
    )


@mcp.tool(description=(
    "Convenience wrapper around update_transaction. Reassign category. Pass "
    "empty string to uncategorize."
))
def update_transaction_category(transaction_id: str, category_id: str) -> dict:
    return W.update_transaction_category(transaction_id, category_id)


@mcp.tool(description=(
    "Convenience wrapper around update_transaction. Set userNotes; '' clears."
))
def update_transaction_notes(transaction_id: str, notes: str) -> dict:
    return W.update_transaction_notes(transaction_id, notes)


@mcp.tool(description=(
    "Apply the same edit to every transaction matching `filter`. Use for "
    "bulk-categorize / bulk-tag. `filter` is a Copilot TransactionFilter "
    "dict — common keys: categoryId (use '' for uncategorized), merchantName, "
    "accountIds, tagIds, startDate, endDate, hidden, isReviewed. Editable: "
    "category_id, user_notes, is_reviewed, tag_ids, hidden. Local fact rows "
    "are NOT auto-refreshed; call refresh_data('copilot') after if you need "
    "fresh local state."
))
def bulk_update_transactions(
    filter: dict,
    category_id: str | None = None,
    user_notes: str | None = None,
    is_reviewed: bool | None = None,
    tag_ids: list[str] | None = None,
    hidden: bool | None = None,
) -> dict:
    return W.bulk_update_transactions(
        filter, category_id, user_notes, is_reviewed, tag_ids, hidden,
    )


@mcp.tool(description=(
    "Link a transaction to an existing recurring stream. Find recurring_id "
    "via get_transactions (each txn carries its recurring_id) or ask_sql "
    "against fact_transaction.recurring_id."
))
def add_transaction_to_recurring(transaction_id: str, recurring_id: str) -> dict:
    return W.add_transaction_to_recurring(transaction_id, recurring_id)


@mcp.tool(description=(
    "Detach a transaction from its recurring stream (e.g. one-off charge "
    "that Copilot incorrectly bucketed into your Netflix recurring)."
))
def exclude_transaction_from_recurring(transaction_id: str) -> dict:
    return W.exclude_transaction_from_recurring(transaction_id)


@mcp.tool(description=(
    "All tags currently defined in Copilot. Call before create_tag to avoid "
    "duplicates and before tag_transaction so you know the IDs."
))
def list_tags() -> dict:
    return W.list_tags()


@mcp.tool(description=(
    "Create a new Copilot tag. color_name accepts: red, orange, yellow, "
    "green, blue, purple, pink, gray (Copilot validates server-side)."
))
def create_tag(name: str, color_name: str | None = None) -> dict:
    return W.create_tag(name, color_name)


@mcp.tool(description=(
    "REPLACE a transaction's tag set with the given IDs. To add or remove a "
    "single tag, fetch its current tags first (via get_transactions) and "
    "merge client-side. For couples-split tagging use set_couple_tag instead."
))
def tag_transaction(transaction_id: str, tag_ids: list[str]) -> dict:
    return W.tag_transaction(transaction_id, tag_ids)


# ---- couples-split workflow ----------------------------------------------
@mcp.tool(description=(
    "Transactions in [start_date, end_date] that have NONE of the couple "
    "tags (me/partner/joint). Defaults to last 30 days if dates omitted. "
    "Use this to build the queue for the categorization conversation."
))
def list_pending_couple_review(
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int = 50,
) -> dict:
    return W.list_pending_couple_review(start_date, end_date, limit)


@mcp.tool(description=(
    "Tag a transaction as 'me' | 'partner' | 'joint'. Replaces any existing "
    "couple tag but preserves other tags (trip tags, etc.). Auto-creates the "
    "couple tags in Copilot on first use."
))
def set_couple_tag(transaction_id: str, owner: str) -> dict:
    return W.set_couple_tag(transaction_id, owner)


@mcp.tool(description=(
    "Show every Copilot account with its configured couple-owner mapping "
    "(me|partner|joint|unassigned). Edit COUPLE_ACCOUNTS_* in .env to assign "
    "ownership; without it, compute_couple_balances skips those transactions."
))
def list_account_owners() -> dict:
    return W.list_account_owners()


@mcp.tool(description=(
    "Compute who owes whom for the period using couple tags + account "
    "ownership. For each tagged transaction: identifies the payer from the "
    "account, applies the configured split (default 50/50) for joint expenses "
    "or full amount for cross-paid personal expenses. Returns net 'me owes "
    "partner' figure plus per-transaction breakdown. Skips transactions whose "
    "account isn't in the COUPLE_ACCOUNTS_* mapping (count surfaced)."
))
def compute_couple_balances(
    start_date: date,
    end_date: date,
    include_personal: bool = False,
) -> dict:
    return W.compute_couple_balances(start_date, end_date, include_personal)


# ---- FastAPI shell ----------------------------------------------------------
def _make_lifespan(mcp_asgi):
    """Combined lifespan: ours (logging, DB pool teardown) + FastMCP's session
    manager (which starts an anyio task group needed by streamable_http)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        configure_logging()
        log.info("mcp.startup", host=settings.MCP_BIND_HOST, port=settings.MCP_BIND_PORT)
        # Enter the inner Starlette's lifespan so FastMCP's task group is
        # initialized before any request hits the streamable-http handler.
        inner_cm = mcp_asgi.router.lifespan_context(mcp_asgi)
        async with inner_cm:
            yield
        close_pools()
        log.info("mcp.shutdown")

    return lifespan


def build_app() -> FastAPI:
    """Outer FastAPI app. Mounts the MCP streamable-HTTP ASGI app at /mcp and
    exposes /health unauth'd alongside it."""
    mcp_asgi = _mcp_asgi_app()
    app = FastAPI(title="life-os MCP", lifespan=_make_lifespan(mcp_asgi))

    @app.get("/health", include_in_schema=False)
    def health(_: None = None) -> dict:
        # Last-success-per-source. Drives the Phase 8 alerting story.
        with conn() as c, c.cursor() as cur:
            cur.execute(
                """
                SELECT source,
                       MAX(started_at) FILTER (WHERE status = 'success') AS last_success_at,
                       MAX(started_at) AS last_attempt_at
                FROM ingestion_runs
                GROUP BY source
                """
            )
            rows = cur.fetchall()
        out = {
            r["source"]: {
                "last_success_at": r["last_success_at"].isoformat() if r["last_success_at"] else None,
                "last_attempt_at": r["last_attempt_at"].isoformat() if r["last_attempt_at"] else None,
            }
            for r in rows
        }
        return {"ok": True, "ingest_runs": out}

    # claude.ai's MCP client probes well-known OAuth metadata before it'll
    # talk to the server. Return shapes that say "no auth required". Combined
    # with the path-secret URL, this is enough to satisfy the discovery dance
    # for a personal connector.
    @app.get("/.well-known/oauth-protected-resource", include_in_schema=False)
    @app.get("/.well-known/oauth-protected-resource/{rest:path}", include_in_schema=False)
    def oauth_protected_resource(rest: str = "") -> dict:
        from lifeos_core.settings import settings as _s
        return {
            "resource": _s.MCP_PUBLIC_BASE_URL or "",
            "authorization_servers": [],
            "bearer_methods_supported": [],
        }

    @app.get("/.well-known/oauth-authorization-server", include_in_schema=False)
    def oauth_authorization_server() -> dict:
        from lifeos_core.settings import settings as _s
        # Minimal RFC 8414 metadata; no real endpoints because there is no real
        # OAuth server. claude.ai falls back to anonymous when registration 404s.
        return {
            "issuer": _s.MCP_PUBLIC_BASE_URL or "",
            "authorization_endpoint": (_s.MCP_PUBLIC_BASE_URL or "") + "/oauth/authorize",
            "token_endpoint": (_s.MCP_PUBLIC_BASE_URL or "") + "/oauth/token",
            "registration_endpoint": (_s.MCP_PUBLIC_BASE_URL or "") + "/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "code_challenge_methods_supported": ["S256"],
        }

    # Path-secret auth: requests to /mcp/<MCP_API_KEY>[/...] are rewritten to
    # /mcp/[...] (preserving the trailing slash that FastMCP's mount expects)
    # before the inner ASGI app sees them. /health, /webhooks/*, and
    # /.well-known/* are exempt.
    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        path = request.url.path
        if is_public(path) or path.startswith("/.well-known/"):
            return await call_next(request)
        if path.startswith(MCP_MOUNT + "/") or path == MCP_MOUNT:
            rewritten = extract_and_validate_token(path)
            if rewritten is None:
                from fastapi.responses import JSONResponse
                return JSONResponse(
                    status_code=401,
                    content={"error": "Unauthorized — bad or missing path-secret."},
                )
            # FastMCP's streamable-http route is at the inner root '/'. After
            # FastAPI's mount at /mcp strips the prefix, the inner app receives
            # whatever's after /mcp. So `/mcp` (bare) lands on the inner '/' —
            # which Starlette treats as "" and sometimes mishandles. Always
            # ensure the rewritten path ends with a trailing slash.
            if rewritten == MCP_MOUNT:
                rewritten = MCP_MOUNT + "/"
            request.scope["path"] = rewritten
            request.scope["raw_path"] = rewritten.encode()
            return await call_next(request)
        # Anything outside /mcp, /health, /webhooks, /.well-known: reject.
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=404, content={"error": "Not found"})

    # Mount the MCP streamable HTTP ASGI app under /mcp. mcp_asgi was created
    # at the top of build_app() so the lifespan can initialize FastMCP's task
    # group on the same instance.
    app.mount("/mcp", mcp_asgi)

    # Phase 9: Whoop webhooks. Self-disables if WHOOP_WEBHOOK_SECRET unset.
    try:
        from ingest_whoop.webhooks import router as whoop_webhook_router
        app.include_router(whoop_webhook_router)
    except ImportError as e:  # pragma: no cover
        log.warning("mcp.webhook_router_unavailable", error=str(e))

    return app


def _mcp_asgi_app():
    """Return the MCP server's ASGI app, tolerating SDK API shifts."""
    for attr in ("streamable_http_app", "http_app", "asgi_app"):
        builder = getattr(mcp, attr, None)
        if builder is not None:
            return builder()
    raise RuntimeError(
        "Couldn't locate the streamable-HTTP ASGI app on the MCP SDK. "
        "Check the installed `mcp` package version (expected >=1.0)."
    )


def main() -> int:
    import uvicorn

    configure_logging()
    uvicorn.run(
        build_app(),
        host=settings.MCP_BIND_HOST,
        port=settings.MCP_BIND_PORT,
        log_config=None,  # let structlog handle it
        # Trust X-Forwarded-Proto/For from Caddy so redirects come back as
        # https://, not http://.
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
    return 0


if __name__ == "__main__":
    main()
