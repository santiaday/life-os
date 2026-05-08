"""Write-side MCP tools.

Three groups:

  1. Refresh — pull the latest from any source on demand. Use this at the
     start of a chat session so analysis isn't on stale data.

  2. Generic Copilot writes — categorize, tag, edit notes, create tags. Each
     write also re-fetches the affected transaction so subsequent reads in
     the same session are fresh.

  3. Couples-split workflow — list untagged transactions, attach me/paulina/
     joint tags, then compute who owes whom on shared cards.

All tools return the standard envelope from tools.py (`_ok`/`_err`).
"""

from __future__ import annotations

from datetime import UTC, date
from typing import Any

from ingest_copilot import ingest as copilot_ingest
from ingest_copilot.graphql import GraphQLClient
from lifeos_core.db import conn
from lifeos_core.logging import get_logger
from lifeos_core.settings import settings
from mcp_server.tools import _err, _ok, _serialize

log = get_logger(__name__)


# ---- refresh tools ----------------------------------------------------------
def refresh_data(source: str = "all") -> dict:
    """Pull fresh data from one or all sources, then rebuild the mart.

    `source` ∈ {all, whoop, calendar, cronometer, copilot, mart}. Default 'all'
    re-runs every ingester and the mart. Use this at session start to ensure
    you're not analyzing stale data."""
    valid = {"all", "whoop", "whoop_journal", "whoop_labs", "hevy", "pushpress",
             "coach", "calendar", "cronometer", "copilot", "mart"}
    if source not in valid:
        return _err("refresh_data", ValueError(f"source must be one of {sorted(valid)}"))

    out: dict[str, Any] = {}
    targets = (
        ("whoop", "whoop_journal", "whoop_labs", "hevy", "pushpress", "coach",
         "calendar", "cronometer", "copilot")
        if source == "all" else (source,)
    )

    if source != "mart":
        for name in targets:
            try:
                if name == "whoop":
                    from ingest_whoop import ingest as whoop_ingest
                    out[name] = whoop_ingest.run_all()
                elif name == "whoop_journal":
                    from ingest_whoop_journal import ingest as journal_ingest
                    out[name] = journal_ingest.run_all()
                elif name == "whoop_labs":
                    # Catalog-only refresh: panel ingestion is file-based
                    # (Whoop has no public API yet) so it's not part of the
                    # automatic source list. Re-seeds dim_lab_biomarker from
                    # the curated reference data — idempotent.
                    from ingest_whoop_labs import ingest as labs_ingest
                    out[name] = {"biomarker_catalog": labs_ingest.ingest_biomarker_catalog()}
                elif name == "hevy":
                    from ingest_hevy import ingest as hevy_ingest
                    out[name] = hevy_ingest.run_all()
                elif name == "pushpress":
                    from ingest_pushpress import ingest as pushpress_ingest
                    out[name] = pushpress_ingest.run_all()
                elif name == "coach":
                    from coach import orchestrator as coach_orch
                    out[name] = coach_orch.run_all()
                elif name == "calendar":
                    from ingest_calendar import ingest as calendar_ingest
                    out[name] = calendar_ingest.run_all()
                elif name == "cronometer":
                    from ingest_cronometer import ingest as cron_ingest
                    out[name] = cron_ingest.run_all()
                elif name == "copilot":
                    out[name] = copilot_ingest.run_all()
            except Exception as e:
                out[name] = f"FAILED: {type(e).__name__}: {e}"
                log.exception("refresh_data.source_failed", source=name)

    # Always rebuild mart unless caller explicitly wants only one source's
    # raw refresh (in which case we still rebuild — it's cheap and keeps
    # consistency).
    try:
        from mart_refresh.refresh import refresh_all as mart_refresh_all
        out["mart"] = mart_refresh_all()
    except Exception as e:
        out["mart"] = f"FAILED: {type(e).__name__}: {e}"
        log.exception("refresh_data.mart_failed")

    return _ok("refresh_data", [out])


