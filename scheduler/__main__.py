"""APScheduler service.

Long-running blocking process. Each cron job shells out to the corresponding
ingester via subprocess (`python -m ingest_<source> ingest [...]`) so a
crash in one job can't bring down the scheduler. After a successful run, the
job optionally chains to `mart_refresh` so the mart layer reflects the new
data without waiting for the nightly rebuild.

Per SPEC.md §8.4. Jobs land alongside their ingesters as phases ship.
"""

from __future__ import annotations

import subprocess
import sys

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from lifeos_core.logging import configure_logging, get_logger
from lifeos_core.settings import settings

log = get_logger(__name__)


def run_subprocess(module: str, *args: str, chain_mart: bool = True) -> None:
    """Invoke `python -m <module> <args>`. On success, optionally fire
    `python -m mart_refresh` so the mart layer updates eagerly."""
    cmd = [sys.executable, "-m", module, *args]
    log.info("scheduler.job.start", cmd=cmd)
    rc = subprocess.run(cmd, check=False)
    log.info(
        "scheduler.job.end",
        cmd=cmd,
        returncode=rc.returncode,
        status="success" if rc.returncode == 0 else "failure",
    )
    if rc.returncode == 0 and chain_mart:
        # Mart refresh is in Phase 4 — guarded so missing module doesn't crash.
        try:
            mart = subprocess.run(
                [sys.executable, "-m", "mart_refresh"], check=False
            )
            log.info("scheduler.mart_refresh.end", returncode=mart.returncode)
        except FileNotFoundError:
            pass


