"""Tests for the read-only WhoopAuth helper.

WhoopAuth has no refresh logic by design (the iPhone Shortcut owns refresh),
so the contract is narrow: load tokens from oauth_tokens, hand them out
until they're stale, then raise WhoopAuthExpired loudly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ingest_whoop_journal import auth


def _row(*, access: str | None = "tok-" + "a" * 60, expires_in_min: int = 60) -> dict:
    return {
        "access_token": access,
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=expires_in_min),
    }


class _Cursor:
    def __init__(self, row): self._row = row
    def __enter__(self): return self
    def __exit__(self, *exc): pass
    def execute(self, *_a, **_k): pass
    def fetchone(self): return self._row


class _Conn:
    def __init__(self, row): self._row = row
    def cursor(self): return _Cursor(self._row)


class _TxCM:
    def __init__(self, row): self._row = row
    def __enter__(self): return _Conn(self._row)
    def __exit__(self, *exc): pass


def _patch_tx(monkeypatch, row):
    """Stub lifeos_core.db.tx() + the auth module's tx import to return our row."""
    monkeypatch.setattr(auth, "tx", lambda: _TxCM(row))


# ---- happy path -----------------------------------------------------------
def test_ensure_fresh_returns_token_when_unexpired(monkeypatch):
    _patch_tx(monkeypatch, _row(expires_in_min=60))
    a = auth.WhoopAuth()
    tok = a.ensure_fresh()
    assert tok.startswith("tok-")
    # Headers shape
    h = a.headers()
    assert h["Authorization"] == f"Bearer {tok}"


def test_ensure_fresh_caches_in_memory(monkeypatch):
    """Repeated calls inside a single instance shouldn't re-hit the DB."""
    calls = {"n": 0}

    def factory(_self=None):
        calls["n"] += 1
        return _TxCM(_row(expires_in_min=60))

    monkeypatch.setattr(auth, "tx", lambda: factory())
    a = auth.WhoopAuth()
    a.ensure_fresh()
    a.ensure_fresh()
    a.headers()
    assert calls["n"] == 1, "WhoopAuth should only DB-load once per instance"


# ---- expiry path ----------------------------------------------------------
def test_ensure_fresh_raises_expired_past_expires_at(monkeypatch):
    _patch_tx(monkeypatch, _row(expires_in_min=-30))  # 30 min in the past
    a = auth.WhoopAuth()
    with pytest.raises(auth.WhoopAuthExpired) as ei:
        a.ensure_fresh()
    assert "expired" in str(ei.value).lower()
    # Hint at the fix in the message
    assert "iPhone" in str(ei.value) or "bootstrap" in str(ei.value)


def test_ensure_fresh_raises_expired_within_skew_buffer(monkeypatch):
    """We treat tokens as expired EXPIRY_SKEW (5 min) before nominal expiry."""
    _patch_tx(monkeypatch, _row(expires_in_min=2))  # inside the 5-min skew
    a = auth.WhoopAuth()
    with pytest.raises(auth.WhoopAuthExpired):
        a.ensure_fresh()


def test_ensure_fresh_raises_when_no_row(monkeypatch):
    _patch_tx(monkeypatch, None)
    a = auth.WhoopAuth()
    with pytest.raises(auth.WhoopAuthError) as ei:
        a.ensure_fresh()
    assert "bootstrap" in str(ei.value).lower()


def test_ensure_fresh_raises_when_access_token_blank(monkeypatch):
    _patch_tx(monkeypatch, _row(access=None))
    a = auth.WhoopAuth()
    with pytest.raises(auth.WhoopAuthError):
        a.ensure_fresh()


# ---- WhoopAuthExpired is a WhoopAuthError -------------------------------
def test_expired_is_subclass_of_error():
    """Callers that broad-catch WhoopAuthError still see expired tokens."""
    assert issubclass(auth.WhoopAuthExpired, auth.WhoopAuthError)
