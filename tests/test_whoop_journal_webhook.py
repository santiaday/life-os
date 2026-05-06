"""Tests for the iPhone refresh-callback webhook.

Three branches we care about:
  - 401 when X-Shared-Secret is missing or wrong
  - 400 when access_token / id_token aren't JWT-shaped
  - 200 + token persisted when the body is valid
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ingest_whoop_journal.refresh_webhook import router as whoop_router

VALID_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJzYW50aSJ9." + "x" * 60
VALID_REFRESH = "r" * 80


@pytest.fixture
def app() -> FastAPI:
    app = FastAPI()
    app.include_router(whoop_router)
    return app


@pytest.fixture
def client(app, monkeypatch) -> TestClient:
    """TestClient with the shared secret set in env so 503 doesn't fire."""
    monkeypatch.setenv("WHOOP_REFRESH_WEBHOOK_SECRET", "test-secret")
    return TestClient(app)


def _saved_calls():
    """Capture for what auth.save_tokens received."""
    return {"calls": []}


@pytest.fixture
def captured_save_tokens():
    """Patch auth.save_tokens for the duration of the test."""
    captured = _saved_calls()

    def fake(**kw):
        captured["calls"].append(kw)

    with patch("ingest_whoop_journal.refresh_webhook.auth.save_tokens", side_effect=fake):
        yield captured


# ---- 401: bad / missing secret -------------------------------------------
def test_returns_401_without_shared_secret(client):
    resp = client.post(
        "/lifelog/whoop/refresh-callback",
        json={"access_token": VALID_JWT, "refresh_token": VALID_REFRESH},
    )
    assert resp.status_code == 401


def test_returns_401_with_wrong_shared_secret(client):
    resp = client.post(
        "/lifelog/whoop/refresh-callback",
        headers={"X-Shared-Secret": "obviously-wrong"},
        json={"access_token": VALID_JWT, "refresh_token": VALID_REFRESH},
    )
    assert resp.status_code == 401


def test_returns_503_when_secret_unconfigured(app, monkeypatch):
    """If WHOOP_REFRESH_WEBHOOK_SECRET isn't set on the server we return 503,
    not 200 — better to be loud than silently accept anything."""
    monkeypatch.delenv("WHOOP_REFRESH_WEBHOOK_SECRET", raising=False)
    c = TestClient(app)
    resp = c.post(
        "/lifelog/whoop/refresh-callback",
        headers={"X-Shared-Secret": "anything"},
        json={"access_token": VALID_JWT, "refresh_token": VALID_REFRESH},
    )
    assert resp.status_code == 503


# ---- 400: malformed body --------------------------------------------------
def test_returns_400_when_access_token_is_not_jwt(client, captured_save_tokens):
    resp = client.post(
        "/lifelog/whoop/refresh-callback",
        headers={"X-Shared-Secret": "test-secret"},
        json={
            "access_token": "eyJhbGc-no-dots-or-second-part",
            "refresh_token": VALID_REFRESH,
        },
    )
    assert resp.status_code == 400
    assert "JWT" in resp.json()["detail"]
    assert captured_save_tokens["calls"] == []


def test_returns_400_when_id_token_is_not_jwt(client, captured_save_tokens):
    resp = client.post(
        "/lifelog/whoop/refresh-callback",
        headers={"X-Shared-Secret": "test-secret"},
        json={
            "access_token": VALID_JWT,
            "refresh_token": VALID_REFRESH,
            "id_token": "not-jwt-shape",
        },
    )
    assert resp.status_code == 400
    assert captured_save_tokens["calls"] == []


def test_returns_422_when_access_token_too_short(client):
    resp = client.post(
        "/lifelog/whoop/refresh-callback",
        headers={"X-Shared-Secret": "test-secret"},
        json={"access_token": "short", "refresh_token": VALID_REFRESH},
    )
    # FastAPI/pydantic emits 422 for min_length violations before our handler.
    assert resp.status_code == 422


# ---- 200: happy path ------------------------------------------------------
def test_returns_200_and_persists_with_valid_input(client, captured_save_tokens):
    resp = client.post(
        "/lifelog/whoop/refresh-callback",
        headers={"X-Shared-Secret": "test-secret"},
        json={
            "access_token": VALID_JWT,
            "refresh_token": VALID_REFRESH,
            "id_token": VALID_JWT,
            "expires_in": 3600,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["expires_at"]

    assert len(captured_save_tokens["calls"]) == 1
    saved = captured_save_tokens["calls"][0]
    assert saved["access_token"] == VALID_JWT
    assert saved["refresh_token"] == VALID_REFRESH
    assert saved["id_token"] == VALID_JWT
    assert saved["metadata"]["source"] == "ios_shortcut_refresh"


def test_returns_200_when_id_token_omitted(client, captured_save_tokens):
    """id_token is optional — Whoop's iOS app sometimes ships RefreshToken
    flows without one."""
    resp = client.post(
        "/lifelog/whoop/refresh-callback",
        headers={"X-Shared-Secret": "test-secret"},
        json={"access_token": VALID_JWT, "refresh_token": VALID_REFRESH},
    )
    assert resp.status_code == 200
    saved = captured_save_tokens["calls"][0]
    assert saved["id_token"] is None