def build() -> BlockingScheduler:
    sched = BlockingScheduler(timezone=settings.LOCAL_TZ)

    # ---- Whoop (Phase 2) ---------------------------------------------------
    sched.add_job(
        run_subprocess,
        CronTrigger(minute=15),
        args=["ingest_whoop", "ingest"],
        id="whoop_hourly",
        name="Whoop hourly incremental",
        max_instances=1,
        coalesce=True,
    )
    sched.add_job(
        run_subprocess,
        CronTrigger(hour=6, minute=0),
        args=["ingest_whoop", "ingest", "--backfill", "3"],
        id="whoop_daily_backfill",
        name="Whoop daily 3-day re-pull (Whoop back-edits older recoveries)",
        max_instances=1,
        coalesce=True,
    )

    # ---- Whoop journal (iPhone-bridge architecture) ------------------------
    # iPhone Shortcut runs at 5:30 AM, POSTs fresh tokens to the webhook.
    # We pull at 5:35 (5 min later) so the token row is fresh by then.
    # Default mode is the 2-day rolling window (today + yesterday).
    sched.add_job(
        run_subprocess,
        CronTrigger(hour=5, minute=35),
        args=["ingest_whoop_journal"],
        id="whoop_journal_daily",
        name="Whoop journal 2-day rebackfill",
        max_instances=1,
        coalesce=True,
    )
    # Sunday: deeper backfill (catches late journal edits within 7 days) +
    # behavior-catalog refresh.
    sched.add_job(
        run_subprocess,
        CronTrigger(day_of_week="sun", hour=5, minute=40),
        args=["ingest_whoop_journal", "--backfill", "7"],
        id="whoop_journal_weekly_backfill",
        name="Whoop journal Sunday 7-day rebackfill",
        max_instances=1,
        coalesce=True,
    )
    sched.add_job(
        run_subprocess,
        CronTrigger(day_of_week="sun", hour=5, minute=45),
        args=["ingest_whoop_journal", "--data-type", "catalog"],
        id="whoop_journal_catalog_weekly",
        name="Whoop behavior catalog weekly refresh",
        max_instances=1,
        coalesce=True,
    )

    # ---- Calendar (Phase 3) ------------------------------------------------
    sched.add_job(
        run_subprocess,
        CronTrigger(minute="*/30"),
        args=["ingest_calendar", "ingest"],
        id="calendar_30min",
        name="Google Calendar incremental sync (every 30 min)",
        max_instances=1,
        coalesce=True,
    )

    # ---- Lifelog calendar publisher ----------------------------------------
    # Pushes events table rows (Whoop sleep/workout, ActivityWatch work
    # blocks) out to the dedicated lifelog Google calendars. Doesn't touch
    # the mart layer, so chain_mart=False keeps mart_refresh from firing
    # on every tick.
    sched.add_job(
        run_subprocess,
        CronTrigger(minute="*/15"),
        args=["calendar_sync", "sync"],
        kwargs={"chain_mart": False},
        id="calendar_sync_15min",
        name="Lifelog events → Google Calendar push (every 15 min)",
        max_instances=1,
        coalesce=True,
    )

    # ---- Cronometer (Phase 6) ---------------------------------------------
    sched.add_job(
        run_subprocess,
        CronTrigger(hour=3, minute=0),
        args=["ingest_cronometer", "ingest", "--backfill", "2"],
        id="cronometer_daily",
        name="Cronometer daily 2-day rebackfill (catches late edits)",
        max_instances=1,
        coalesce=True,
    )
    sched.add_job(
        run_subprocess,
        CronTrigger(day_of_week="sun", hour=3, minute=30),
        args=["ingest_cronometer", "ingest", "--backfill", "14"],
        id="cronometer_weekly",
        name="Cronometer Sunday 14-day rebackfill (catches late corrections)",
        max_instances=1,
        coalesce=True,
    )

    # ---- Copilot (Phase 7) ------------------------------------------------
    sched.add_job(
        run_subprocess,
        CronTrigger(minute=0, hour="*/4"),
        args=["ingest_copilot", "ingest"],
        id="copilot_4hr",
        name="Copilot 4-hourly transaction sync (35-day window)",
        max_instances=1,
        coalesce=True,
    )
    sched.add_job(
        run_subprocess,
        CronTrigger(hour=4, minute=0),
        args=["ingest_copilot", "ingest", "--backfill", "1825"],
        id="copilot_nightly",
        name="Copilot nightly 5-year refresh",
        max_instances=1,
        coalesce=True,
    )

    # ---- Nightly mart rebuild (Phase 4) ------------------------------------
    sched.add_job(
        run_subprocess,
        CronTrigger(hour=4, minute=30),
        args=["mart_refresh"],
        kwargs={"chain_mart": False},
        id="mart_nightly",
        name="Nightly mart rebuild",
        max_instances=1,
        coalesce=True,
    )

    # ---- Stale-ingest alerting (Phase 8) -----------------------------------
    sched.add_job(
        _alert_check,
        CronTrigger(minute=45),
        id="alert_hourly",
        name="Hourly stale-ingest survey + alert",
        max_instances=1,
        coalesce=True,
    )

    # ---- Lifelog stale-session closer --------------------------------------
    # iOS Live Activities die after 8h. If the user forgets to end the session
    # (or the device went offline), the open ios_manual row will sit forever.
    # Every 30 min, close any open session older than 12h with an estimated
    # 4h end. See lifelog_api.service.close_stale_events.
    sched.add_job(
        _lifelog_stale_close,
        CronTrigger(minute="*/30"),
        id="lifelog_stale_close",
        name="Lifelog: auto-close stale ios_manual sessions",
        max_instances=1,
        coalesce=True,
    )

    return sched


def _alert_check() -> None:
    """In-process alert check (no subprocess — it's a few SQL queries)."""
    from lifeos_core.alerts import check_and_alert

    result = check_and_alert()
    log.info("scheduler.alert_check", result=result)


def _lifelog_stale_close() -> None:
    """In-process: close any ios_manual sessions open >12h. Same shape as
    _alert_check — a single SQL UPDATE, not worth the subprocess overhead."""
    try:
        from lifelog_api.service import close_stale_events
    except ImportError:  # pragma: no cover - module optional
        return
    closed = close_stale_events()
    if closed:
        log.warning("scheduler.lifelog_stale_close", closed=closed)
    else:
        log.info("scheduler.lifelog_stale_close", closed=0)


def main() -> int:
    configure_logging()
    sched = build()
    log.info(
        "scheduler.starting",
        tz=settings.LOCAL_TZ,
        jobs=[j.id for j in sched.get_jobs()],
    )
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("scheduler.stopping")
    return 0


if __name__ == "__main__":
    sys.exit(main())
