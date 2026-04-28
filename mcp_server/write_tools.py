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

from datetime import date
from typing import Any

from psycopg import sql

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
    valid = {"all", "whoop", "calendar", "cronometer", "copilot", "mart"}
    if source not in valid:
        return _err("refresh_data", ValueError(f"source must be one of {sorted(valid)}"))

    out: dict[str, Any] = {}
    targets = ("whoop", "calendar", "cronometer", "copilot") if source == "all" else (source,)

    if source != "mart":
        for name in targets:
            try:
                if name == "whoop":
                    from ingest_whoop import ingest as whoop_ingest
                    out[name] = whoop_ingest.run_all()
                elif name == "calendar":
                    from ingest_calendar import ingest as calendar_ingest
                    out[name] = calendar_ingest.run_all()
                elif name == "cronometer":
                    from ingest_cronometer import ingest as cron_ingest
                    out[name] = cron_ingest.run_all()
                elif name == "copilot":
                    out[name] = copilot_ingest.run_all()
            except Exception as e:  # noqa: BLE001
                out[name] = f"FAILED: {type(e).__name__}: {e}"
                log.exception("refresh_data.source_failed", source=name)

    # Always rebuild mart unless caller explicitly wants only one source's
    # raw refresh (in which case we still rebuild — it's cheap and keeps
    # consistency).
    try:
        from mart_refresh.refresh import refresh_all as mart_refresh_all
        out["mart"] = mart_refresh_all()
    except Exception as e:  # noqa: BLE001
        out["mart"] = f"FAILED: {type(e).__name__}: {e}"
        log.exception("refresh_data.mart_failed")

    return _ok("refresh_data", [out])


# ---- generic Copilot writes -------------------------------------------------
def update_transaction_category(transaction_id: str, category_id: str) -> dict:
    """Reassign a transaction's category. Pass empty string to uncategorize."""
    try:
        with GraphQLClient() as client:
            updated = client.update_transaction(transaction_id, category_id=category_id)
        local = copilot_ingest.refresh_one_transaction(transaction_id)
    except Exception as e:  # noqa: BLE001
        return _err("update_transaction_category", e)
    return _ok(
        "update_transaction_category",
        [{"copilot_response": updated, "local_after_refresh": _coerce_row(local)}],
    )


def update_transaction_notes(transaction_id: str, notes: str) -> dict:
    """Set the userNotes on a transaction. Pass '' to clear."""
    try:
        with GraphQLClient() as client:
            updated = client.update_transaction(transaction_id, user_notes=notes)
        local = copilot_ingest.refresh_one_transaction(transaction_id)
    except Exception as e:  # noqa: BLE001
        return _err("update_transaction_notes", e)
    return _ok(
        "update_transaction_notes",
        [{"copilot_response": updated, "local_after_refresh": _coerce_row(local)}],
    )


def list_tags() -> dict:
    """All tags currently defined in Copilot. Call before create_tag to avoid
    duplicates and before tag_transaction so you know the IDs."""
    try:
        with GraphQLClient() as client:
            tags = client.tags()
    except Exception as e:  # noqa: BLE001
        return _err("list_tags", e)
    return _ok("list_tags", tags)


def create_tag(name: str, color_name: str | None = None) -> dict:
    """Create a new tag. Color names accepted by Copilot include red, orange,
    yellow, green, blue, purple, pink, gray (server validates)."""
    try:
        with GraphQLClient() as client:
            tag = client.create_tag(name, color_name=color_name)
    except Exception as e:  # noqa: BLE001
        return _err("create_tag", e)
    return _ok("create_tag", [tag])


def tag_transaction(transaction_id: str, tag_ids: list[str]) -> dict:
    """REPLACE the transaction's tag set with the given IDs. To add or remove
    a single tag, fetch current tags first via get_transactions/get_transaction
    and merge client-side."""
    try:
        with GraphQLClient() as client:
            updated = client.tag_transaction(transaction_id, tag_ids)
        local = copilot_ingest.refresh_one_transaction(transaction_id)
    except Exception as e:  # noqa: BLE001
        return _err("tag_transaction", e)
    return _ok(
        "tag_transaction",
        [{"copilot_response": updated, "local_after_refresh": _coerce_row(local)}],
    )


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
    from datetime import date as _date, timedelta

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

        # Read current tag_ids from the local fact row first (fast, no API
        # round-trip). If the row isn't local yet, fall back to fetching
        # from Copilot.
        with conn() as c, c.cursor() as cur:
            cur.execute(
                "SELECT tag_ids FROM fact_transaction WHERE transaction_id = %s",
                [transaction_id],
            )
            row = cur.fetchone()
        if row is not None:
            current_ids = list(row["tag_ids"] or [])
        else:
            with GraphQLClient() as client:
                txn = client.transaction(transaction_id)
            if txn is None:
                return _err("set_couple_tag", ValueError("transaction not found"))
            current_ids = [t["id"] for t in (txn.get("tags") or [])]

        # Drop any existing couple tags, keep the rest, add the new one.
        other_ids = [i for i in current_ids if i not in couple_id_set]
        new_ids = other_ids + [couple_ids[owner]]

        with GraphQLClient() as client:
            updated = client.tag_transaction(transaction_id, new_ids)
        local = copilot_ingest.refresh_one_transaction(transaction_id)
    except Exception as e:  # noqa: BLE001
        return _err("set_couple_tag", e)
    return _ok("set_couple_tag",
               [{"owner": owner, "tag_ids": new_ids,
                 "copilot_response": updated,
                 "local_after_refresh": _coerce_row(local)}])


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


def compute_couple_balances(
    start_date: date,
    end_date: date,
    include_personal: bool = False,
) -> dict:
    """Compute who owes whom for the period.

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
