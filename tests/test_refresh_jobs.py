"""Background refresh jobs.

Regression cover for the 2026-08-04 outage: refresh_data ran inline, a single
'all' call occupied the MCP server for minutes, and every other request —
including `initialize` — queued until Caddy timed it out at 240s. The connector
looked dead rather than busy.

The property that matters is simply that refresh_data RETURNS PROMPTLY. These
tests stub the actual work so they assert scheduling behaviour, not ingestion.
"""

from __future__ import annotations

import time

import pytest

from mcp_server import write_tools as W


@pytest.fixture(autouse=True)
def _clean_registry():
    with W._JOBS_LOCK:
        W._JOBS.clear()
        W._RUNNING.clear()
    yield
    with W._JOBS_LOCK:
        W._JOBS.clear()
        W._RUNNING.clear()


@pytest.fixture
def slow_work(monkeypatch):
    """Stand in for a refresh that takes a while."""
    started = []

    def fake(job_id, source):
        started.append(source)
        time.sleep(0.4)
        W._record(job_id, status="done", stage=None, result={source: "ok"})
        with W._JOBS_LOCK:
            W._RUNNING.discard(source)

    monkeypatch.setattr(W, "_run_refresh", fake)
    return started


def test_refresh_returns_immediately_even_when_work_is_slow(slow_work):
    t0 = time.monotonic()
    res = W.refresh_data("cronometer")
    elapsed = time.monotonic() - t0
    # The whole point: the caller is not held for the duration of the work.
    assert elapsed < 0.2, f"refresh_data blocked for {elapsed:.2f}s"
    row = res["rows"][0]
    assert row["started"] is True
    assert row["job_id"]


def test_status_tracks_the_job_to_completion(slow_work):
    job_id = W.refresh_data("cronometer")["rows"][0]["job_id"]
    assert W.get_refresh_status(job_id)["rows"][0]["status"] == "running"
    for _ in range(40):
        if W.get_refresh_status(job_id)["rows"][0]["status"] != "running":
            break
        time.sleep(0.05)
    done = W.get_refresh_status(job_id)["rows"][0]
    assert done["status"] == "done"
    assert done["result"] == {"cronometer": "ok"}


def test_second_call_for_a_running_source_does_not_start_a_duplicate(slow_work):
    first = W.refresh_data("cronometer")["rows"][0]
    second = W.refresh_data("cronometer")["rows"][0]
    assert second["started"] is False
    assert second["already_running"] is True
    assert second["job_id"] == first["job_id"]
    time.sleep(0.6)
    assert slow_work == ["cronometer"], "work ran twice"


def test_different_sources_run_concurrently(slow_work):
    a = W.refresh_data("cronometer")["rows"][0]
    b = W.refresh_data("copilot")["rows"][0]
    assert a["started"] and b["started"]
    assert a["job_id"] != b["job_id"]


def test_all_schedules_ingesters_then_unify_then_mart(slow_work):
    """Order is load-bearing: unify reads what the ingesters wrote, and the
    mart reads unify's views. Getting it wrong yields a mart built on the
    previous run's data — wrong numbers, no error."""
    legs = W.refresh_data("all")["rows"][0]["legs"]
    assert legs[-1] == "mart", f"mart must run last, got {legs}"
    assert legs[-2] == "unify", f"unify must precede mart, got {legs}"
    assert set(legs[:-2]) == set(W.ALL_INGESTERS)
    assert legs.index("loseit") < legs.index("unify")


def test_single_source_still_rebuilds_the_mart(slow_work):
    """A scoped refresh has to rebuild the mart too, or the new rows are
    invisible to every daily-grain query."""
    legs = W.refresh_data("pushpress")["rows"][0]["legs"]
    assert legs == ["pushpress", "mart"]


def test_unify_alone_skips_the_ingesters(slow_work):
    legs = W.refresh_data("unify")["rows"][0]["legs"]
    assert legs == ["unify", "mart"]


def test_unknown_source_is_rejected_without_scheduling():
    res = W.refresh_data("banana")
    assert res["ok"] is False
    assert "must be one of" in res["error"]
    with W._JOBS_LOCK:
        assert not W._RUNNING


def test_unknown_job_id_errors_cleanly():
    res = W.get_refresh_status("does-not-exist")
    assert res["ok"] is False
    assert "unknown job_id" in res["error"]


def test_registry_is_bounded(slow_work):
    for i in range(W._MAX_JOBS + 8):
        W._record(f"job{i}", source="x", status="done",
                  started_at=f"2026-08-04T00:00:{i:02d}")
    with W._JOBS_LOCK:
        assert len(W._JOBS) <= W._MAX_JOBS


def test_a_crashing_job_releases_the_source_lock(monkeypatch):
    def boom(job_id, source):
        try:
            raise RuntimeError("kaboom")
        except Exception as e:
            W._record(job_id, status="failed", error=str(e))
        finally:
            with W._JOBS_LOCK:
                W._RUNNING.discard(source)

    monkeypatch.setattr(W, "_run_refresh", boom)
    W.refresh_data("copilot")
    time.sleep(0.2)
    # A wedged source must not block refreshes forever.
    with W._JOBS_LOCK:
        assert "copilot" not in W._RUNNING
    assert W.refresh_data("copilot")["rows"][0]["started"] is True