# ---- generic Copilot writes -------------------------------------------------
def _lookup_transaction_locator(transaction_id: str) -> tuple[str | None, str | None]:
    """Fetch (item_id, account_id) for a transaction from the local fact
    table. Returns (None, None) if not found locally."""
    with conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT item_id, account_id FROM fact_transaction WHERE transaction_id = %s",
            [transaction_id],
        )
        row = cur.fetchone()
    if row is None:
        return None, None
    return row.get("item_id"), row.get("account_id")


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
    """Universal Copilot transaction edit. Pass any combination of the
    optional fields; None means "leave unchanged". To clear a string field
    pass empty string. After mutation, the local fact row is re-fetched so
    subsequent reads see fresh state.

    For tag mutations: `tag_ids` REPLACES the tag set. To add or remove
    individual tags, fetch current tag_ids via get_transactions and merge.
    For couples-tag workflow use set_couple_tag instead — it handles the
    me/partner/joint tag preservation logic.

    For recurring stream linking use add_to_recurring / exclude_from_recurring.
    """
    item_id, account_id = _lookup_transaction_locator(transaction_id)
    if not item_id or not account_id:
        return _err("update_transaction", ValueError(
            f"Missing item_id/account_id for transaction {transaction_id}. "
            f"Run refresh_data('copilot') first so the local fact table has "
            f"the locator triple Copilot's editTransaction mutation requires."))

    try:
        with GraphQLClient() as client:
            updated = client.edit_transaction(
                transaction_id=transaction_id,
                item_id=item_id,
                account_id=account_id,
                category_id=category_id,
                user_notes=user_notes,
                name=name,
                amount=amount,
                date=date,
                tip_amount=tip_amount,
                is_reviewed=is_reviewed,
                type=copilot_type,
                hidden=hidden,
                tag_ids=tag_ids,
            )
        # editTransaction returns the post-mutation transaction. Persist it
        # locally so subsequent reads in this session are fresh — avoids
        # needing a separate query (whose single-txn-fetch schema we don't
        # fully know).
        local = (
            copilot_ingest.upsert_transaction_from_api(updated)
            if updated and updated.get("id") else None
        )
    except Exception as e:
        return _err("update_transaction", e)
    return _ok(
        "update_transaction",
        [{"copilot_response": updated, "local_after_refresh": _coerce_row(local)}],
    )


def update_transaction_category(transaction_id: str, category_id: str) -> dict:
    """Convenience wrapper around update_transaction. Reassign a transaction's
    category. Pass empty string to uncategorize."""
    return update_transaction(transaction_id, category_id=category_id)


def update_transaction_notes(transaction_id: str, notes: str) -> dict:
    """Convenience wrapper around update_transaction. Set userNotes; pass ''
    to clear."""
    return update_transaction(transaction_id, user_notes=notes)


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
    # ---- the edit to apply ----
    set_category_id: str | None = None,
    set_user_notes: str | None = None,
    set_is_reviewed: bool | None = None,
    set_tag_ids: list[str] | None = None,
    set_hidden: bool | None = None,
    dry_run: bool = False,
    max_count: int = 200,
) -> dict:
    """Apply the same edit to every locally-known transaction matching the
    filter args. Implementation: SELECT from fact_transaction, then loop
    update_transaction per row.

    Filter args (combine freely; AND semantics):
      start_date, end_date         date range (date column)
      merchant                     ILIKE substring match on merchant name
      category_id_match            exact category_id (use '' for uncategorized)
      account_id                   exact account_id
      has_tag                      ILIKE substring match against any tag
      untagged_for_couples         no me/partner/joint tag (for couples flow)
      min_amount, max_amount       amount range (Copilot sign convention)
      transaction_ids              explicit id list (skips other filters)

    Edit args (apply to every match; combine freely):
      set_category_id, set_user_notes, set_is_reviewed, set_tag_ids,
      set_hidden

    `dry_run=True` returns the matched rows without mutating Copilot. Always
    do this first when batch-editing > 5 rows so you can verify the filter
    caught the right things.

    `max_count` defaults to 200 for safety; pass higher only when intentional.

    Returns per-row results so you can see which succeeded and which failed.
    """
    edits = {
        "category_id": set_category_id,
        "user_notes": set_user_notes,
        "is_reviewed": set_is_reviewed,
        "tag_ids": set_tag_ids,
        "hidden": set_hidden,
    }
    edits = {k: v for k, v in edits.items() if v is not None}
    if not edits:
        return _err("bulk_update_transactions",
                    ValueError("provide at least one set_* field"))

    # ---- build the local SQL filter ----
    if transaction_ids:
        where = ["transaction_id = ANY(%s)"]
        params: list = [transaction_ids]
    else:
        where = ["NOT is_excluded"]
        params = []
        if start_date is not None:
            where.append("date >= %s")
            params.append(start_date)
        if end_date is not None:
            where.append("date <= %s")
            params.append(end_date)
        if merchant:
            where.append("merchant ILIKE %s")
            params.append(f"%{merchant}%")
        if category_id_match is not None:
            if category_id_match == "":
                where.append("category_id IS NULL")
            else:
                where.append("category_id = %s")
                params.append(category_id_match)
        if account_id:
            where.append("account_id = %s")
            params.append(account_id)
        if has_tag:
            where.append("EXISTS (SELECT 1 FROM unnest(tags) x WHERE x ILIKE %s)")
            params.append(f"%{has_tag}%")
        if untagged_for_couples:
            couple_names = [
                settings.COUPLE_TAG_ME.lower(),
                settings.COUPLE_TAG_PARTNER.lower(),
                settings.COUPLE_TAG_JOINT.lower(),
            ]
            where.append(
                "NOT EXISTS (SELECT 1 FROM unnest(tags) x WHERE LOWER(x) = ANY(%s))"
            )
            params.append(couple_names)
        if min_amount is not None:
            where.append("amount >= %s")
            params.append(min_amount)
        if max_amount is not None:
            where.append("amount <= %s")
            params.append(max_amount)

    q = f"""
        SELECT transaction_id, date, amount, merchant, category_id, tags
        FROM fact_transaction
        WHERE {" AND ".join(where)}
        ORDER BY date DESC
        LIMIT %s
    """
    params.append(max_count + 1)

    with conn() as c, c.cursor() as cur:
        cur.execute(q, params)
        rows = cur.fetchall()

    truncated = len(rows) > max_count
    if truncated:
        rows = rows[:max_count]

    if dry_run:
        return _ok(
            "bulk_update_transactions",
            _serialize(rows),
            truncated=truncated,
            extra={
                "matched_count": len(rows),
                "would_apply": edits,
                "dry_run": True,
                "note": "Re-run with dry_run=False to actually apply.",
            },
        )

    results: list[dict] = []
    succeeded = 0
    failed = 0
    for row in rows:
        out = update_transaction(row["transaction_id"], **edits)
        results.append({
            "transaction_id": row["transaction_id"],
            "merchant": row["merchant"],
            "ok": out.get("ok", False),
            "error": out.get("error"),
        })
        if out.get("ok"):
            succeeded += 1
        else:
            failed += 1

    return _ok(
        "bulk_update_transactions",
        results,
        truncated=truncated,
        extra={
            "matched_count": len(rows),
            "succeeded": succeeded,
            "failed": failed,
            "applied": edits,
        },
    )


