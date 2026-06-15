"""Tests for stale-ingest detection.

alerts._stale_sources walks SOURCE_THRESHOLDS, querying ingestion_runs for each
source's last success and flagging the ones older than their threshold. These
tests stub the DB so we can assert the staleness math and — critically — that
the resilience-backbone sources (whoop_private especially) are actually watched.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from lifeos_core import alerts


class _Cursor:
    """Returns a per-source last_success for fetchone(); [] for fetchall()."""

    def __init__(self, last_success_by_source: dict):
        self._map = last_success_by_source
        self._last_source: str | None = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def execute(self, _sql, params=None):
        # _stale_sources passes [source]; _stale_hosts passes nothing.
        self._last_source = params[0] if params else None

    def fetchone(self):
        return {"last_success": self._map.get(self._last_source)}

    def fetchall(self):
        return []  # no host heartbeats in these tests


class _Conn:
    def __init__(self, m):
        self._m = m

    def cursor(self):
        return _Cursor(self._m)


class _TxCM:
    def __init__(self, m):
        self._m = m

    def __enter__(self):
        return _Conn(self._m)

    def __exit__(self, *exc):
        pass


def _patch(monkeypatch, last_success_by_source):
    monkeypatch.setattr(alerts, "tx", lambda: _TxCM(last_success_by_source))


def test_whoop_private_is_monitored():
    """The resilience backbone must be in the threshold list, or it can die silently."""
    monitored = {src for src, _label, _hrs in alerts.SOURCE_THRESHOLDS}
    assert "whoop_private" in monitored
    # whoop_labs is intentionally NOT monitored — native labs run under whoop_private,
    # and no recurring job writes source='whoop_labs' (would alert forever).
    assert "whoop_labs" not in monitored


def test_fresh_source_not_flagged(monkeypatch):
    now = datetime.now(UTC)
    # Every source succeeded one minute ago.
    fresh = {src: now - timedelta(minutes=1) for src, _l, _h in alerts.SOURCE_THRESHOLDS}
    _patch(monkeypatch, fresh)
    assert alerts._stale_sources(now=now) == []


def test_stale_source_flagged_with_hours(monkeypatch):
    now = datetime.now(UTC)
    fresh = {src: now - timedelta(minutes=1) for src, _l, _h in alerts.SOURCE_THRESHOLDS}
    # whoop_private threshold is 30h; make it 40h stale.
    fresh["whoop_private"] = now - timedelta(hours=40)
    _patch(monkeypatch, fresh)
    stale = alerts._stale_sources(now=now)
    assert [s["source"] for s in stale] == ["whoop_private"]
    assert stale[0]["hours_stale"] == 40.0
    assert stale[0]["threshold_hours"] == 30


def test_never_succeeded_flagged(monkeypatch):
    now = datetime.now(UTC)
    fresh = {src: now - timedelta(minutes=1) for src, _l, _h in alerts.SOURCE_THRESHOLDS}
    fresh["whoop_journal"] = None  # no successful run on record
    _patch(monkeypatch, fresh)
    stale = alerts._stale_sources(now=now)
    assert [s["source"] for s in stale] == ["whoop_journal"]
    assert stale[0]["last_success"] is None
    assert stale[0]["hours_stale"] is None


def test_naive_timestamp_treated_as_utc(monkeypatch):
    """ingestion_runs.started_at can come back tz-naive; must not crash."""
    now = datetime.now(UTC)
    fresh = {src: now.replace(tzinfo=None) - timedelta(minutes=1)
             for src, _l, _h in alerts.SOURCE_THRESHOLDS}
    _patch(monkeypatch, fresh)
    assert alerts._stale_sources(now=now) == []
