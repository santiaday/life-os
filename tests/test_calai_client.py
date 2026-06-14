"""Tests for the Cal AI client's capture-independent mechanics:
Firestore value decoding, securetoken response parsing, and the token-freshness
decision. The networked refresh / Firestore query are exercised live once the
follow-up capture provides the API key + refresh token.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ingest_calai import client as C


# ---- Firestore REST decoding ----------------------------------------------
def test_decode_value_primitives():
    assert C._decode_value({"stringValue": "hi"}) == "hi"
    assert C._decode_value({"integerValue": "42"}) == 42
    assert C._decode_value({"doubleValue": 1.5}) == 1.5
    assert C._decode_value({"booleanValue": True}) is True
    assert C._decode_value({"nullValue": None}) is None
    assert C._decode_value({"timestampValue": "2026-06-14T19:30:00Z"}) == "2026-06-14T19:30:00Z"


def test_decode_value_nested():
    v = {"mapValue": {"fields": {
        "name": {"stringValue": "Salmon"},
        "macros": {"mapValue": {"fields": {"kcal": {"integerValue": "752"}}}},
        "tags": {"arrayValue": {"values": [{"stringValue": "fish"}, {"integerValue": "2"}]}},
    }}}
    out = C._decode_value(v)
    assert out == {"name": "Salmon", "macros": {"kcal": 752}, "tags": ["fish", 2]}


def test_decode_document_keeps_path():
    doc = {"name": "projects/calai-app/databases/(default)/documents/foods/abc",
           "fields": {"calories": {"integerValue": "752"}}}
    out = C.decode_document(doc)
    assert out["calories"] == 752
    assert out["_name"].endswith("/foods/abc")


# ---- securetoken response parsing -----------------------------------------
def test_parse_securetoken_snake_and_camel():
    snake = C.parse_securetoken_response(
        {"id_token": "ID", "refresh_token": "RT", "expires_in": "3600", "user_id": "U"})
    assert snake == {"id_token": "ID", "refresh_token": "RT", "expires_in": 3600, "user_id": "U"}
    camel = C.parse_securetoken_response(
        {"idToken": "ID2", "refreshToken": "RT2", "expiresIn": 3600, "userId": "U2"})
    assert camel["id_token"] == "ID2" and camel["refresh_token"] == "RT2"


# ---- token freshness (mocked DB) ------------------------------------------
class _Cur:
    def __init__(self, row): self._row = row
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def execute(self, *a, **k): pass
    def fetchone(self): return self._row


class _Conn:
    def __init__(self, row): self._row = row
    def cursor(self): return _Cur(self._row)
    def commit(self): pass


class _Tx:
    def __init__(self, row): self._row = row
    def __enter__(self): return _Conn(self._row)
    def __exit__(self, *a): pass


def _patch_tx(monkeypatch, row):
    monkeypatch.setattr(C, "tx", lambda: _Tx(row))


def test_fresh_token_returned_without_refresh(monkeypatch):
    row = {"access_token": "ID-fresh", "refresh_token": "RT",
           "expires_at": datetime.now(UTC) + timedelta(hours=1),
           "metadata": {"user_id": "U"}}
    _patch_tx(monkeypatch, row)
    # if it tried to refresh, this would raise (no API key set)
    monkeypatch.setattr(C, "_securetoken_refresh",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not refresh")))
    auth = C.CalaiAuth()
    assert auth.ensure_fresh() == "ID-fresh"
    assert auth.user_id == "U"


def test_stale_token_triggers_refresh(monkeypatch):
    monkeypatch.setenv("CALAI_FIREBASE_API_KEY", "test-key")
    row = {"access_token": "ID-old", "refresh_token": "RT",
           "expires_at": datetime.now(UTC) - timedelta(minutes=1),
           "metadata": {"user_id": "U"}}
    _patch_tx(monkeypatch, row)
    monkeypatch.setattr(C, "_securetoken_refresh",
                        lambda rt, key: {"id_token": "ID-new", "refresh_token": "RT2",
                                         "expires_in": 3600, "user_id": "U"})
    auth = C.CalaiAuth()
    assert auth.ensure_fresh() == "ID-new"


def test_no_refresh_token_raises(monkeypatch):
    row = {"access_token": None, "refresh_token": None,
           "expires_at": None, "metadata": {}}
    _patch_tx(monkeypatch, row)
    auth = C.CalaiAuth()
    try:
        auth.ensure_fresh()
        raise AssertionError("expected CalaiAuthError")
    except C.CalaiAuthError as e:
        assert "refresh token" in str(e)