def add_transaction_to_recurring(transaction_id: str, recurring_id: str) -> dict:
    """Link a transaction to an existing recurring stream. Find recurring
    stream ids by inspecting fact_transaction.recurring_id values via
    get_transactions or ask_sql. Local fact row is updated from the
    mutation response (just the recurring_id field changes)."""
    try:
        with GraphQLClient() as client:
            updated = client.add_transaction_to_recurring(transaction_id, recurring_id)
        # Mutation returns only {id, recurringId} — patch our local row
        # directly without re-fetching.
        with conn() as c, c.cursor() as cur:
            cur.execute(
                "UPDATE fact_transaction SET is_recurring = TRUE WHERE transaction_id = %s",
                [transaction_id],
            )
    except Exception as e:
        return _err("add_transaction_to_recurring", e)
    return _ok("add_transaction_to_recurring", [{"copilot_response": updated}])


def exclude_transaction_from_recurring(transaction_id: str) -> dict:
    """Detach from recurring stream."""
    try:
        with GraphQLClient() as client:
            updated = client.exclude_transaction_from_recurring(transaction_id)
        with conn() as c, c.cursor() as cur:
            cur.execute(
                "UPDATE fact_transaction SET is_recurring = FALSE WHERE transaction_id = %s",
                [transaction_id],
            )
    except Exception as e:
        return _err("exclude_transaction_from_recurring", e)
    return _ok("exclude_transaction_from_recurring", [{"copilot_response": updated}])


def list_tags() -> dict:
    """All tags currently defined in Copilot. Call before create_tag to avoid
    duplicates and before tag_transaction so you know the IDs."""
    try:
        with GraphQLClient() as client:
            tags = client.tags()
    except Exception as e:
        return _err("list_tags", e)
    return _ok("list_tags", tags)


def create_tag(name: str, color_name: str | None = None) -> dict:
    """Create a new tag. Color names accepted by Copilot include red, orange,
    yellow, green, blue, purple, pink, gray (server validates)."""
    try:
        with GraphQLClient() as client:
            tag = client.create_tag(name, color_name=color_name)
    except Exception as e:
        return _err("create_tag", e)
    return _ok("create_tag", [tag])


def tag_transaction(transaction_id: str, tag_ids: list[str]) -> dict:
    """REPLACE the transaction's tag set with the given IDs. To add or remove
    a single tag, fetch current tags first via get_transactions and merge.
    Routes through the universal editTransaction mutation."""
    return update_transaction(transaction_id, tag_ids=tag_ids)


