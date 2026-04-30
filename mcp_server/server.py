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

from fastapi import FastAPI, Request
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from lifeos_core.db import close_pools, conn
from lifeos_core.logging import configure_logging, get_logger
from lifeos_core.settings import settings
from mcp_server import tools as T
from mcp_server import write_tools as W
from mcp_server.auth import (
    MCP_MOUNT,
    extract_and_validate_token,
    is_public,
)
from mcp_server.telemetry import recent_tool_perf, trace_tool

log = get_logger(__name__)

mcp = FastMCP(
    name="life-os",
    instructions=(
        "Personal life-data tools for Santi: Whoop (recovery, sleep, "
        "workouts, journal, ADVANCED LABS BIOMARKERS), Google Calendar, "
        "Cronometer, and Copilot Money in a single warehouse. "
        "Always call get_schema_docs first when answering an analytical "
        "question. Prefer mart_daily for daily-grain queries. "
        "ANY health-related question (energy, recovery problems, hormones, "
        "lipids, sleep, inflammation, vitamins, libido, weight) should "
        "ALWAYS call get_lab_results first to ground the answer in the "
        "user's actual biomarker values — out-of-range markers sort first. "
        "Use get_biomarker_info(biomarker_id) for a deep dive on any "
        "single biomarker. Use ask_sql only when no semantic tool fits."
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


def _tool(description: str):
    """Combined decorator: telemetry-wrap, then register with FastMCP. Applies
    `trace_tool()` so every call is timed, logged, and (if LIFEOS_OTLP_ENDPOINT
    is set) traced via OpenTelemetry — with no per-tool boilerplate."""
    def deco(fn):
        wrapped = trace_tool(name=fn.__name__)(fn)
        return mcp.tool(description=description)(wrapped)
    return deco


# ---- tool registrations ----------------------------------------------------
# Each wrapper has explicit type hints so FastMCP can derive a clean JSON
# schema and Claude can pick correct argument types. Bodies just delegate to
# the implementations in tools.py.

@_tool(description=T.TOOLS["get_schema_docs"]["description"])
def get_schema_docs(table_name: str | None = None) -> dict:
    return T.get_schema_docs(table_name)


@_tool(description=T.TOOLS["get_daily_summary"]["description"])
def get_daily_summary(
    start_date: date,
    end_date: date,
    columns: list[str] | None = None,
) -> dict:
    return T.get_daily_summary(start_date, end_date, columns)


@_tool(description=T.TOOLS["get_recovery_trend"]["description"])
def get_recovery_trend(
    start_date: date,
    end_date: date,
    smoothing: int | None = None,
) -> dict:
    return T.get_recovery_trend(start_date, end_date, smoothing)


@_tool(description=T.TOOLS["get_sleep_summary"]["description"])
def get_sleep_summary(
    start_date: date,
    end_date: date,
    include_naps: bool = False,
) -> dict:
    return T.get_sleep_summary(start_date, end_date, include_naps)


@_tool(description=T.TOOLS["get_workouts"]["description"])
def get_workouts(
    start_date: date,
    end_date: date,
    sport_name: str | None = None,
) -> dict:
    return T.get_workouts(start_date, end_date, sport_name)


@_tool(description=T.TOOLS["get_food_log"]["description"])
def get_food_log(
    start_date: date,
    end_date: date,
    meal_window: str | None = None,
    search: str | None = None,
) -> dict:
    return T.get_food_log(start_date, end_date, meal_window, search)


@_tool(description=T.TOOLS["get_meal_summary"]["description"])
def get_meal_summary(
    start_date: date,
    end_date: date,
    meal_window: str | None = None,
) -> dict:
    return T.get_meal_summary(start_date, end_date, meal_window)


@_tool(description=T.TOOLS["get_calendar_load"]["description"])
def get_calendar_load(start_date: date, end_date: date) -> dict:
    return T.get_calendar_load(start_date, end_date)


@_tool(description=T.TOOLS["get_calendar_events"]["description"])
def get_calendar_events(
    start_date: date,
    end_date: date,
    classification: str | None = None,
    search: str | None = None,
) -> dict:
    return T.get_calendar_events(start_date, end_date, classification, search)


@_tool(description=T.TOOLS["get_spending"]["description"])
def get_spending(
    start_date: date,
    end_date: date,
    category: str | None = None,
    group_by: str = "day",
    account_id: str | None = None,
    account: str | None = None,
    exact_category: bool = False,
    merchant: str | None = None,
) -> dict:
    return T.get_spending(
        start_date, end_date, category, group_by,
        account_id=account_id, account=account,
        exact_category=exact_category, merchant=merchant,
    )


@_tool(description=T.TOOLS["get_transactions"]["description"])
def get_transactions(
    start_date: date,
    end_date: date,
    category: str | None = None,
    merchant: str | None = None,
    min_amount: float | None = None,
    max_amount: float | None = None,
    tag: str | None = None,
    has_no_tags: bool = False,
    untagged_for_couples: bool = False,
    account_id: str | None = None,
    account: str | None = None,
    account_ids: list[str] | None = None,
    exclude_excluded: bool = True,
    only_charges: bool = False,
    exact_category: bool = False,
    limit: int = 500,
) -> dict:
    return T.get_transactions(
        start_date, end_date, category, merchant, min_amount,
        max_amount=max_amount,
        tag=tag, has_no_tags=has_no_tags,
        untagged_for_couples=untagged_for_couples,
        account_id=account_id, account=account, account_ids=account_ids,
        exclude_excluded=exclude_excluded, only_charges=only_charges,
        exact_category=exact_category, limit=limit,
    )


@_tool(description=T.TOOLS["get_biometrics"]["description"])
def get_biometrics(
    metric: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
) -> dict:
    return T.get_biometrics(metric, start_date, end_date)


@_tool(description=T.TOOLS["correlate_metrics"]["description"])
def correlate_metrics(
    metric_a: str,
    metric_b: str,
    start_date: date,
    end_date: date,
    lag_days: int = 0,
    method: str = "pearson",
    lag_range: list[int] | None = None,
    return_series: bool = True,
) -> dict:
    return T.correlate_metrics(
        metric_a, metric_b, start_date, end_date, lag_days, method,
        lag_range=lag_range, return_series=return_series,
    )


@_tool(description=(
    "List Whoop's behavior catalog (200+ trackable behaviors). Filter by "
    "category (DAYTIME / NIGHTTIME / YOUR WEEKLY PLAN / ...) or substring "
    "search across title and internal_name. Use this to discover habit_key "
    "values for get_habit_history."
))
def list_behaviors(category: str | None = None, search: str | None = None) -> dict:
    return T.list_behaviors(category, search)


@_tool(description=(
    "Daily journal entries from Whoop. Pass `day` for a single day with "
    "full payload + parsed habit log + notes. Otherwise returns a window "
    "summary: one row per day with notes + habit counts."
))
def get_journal_entries(
    start_date: date,
    end_date: date,
    day: date | None = None,
) -> dict:
    return T.get_journal_entries(start_date, end_date, day)


@_tool(description=(
    "Time series of a single Whoop journal habit (e.g. alcohol, caffeine, "
    "late-meal, magnesium). habit_key is dim_whoop_behavior.internal_name; "
    "discover values via list_behaviors. Returns rows + summary stats "
    "(n_days, yes_count, yes_rate)."
))
def get_habit_history(habit_key: str, start_date: date, end_date: date) -> dict:
    return T.get_habit_history(habit_key, start_date, end_date)


@_tool(description=T.TOOLS["list_lab_tests"]["description"])
def list_lab_tests() -> dict:
    return T.list_lab_tests()


@_tool(description=T.TOOLS["get_lab_results"]["description"])
def get_lab_results(
    biomarker_id: str | None = None,
    status: str | None = None,
    category: str | None = None,
    test_id: str | None = None,
    search: str | None = None,
) -> dict:
    return T.get_lab_results(
        biomarker_id=biomarker_id,
        status=status,
        category=category,
        test_id=test_id,
        search=search,
    )


@_tool(description=T.TOOLS["get_biomarker_info"]["description"])
def get_biomarker_info(biomarker_id: str) -> dict:
    return T.get_biomarker_info(biomarker_id)


@_tool(description=T.TOOLS["ask_sql"]["description"])
def ask_sql(
    query: str,
    max_rows: int = 200,
    timeout_ms: int | None = None,
    explain: bool = False,
) -> dict:
    return T.ask_sql(query, max_rows, timeout_ms=timeout_ms, explain=explain)


# ---- write / refresh tools ------------------------------------------------
@_tool(description=(
    "Pull fresh data from one or all sources, then rebuild the mart. Call "
    "this at the START of a chat session if the user is asking about recent "
    "data so analysis isn't on stale numbers. Default source='all' refreshes "
    "Whoop, Calendar, Cronometer, Copilot, then mart. Pass a single source "
    "name (whoop|calendar|cronometer|copilot|mart) to scope it."
))
def refresh_data(source: str = "all") -> dict:
    return W.refresh_data(source)


@_tool(description=(
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


@_tool(description=(
    "Convenience wrapper around update_transaction. Reassign category. Pass "
    "empty string to uncategorize."
))
def update_transaction_category(transaction_id: str, category_id: str) -> dict:
    return W.update_transaction_category(transaction_id, category_id)


@_tool(description=(
    "Convenience wrapper around update_transaction. Set userNotes; '' clears."
))
def update_transaction_notes(transaction_id: str, notes: str) -> dict:
    return W.update_transaction_notes(transaction_id, notes)


@_tool(description=(
    "Apply the same edit to many transactions in one call. Filter args "
    "(combine freely; AND): start_date, end_date, merchant (ILIKE), "
    "category_id_match (use '' for uncategorized), account_id, has_tag, "
    "untagged_for_couples, min_amount, max_amount, transaction_ids "
    "(explicit list — skips other filters). Edit args (apply to every "
    "match): set_category_id, set_user_notes, set_is_reviewed, "
    "set_tag_ids, set_hidden. ALWAYS pass dry_run=True FIRST to verify "
    "the filter caught the right rows before mutating. max_count caps "
    "matches at 200 by default."
))
def bulk_update_transactions(
    start_date: date | None = None,
    end_date: date | None = None,
    merchant: str | None = None,
    category_id_match: str | None = None,
    account_id: str | None = None,
    has_tag: str | None = None,
    untagged_for_couples: bool = False,
    min_amount: float | None = None,
    max_amount: float | None = None,
    transaction_ids: list[str] | None = None,
    set_category_id: str | None = None,
    set_user_notes: str | None = None,
    set_is_reviewed: bool | None = None,
    set_tag_ids: list[str] | None = None,
    set_hidden: bool | None = None,
    dry_run: bool = False,
    max_count: int = 200,
) -> dict:
    return W.bulk_update_transactions(
        start_date=start_date, end_date=end_date, merchant=merchant,
        category_id_match=category_id_match, account_id=account_id,
        has_tag=has_tag, untagged_for_couples=untagged_for_couples,
        min_amount=min_amount, max_amount=max_amount,
        transaction_ids=transaction_ids,
        set_category_id=set_category_id, set_user_notes=set_user_notes,
        set_is_reviewed=set_is_reviewed, set_tag_ids=set_tag_ids,
        set_hidden=set_hidden, dry_run=dry_run, max_count=max_count,
    )


@_tool(description=(
    "Link a transaction to an existing recurring stream. Find recurring_id "
    "via get_transactions (each txn carries its recurring_id) or ask_sql "
    "against fact_transaction.recurring_id."
))
def add_transaction_to_recurring(transaction_id: str, recurring_id: str) -> dict:
    return W.add_transaction_to_recurring(transaction_id, recurring_id)


@_tool(description=(
    "Detach a transaction from its recurring stream (e.g. one-off charge "
    "that Copilot incorrectly bucketed into your Netflix recurring)."
))
def exclude_transaction_from_recurring(transaction_id: str) -> dict:
    return W.exclude_transaction_from_recurring(transaction_id)


@_tool(description=(
    "All tags currently defined in Copilot. Call before create_tag to avoid "
    "duplicates and before tag_transaction so you know the IDs."
))
def list_tags() -> dict:
    return W.list_tags()


@_tool(description=(
    "Create a new Copilot tag. color_name accepts: red, orange, yellow, "
    "green, blue, purple, pink, gray (Copilot validates server-side)."
))
def create_tag(name: str, color_name: str | None = None) -> dict:
    return W.create_tag(name, color_name)


@_tool(description=(
    "REPLACE a transaction's tag set with the given IDs. To add or remove a "
    "single tag, fetch its current tags first (via get_transactions) and "
    "merge client-side. For couples-split tagging use set_couple_tag instead."
))
def tag_transaction(transaction_id: str, tag_ids: list[str]) -> dict:
    return W.tag_transaction(transaction_id, tag_ids)


# ---- couples-split workflow ----------------------------------------------
@_tool(description=(
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


@_tool(description=(
    "Tag a transaction as 'me' | 'partner' | 'joint'. Replaces any existing "
    "couple tag but preserves other tags (trip tags, etc.). Auto-creates the "
    "couple tags in Copilot on first use."
))
def set_couple_tag(transaction_id: str, owner: str) -> dict:
    return W.set_couple_tag(transaction_id, owner)


@_tool(description=(
    "Show every Copilot account with its configured couple-owner mapping "
    "(me|partner|joint|unassigned). Edit COUPLE_ACCOUNTS_* in .env to assign "
    "ownership; without it, compute_couple_balances skips those transactions."
))
def list_account_owners() -> dict:
    return W.list_account_owners()


@_tool(description=(
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


@_tool(description=(
    "One-shot couples owed-on-card calc. Pass account_ids OR account_names "
    "(ILIKE) to scope to specific cards (joint Chase, Amazon, etc.). Override "
    "split_me/split_partner per call (e.g. 0.65/0.35). Joint-tagged charges "
    "are split per configured ratio; me/partner-tagged go fully to that "
    "person; untagged charges are flagged in needs_review and (unless "
    "joint_only=true) treated as joint. Payments (negative amounts) are "
    "credited per tag: a 'me'-tagged payment reduces what you owe; a "
    "'joint'-tagged payment reduces the joint pool (both shares per split); "
    "untagged payments go to needs_review and are NOT auto-applied. By default "
    "auto-refreshes from Copilot if the local mirror is older than 30 minutes "
    "(refresh_if_stale_minutes=null to disable). Pending+posted duplicates "
    "auto-flagged and skipped."
))
def compute_couple_owed(
    start_date: date,
    end_date: date,
    account_ids: list[str] | None = None,
    account_names: list[str] | None = None,
    split_me: float | None = None,
    split_partner: float | None = None,
    joint_only: bool = False,
    flag_duplicate_pending: bool = True,
    include_payments: bool = True,
    refresh_if_stale_minutes: int | None = 30,
) -> dict:
    return W.compute_couple_owed(
        start_date, end_date,
        account_ids=account_ids, account_names=account_names,
        split_me=split_me, split_partner=split_partner,
        joint_only=joint_only,
        flag_duplicate_pending=flag_duplicate_pending,
        include_payments=include_payments,
        refresh_if_stale_minutes=refresh_if_stale_minutes,
    )


@_tool(description=(
    "Self-observability: latency, error rate, and call count per MCP tool over "
    "the last `window_minutes` (default 24h). Reads from mcp_tool_log which is "
    "populated automatically on every call. Use this to spot slow tools, "
    "redundant calls in a conversation, or repeated failures."
))
def get_tool_stats(window_minutes: int = 1440) -> dict:
    rows = recent_tool_perf(window_minutes=window_minutes)
    return {
        "ok": True, "tool": "get_tool_stats", "rows": rows,
        "row_count": len(rows), "truncated": False, "warnings": [],
        "window_minutes": window_minutes,
    }


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
