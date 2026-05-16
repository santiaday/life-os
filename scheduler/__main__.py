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


def run_pushpress_coach_pipeline() -> None:
    """Single-shot end-to-end refresh: ingest_pushpress → coach run.

    Used by the daily 4 AM trigger and the Sunday afternoon triple-fire (1/3/5
    PM ET), which exists for the case where the gym publishes the next week's
    programming on Sunday afternoon and we want it parsed + in Hevy before
    the user wakes up Monday. The 4 AM Monday cron would catch it too, but
    the Sunday triple-fire shortens the lag and lets the user preview next
    week's loads on Sunday night."""
    log.info("scheduler.coach_pipeline.start")
    rc = subprocess.run(
        [sys.executable, "-m", "ingest_pushpress", "ingest"], check=False,
    )
    if rc.returncode != 0:
        log.error("scheduler.coach_pipeline.pushpress_failed", returncode=rc.returncode)
        return
    rc = subprocess.run(
        [sys.executable, "-m", "coach", "run"], check=False,
    )
    log.info(
        "scheduler.coach_pipeline.end",
        coach_rc=rc.returncode,
        status="success" if rc.returncode == 0 else "failure",
    )


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

    # ---- Hevy strength training -------------------------------------------
    # Daily incremental at 6 AM (after Whoop's 6:00 backfill so the Whoop ↔
    # Hevy linker can find fact_workout rows from this morning's session).
    sched.add_job(
        run_subprocess,
        CronTrigger(hour=6, minute=10),
        args=["ingest_hevy", "ingest"],
        id="hevy_daily",
        name="Hevy daily incremental (last_run lookback)",
        max_instances=1,
        coalesce=True,
    )
    # Sunday: deeper rebackfill (catches per-set tweaks made hours-to-days
    # later in the Hevy app) + exercise template catalog refresh.
    sched.add_job(
        run_subprocess,
        CronTrigger(day_of_week="sun", hour=6, minute=15),
        args=["ingest_hevy", "ingest", "--backfill", "30", "--catalog"],
        id="hevy_weekly_rebackfill",
        name="Hevy Sunday 30-day rebackfill + exercise catalog",
        max_instances=1,
        coalesce=True,
    )

    # ---- PushPress (programmed gym workouts) ------------------------------
    # Gym typically publishes the next week's programming by 4 PM ET the
    # prior week, so 4 AM the next day gives a comfortable buffer. Window is
    # ±7 days around today — past for late coach edits, future for new
    # programming as soon as it lands.
    sched.add_job(
        run_subprocess,
        CronTrigger(hour=4, minute=0),
        args=["ingest_pushpress", "ingest"],
        kwargs={"chain_mart": False},
        id="pushpress_daily",
        name="PushPress daily ±7-day programmed-workout sync",
        max_instances=1,
        coalesce=True,
    )
    # Coach: parse → normalize → recommend loads → push Hevy routines.
    # Runs 5 min after the PushPress sync so today's programming is fresh.
    # No mart-refresh chain — coach writes don't affect mart_daily.
    sched.add_job(
        run_subprocess,
        CronTrigger(hour=4, minute=5),
        args=["coach", "run"],
        kwargs={"chain_mart": False},
        id="coach_daily",
        name="Coach: parse PushPress + recommend loads + sync Hevy routines",
        max_instances=1,
        coalesce=True,
    )
    # Hourly load recompute — picks up new actuals (PR set last night, RPE
    # change in the latest Hevy session) and updates open future routines
    # so the prescribed weight matches the user's latest training state.
    sched.add_job(
        run_subprocess,
        CronTrigger(minute=35),
        args=["coach", "recompute", "--future", "14"],
        kwargs={"chain_mart": False},
        id="coach_recompute_hourly",
        name="Coach: hourly load recompute (post-Hevy-ingest fresh PRs)",
        max_instances=1,
        coalesce=True,
    )
    # Sunday afternoon triple-fire (1 PM / 3 PM / 5 PM ET). Gym typically
    # publishes the next week's programming on Sunday afternoon; firing at
    # multiple slots minimizes the lag between "coach published" and
    # "user sees recommended loads in Hevy". Each fire is the full chain:
    # ingest_pushpress (pull fresh) → coach run (parse + recompute + Hevy push).
    # max_instances=1 + coalesce=True means an in-flight run won't get
    # stomped if the previous slot hasn't finished.
    for hour in (13, 15, 17):
        sched.add_job(
            run_pushpress_coach_pipeline,
            CronTrigger(day_of_week="sun", hour=hour, minute=0),
            id=f"pushpress_coach_sunday_{hour:02d}",
            name=f"PushPress + coach pipeline (Sunday {hour:02d}:00 ET)",
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

    # ---- Body-image weekly reference validation ---------------------------
    # Runs every reference photo in body_image/calibration/validation/
    # through the live rating pipeline and checks Pearson r against
    # expected scores. Alerts via Pushover if r < 0.7. Skips silently
    # when the validation directory is empty so the cron can land on
    # the droplet before reference photos are sourced.
    sched.add_job(
        _body_image_validation,
        CronTrigger(day_of_week="sun", hour=5, minute=0),
        id="body_image_validation_weekly",
        name="Body-image weekly reference validation",
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


def _body_image_validation() -> None:
    """In-process: weekly Pearson-r check on body-image reference photos.
    Subprocess avoided — body_image.validation already handles its own
    logging + alerting, and we want the result to land in the scheduler's
    structured log stream rather than a child process's stdout."""
    try:
        from body_image.validation import run_weekly_validation
    except ImportError:  # pragma: no cover - module optional
        return
    try:
        result = run_weekly_validation()
        log.info("scheduler.body_image_validation", **result)
    except Exception:
        log.exception("scheduler.body_image_validation_failed")


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