# ---- couples-split workflow -------------------------------------------------
def _ensure_couple_tag_ids() -> dict[str, str]:
    """Look up (or create) the three couple tags and return {role: tag_id}.

    role ∈ {me, partner, joint}. Tag names come from settings.COUPLE_TAG_*."""
    wanted = {
        "me": settings.COUPLE_TAG_ME,
        "partner": settings.COUPLE_TAG_PARTNER,
        "joint": settings.COUPLE_TAG_JOINT,
    }
    with GraphQLClient() as client:
        existing = {t["name"].lower(): t for t in client.tags()}
        out: dict[str, str] = {}
        for role, name in wanted.items():
            t = existing.get(name.lower())
            if t is None:
                created = client.create_tag(name)
                out[role] = created["id"]
                log.info("couples.tag_created", role=role, name=name, id=created["id"])
            else:
                out[role] = t["id"]
    return out


def list_pending_couple_review(
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int = 50,
) -> dict:
    """Transactions in the window that have NONE of the couple tags
    (me/partner/joint). Reads tags from the local fact table — no Copilot
    round-trip needed (sync runs in background via cron + refresh_data).

    If dates are omitted, defaults to last 30 days."""
    from datetime import date as _date
    from datetime import timedelta

    if start_date is None:
        start_date = _date.today() - timedelta(days=30)
    if end_date is None:
        end_date = _date.today()

    couple_names = [
        settings.COUPLE_TAG_ME.lower(),
        settings.COUPLE_TAG_PARTNER.lower(),
        settings.COUPLE_TAG_JOINT.lower(),
    ]

    with conn() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT t.transaction_id, t.date, t.amount, t.merchant, t.notes,
                   c.name AS category, a.name AS account, t.account_id,
                   t.tags AS current_tags, t.is_pending
            FROM fact_transaction t
            LEFT JOIN dim_category c ON c.category_id = t.category_id
            LEFT JOIN dim_account  a ON a.account_id  = t.account_id
            WHERE t.date BETWEEN %s AND %s
              AND NOT t.is_excluded
              AND t.amount > 0
              AND NOT EXISTS (
                SELECT 1 FROM unnest(t.tags) x WHERE LOWER(x) = ANY(%s)
              )
            ORDER BY t.date DESC, ABS(t.amount) DESC
            LIMIT %s
            """,
            [start_date, end_date, couple_names, limit],
        )
        rows = _serialize(cur.fetchall())

    return _ok(
        "list_pending_couple_review",
        rows,
        extra={
            "start_date": str(start_date),
            "end_date": str(end_date),
            "couple_tags_checked": couple_names,
        },
    )


def set_couple_tag(transaction_id: str, owner: str) -> dict:
    """Tag a transaction with one of: 'me' | 'partner' | 'joint'.

    Replaces any existing couple tags but preserves other tags (e.g. trip
    tags). Auto-creates the couple tags in Copilot on first use."""
    if owner not in ("me", "partner", "joint"):
        return _err("set_couple_tag",
                    ValueError("owner must be 'me', 'partner', or 'joint'"))
    try:
        couple_ids = _ensure_couple_tag_ids()
        couple_id_set = set(couple_ids.values())

        with conn() as c, c.cursor() as cur:
            cur.execute(
                "SELECT tag_ids FROM fact_transaction WHERE transaction_id = %s",
                [transaction_id],
            )
            row = cur.fetchone()
        if row is None:
            return _err("set_couple_tag", ValueError(
                f"Transaction {transaction_id} not in local DB. "
                f"Run refresh_data('copilot') first."))
        current_ids = list(row["tag_ids"] or [])

        # Drop any existing couple tags, keep the rest, add the new one.
        other_ids = [i for i in current_ids if i not in couple_id_set]
        new_ids = other_ids + [couple_ids[owner]]
    except Exception as e:
        return _err("set_couple_tag", e)

    # Route through the universal edit so we hit the verified mutation path.
    result = update_transaction(transaction_id, tag_ids=new_ids)
    if not result.get("ok"):
        return result
    rows = result.get("rows") or []
    return _ok(
        "set_couple_tag",
        [{
            "owner": owner,
            "tag_ids": new_ids,
            **(rows[0] if rows else {}),
        }],
    )


def list_account_owners() -> dict:
    """Return the configured account → owner mapping plus all known accounts
    so Claude can spot any that aren't yet assigned."""
    ownership = settings.couple_account_ownership()
    with conn() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT account_id, name, institution, type, currency, is_hidden
            FROM dim_account ORDER BY name
            """
        )
        accounts = cur.fetchall()
    annotated = []
    for a in accounts:
        owner = ownership.get(a["account_id"])
        annotated.append({**_coerce_row(dict(a)), "couple_owner": owner or "unassigned"})
    return _ok(
        "list_account_owners",
        annotated,
        extra={
            "split_me": settings.COUPLE_SPLIT_ME,
            "split_partner": settings.COUPLE_SPLIT_PARTNER,
            "configure_via": ".env COUPLE_ACCOUNTS_ME / COUPLE_ACCOUNTS_PARTNER / COUPLE_ACCOUNTS_JOINT (comma-separated account_ids)",
        },
    )


def _default_window(
    start_date: date | None, end_date: date | None
) -> tuple[date, date]:
    """If a couples-calc caller omits dates, default to the current calendar
    month (1st of this month → today). Set independently so a caller can
    pass just one bound and get a sensible default for the other."""
    from datetime import date as _date

    today = _date.today()
    if end_date is None:
        end_date = today
    if start_date is None:
        start_date = end_date.replace(day=1)
    return start_date, end_date


def compute_couple_balances(
    start_date: date | None = None,
    end_date: date | None = None,
    include_personal: bool = False,
) -> dict:
    """Compute who owes whom for the period. Defaults to the current calendar
    month if dates are omitted.

    Algorithm:
      For every transaction in [start_date, end_date] that has a couple tag:
        - Identify the payer from account ownership (settings.couple_account_ownership).
        - If tag is 'me'/'partner', it's that person's own expense — no debt
          unless paid from the other person's account.
        - If tag is 'joint', split per COUPLE_SPLIT_ME / COUPLE_SPLIT_PARTNER.
          Whoever paid is owed the other person's share.

    Returns a per-account breakdown plus the bottom-line "X owes Y $N" number.
    Excludes transactions on accounts that aren't owner-mapped (logged as
    `unmapped_count`).

    `include_personal=True` includes own-tag transactions (purely informational
    — they don't affect debt unless cross-paid)."""
    couple_names = {
        settings.COUPLE_TAG_ME.lower(): "me",
        settings.COUPLE_TAG_PARTNER.lower(): "partner",
        settings.COUPLE_TAG_JOINT.lower(): "joint",
    }
    start_date, end_date = _default_window(start_date, end_date)
    ownership = settings.couple_account_ownership()
    split_me = float(settings.COUPLE_SPLIT_ME)
    split_partner = float(settings.COUPLE_SPLIT_PARTNER)
    if abs(split_me + split_partner - 1.0) > 0.001:
        return _err("compute_couple_balances",
                    ValueError(f"COUPLE_SPLIT_ME ({split_me}) + COUPLE_SPLIT_PARTNER "
                               f"({split_partner}) must sum to 1.0"))

    # Tags now live in fact_transaction.tags (TEXT[]) so this is a single SQL
    # roundtrip — no per-row Copilot fetches needed.
    with conn() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT transaction_id, date, amount, merchant, account_id, tags
            FROM fact_transaction
            WHERE date BETWEEN %s AND %s
              AND NOT is_excluded
              AND amount > 0
            ORDER BY date
            """,
            [start_date, end_date],
        )
        txns = cur.fetchall()

    me_owes_partner = 0.0  # positive means me owes partner
    by_account: dict[str, dict] = {}
    unmapped_count = 0
    skipped_no_tag = 0
    breakdown: list[dict] = []

    for t in txns:
        tag_names = [(x or "").lower() for x in (t.get("tags") or [])]
        role = next((couple_names[n] for n in tag_names if n in couple_names), None)
        if role is None:
            skipped_no_tag += 1
            continue

        payer = ownership.get(t["account_id"])
        if payer is None:
            unmapped_count += 1
            continue

        amount = float(t["amount"])
        if role == "joint":
            # Configured share owed by the non-payer.
            if payer == "me":
                delta = amount * split_partner   # partner owes me
                me_owes_partner -= delta
            elif payer == "partner":
                delta = amount * split_me        # me owes partner
                me_owes_partner += delta
            else:  # joint account paid for joint expense — already shared
                delta = 0.0
            breakdown.append({
                "transaction_id": t["transaction_id"],
                "date": str(t["date"]),
                "merchant": t["merchant"],
                "amount": amount,
                "tag": "joint",
                "payer": payer,
                "delta_me_owes_partner": round(delta if payer == "partner" else -delta, 2),
            })
        elif role == "me" and payer == "partner":
            me_owes_partner += amount
            breakdown.append({**_brkdown(t, role, payer), "delta_me_owes_partner": amount})
        elif role == "partner" and payer == "me":
            me_owes_partner -= amount
            breakdown.append({**_brkdown(t, role, payer), "delta_me_owes_partner": -amount})
        elif include_personal:
            breakdown.append({**_brkdown(t, role, payer), "delta_me_owes_partner": 0.0})

        acc_key = t["account_id"]
        by_account.setdefault(acc_key, {"account_id": acc_key, "joint_total": 0.0,
                                        "personal_total": 0.0, "txn_count": 0})
        by_account[acc_key]["txn_count"] += 1
        if role == "joint":
            by_account[acc_key]["joint_total"] += amount
        else:
            by_account[acc_key]["personal_total"] += amount

    # Pretty bottom line.
    if me_owes_partner > 0.005:
        summary = f"You owe partner ${round(me_owes_partner, 2)}"
    elif me_owes_partner < -0.005:
        summary = f"Partner owes you ${round(-me_owes_partner, 2)}"
    else:
        summary = "Even"

    return _ok(
        "compute_couple_balances",
        breakdown,
        extra={
            "summary": summary,
            "me_owes_partner_net": round(me_owes_partner, 2),
            "by_account": list(by_account.values()),
            "split": {"me": split_me, "partner": split_partner},
            "skipped_no_couple_tag": skipped_no_tag,
            "skipped_unmapped_account": unmapped_count,
            "window": {"start": str(start_date), "end": str(end_date)},
        },
    )


def compute_couple_owed(
    start_date: date | None = None,
    end_date: date | None = None,
    account_ids: list[str] | None = None,
    account_names: list[str] | None = None,
    split_me: float | None = None,
    split_partner: float | None = None,
    joint_only: bool = False,
    flag_duplicate_pending: bool = True,
    include_payments: bool = True,
    refresh_if_stale_minutes: int | None = 30,
) -> dict:
    """One-shot couples calc for a specific set of cards.

    Defaults to the current calendar month if dates are omitted. Built for
    the common conversation: "calculate what me and my wife owe on our joint
    Chase card and Amazon card this month, split joint-tagged transactions
    65/35." Avoids the previous round-trip-per-tool, raw-fetch, Python-dedupe
    path.

    Charges (amount > 0) accrue debt per their couple tag. Payments (amount < 0)
    on the same accounts CREDIT the relevant pool per their tag — so a payment
    you make tagged 'me' reduces what you owe; a payment tagged 'joint' reduces
    the joint pool (and thus both shares per the configured split).

    Filters:
        account_ids        Exact dim_account.account_id values to include.
        account_names      ILIKE substrings against dim_account.name. Either
                           account filter is required (refusing to bill the
                           whole portfolio).
        split_me           Override settings.COUPLE_SPLIT_ME for this call.
        split_partner      Override settings.COUPLE_SPLIT_PARTNER for this call.
        joint_only         If true, only joint-tagged charges count; untagged
                           rows are surfaced separately for review (payments
                           still apply per tag).
        flag_duplicate_pending
                           Pending row that matches a posted row on
                           (date, amount, merchant) is dropped.
        include_payments   If true (default), pull in negative-amount rows
                           and apply credit per tag. Untagged payments go to
                           needs_review and are NOT auto-applied.
        refresh_if_stale_minutes
                           If the most recent successful Copilot ingest is
                           older than this, pull a fresh sync before reading
                           tags. Set to None to never auto-refresh. Default
                           30 — covers the common 'I just tagged some txns
                           and want the new totals' flow.

    Returns per-person owed totals on each account, the bottom-line net, the
    list of transactions used in the math, and a `needs_review` list of
    untagged charges Claude can ask the user about.
    """
    if not account_ids and not account_names:
        return _err(
            "compute_couple_owed",
            ValueError(
                "Pass at least one of account_ids or account_names so we know "
                "which cards to consider."
            ),
        )

    start_date, end_date = _default_window(start_date, end_date)
    sm = float(split_me if split_me is not None else settings.COUPLE_SPLIT_ME)
    sp = float(split_partner if split_partner is not None else settings.COUPLE_SPLIT_PARTNER)
    if abs(sm + sp - 1.0) > 0.001:
        return _err(
            "compute_couple_owed",
            ValueError(f"split_me ({sm}) + split_partner ({sp}) must sum to 1.0"),
        )

    # Auto-refresh if the local mirror is stale. The 4-hour cron means tags
    # added in the last few hours often haven't replicated yet — and the
    # most common reason someone runs this tool is right after tagging.
    refresh_info: dict[str, Any] = {"refreshed": False}
    if refresh_if_stale_minutes is not None:
        with conn() as c, c.cursor() as cur:
            cur.execute(
                """
                SELECT MAX(started_at) AS last_success
                FROM ingestion_runs
                WHERE source = 'copilot' AND status = 'success'
                """
            )
            row = cur.fetchone()
        last_success = row["last_success"] if row else None
        from datetime import datetime as _dt
        from datetime import timedelta as _td
        threshold = _dt.now(UTC) - _td(minutes=refresh_if_stale_minutes)
        if last_success is None or last_success < threshold:
            try:
                copilot_ingest.run_all()
                refresh_info = {
                    "refreshed": True,
                    "reason": (
                        f"copilot last_success {last_success.isoformat() if last_success else 'never'} "
                        f"older than {refresh_if_stale_minutes}min threshold"
                    ),
                }
                log.info("compute_couple_owed.auto_refreshed",
                         last_success=str(last_success),
                         threshold_minutes=refresh_if_stale_minutes)
            except Exception as e:
                refresh_info = {"refreshed": False, "refresh_failed": str(e)}
                log.warning("compute_couple_owed.refresh_failed", error=str(e))

    couple_names = {
        settings.COUPLE_TAG_ME.lower(): "me",
        settings.COUPLE_TAG_PARTNER.lower(): "partner",
        settings.COUPLE_TAG_JOINT.lower(): "joint",
    }

    # Pull both sides (charges and payments) in one query; partition in code.
    amount_clause = "t.amount <> 0" if include_payments else "t.amount > 0"

    where = ["t.date BETWEEN %s AND %s", "NOT t.is_excluded", amount_clause]
    params: list = [start_date, end_date]
    acct_where: list[str] = []
    if account_ids:
        acct_where.append("t.account_id = ANY(%s)")
        params.append(list(account_ids))
    if account_names:
        clauses = []
        for n in account_names:
            clauses.append("a.name ILIKE %s")
            params.append(f"%{n}%")
        acct_where.append("(" + " OR ".join(clauses) + ")")
    where.append("(" + " OR ".join(acct_where) + ")")

    q = f"""
        SELECT t.transaction_id, t.date, t.amount, t.merchant, t.is_pending,
               t.tags, t.account_id, a.name AS account
        FROM fact_transaction t
        LEFT JOIN dim_account a ON a.account_id = t.account_id
        WHERE {" AND ".join(where)}
        ORDER BY t.date, t.amount DESC
    """
    with conn() as c, c.cursor() as cur:
        cur.execute(q, params)
        rows = cur.fetchall()

    # Dedup: a pending row that matches a posted row on (date, amount,
    # merchant) is the same transaction. Applies to charges and payments
    # equally.
    seen_posted: set[tuple] = set()
    if flag_duplicate_pending:
        for r in rows:
            if not r["is_pending"]:
                seen_posted.add((str(r["date"]), float(r["amount"]), (r["merchant"] or "")))

    per_account: dict[str, dict] = {}
    needs_review: list[dict] = []
    used: list[dict] = []
    me_total = 0.0
    partner_total = 0.0
    duplicate_skipped: list[dict] = []
    payment_credits = {"me": 0.0, "partner": 0.0, "joint": 0.0, "untagged": 0.0}

    for r in rows:
        key = (str(r["date"]), float(r["amount"]), (r["merchant"] or ""))
        if flag_duplicate_pending and r["is_pending"] and key in seen_posted:
            duplicate_skipped.append({
                "transaction_id": r["transaction_id"],
                "date": str(r["date"]),
                "merchant": r["merchant"],
                "amount": float(r["amount"]),
                "reason": "pending duplicate of posted",
            })
            continue

        tag_names = [(x or "").lower() for x in (r.get("tags") or [])]
        role = next((couple_names[n] for n in tag_names if n in couple_names), None)

        amount = float(r["amount"])
        is_payment = amount < 0
        abs_amt = abs(amount)
        acct = r["account"] or r["account_id"] or "unknown"
        bucket = per_account.setdefault(
            acct,
            {"account": acct, "account_id": r["account_id"],
             "me_owes": 0.0, "partner_owes": 0.0, "joint_owes": 0.0,
             "me_paid": 0.0, "partner_paid": 0.0, "joint_paid": 0.0,
             "untagged": 0.0, "txn_count": 0},
        )
        bucket["txn_count"] += 1

        # ----- payments path -----
        if is_payment:
            if role == "me":
                me_total -= abs_amt
                bucket["me_paid"] += abs_amt
                payment_credits["me"] += abs_amt
                used.append({**_brkdown(r, "me-payment", "n/a"),
                             "me_share": -abs_amt, "partner_share": 0.0,
                             "kind": "payment"})
            elif role == "partner":
                partner_total -= abs_amt
                bucket["partner_paid"] += abs_amt
                payment_credits["partner"] += abs_amt
                used.append({**_brkdown(r, "partner-payment", "n/a"),
                             "me_share": 0.0, "partner_share": -abs_amt,
                             "kind": "payment"})
            elif role == "joint":
                # A joint-tagged payment reduces the joint pool. Both people
                # get credited per the configured split — symmetric to how a
                # joint charge is debited.
                ms = round(abs_amt * sm, 2)
                ps = round(abs_amt * sp, 2)
                me_total -= ms
                partner_total -= ps
                bucket["joint_paid"] += abs_amt
                payment_credits["joint"] += abs_amt
                used.append({**_brkdown(r, "joint-payment", "n/a"),
                             "me_share": -ms, "partner_share": -ps,
                             "kind": "payment"})
            else:
                # Untagged payment. Don't auto-apply — payments are
                # high-value rows and the user almost always wants to confirm.
                payment_credits["untagged"] += abs_amt
                needs_review.append({
                    "transaction_id": r["transaction_id"],
                    "date": str(r["date"]),
                    "merchant": r["merchant"],
                    "amount": amount,
                    "account": acct,
                    "kind": "payment",
                    "note": "untagged payment — not credited until tagged",
                })
            continue

        # ----- charges path -----
        if role == "me":
            me_total += amount
            bucket["me_owes"] += amount
            used.append({**_brkdown(r, "me", "n/a"),
                         "me_share": amount, "partner_share": 0.0,
                         "kind": "charge"})
        elif role == "partner":
            partner_total += amount
            bucket["partner_owes"] += amount
            used.append({**_brkdown(r, "partner", "n/a"),
                         "me_share": 0.0, "partner_share": amount,
                         "kind": "charge"})
        elif role == "joint":
            ms = round(amount * sm, 2)
            ps = round(amount * sp, 2)
            me_total += ms
            partner_total += ps
            bucket["joint_owes"] += amount
            used.append({**_brkdown(r, "joint", "n/a"),
                         "me_share": ms, "partner_share": ps,
                         "kind": "charge"})
        else:
            bucket["untagged"] += amount
            entry = {
                "transaction_id": r["transaction_id"],
                "date": str(r["date"]),
                "merchant": r["merchant"],
                "amount": amount,
                "account": acct,
                "kind": "charge",
            }
            needs_review.append(entry)
            if not joint_only:
                ms = round(amount * sm, 2)
                ps = round(amount * sp, 2)
                me_total += ms
                partner_total += ps
                used.append({**entry, "tag": "untagged_assumed_joint",
                             "me_share": ms, "partner_share": ps})

    summary_parts = [
        f"You owe ${round(me_total, 2)}",
        f"partner owes ${round(partner_total, 2)}",
        f"joint split {int(sm*100)}/{int(sp*100)}",
    ]
    if include_payments and any(payment_credits[k] for k in ("me", "partner", "joint")):
        summary_parts.append(
            f"payments credited: me ${round(payment_credits['me'], 2)}, "
            f"partner ${round(payment_credits['partner'], 2)}, "
            f"joint ${round(payment_credits['joint'], 2)}"
        )
    summary = "; ".join(summary_parts) + "."

    return _ok(
        "compute_couple_owed",
        used,
        extra={
            "summary": summary,
            "me_owes_total": round(me_total, 2),
            "partner_owes_total": round(partner_total, 2),
            "by_account": list(per_account.values()),
            "needs_review_count": len(needs_review),
            "needs_review": needs_review[:50],
            "duplicate_pending_skipped": duplicate_skipped,
            "payment_credits": {k: round(v, 2) for k, v in payment_credits.items()},
            "split": {"me": sm, "partner": sp},
            "window": {"start": str(start_date), "end": str(end_date)},
            "joint_only": joint_only,
            "include_payments": include_payments,
            "auto_refresh": refresh_info,
        },
    )


# ---- helpers ---------------------------------------------------------------
def _coerce_row(row: dict | None) -> dict | None:
    if row is None:
        return None
    return _serialize([row])[0]


def _brkdown(t: dict, role: str, payer: str) -> dict:
    return {
        "transaction_id": t["transaction_id"],
        "date": str(t["date"]),
        "merchant": t["merchant"],
        "amount": float(t["amount"]),
        "tag": role,
        "payer": payer,
    }
