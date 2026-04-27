"""Tests for the Whoop webhook signature verification.

We don't test the dispatch path — that's a thin wrapper around the
ingest pipelines that already have their own coverage. The signature
verification is the security-critical part.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

import pytest

from ingest_whoop import webhooks


def _sign(secret: str, body: bytes, ts: int | None = None) -> tuple[str, str]:
    """Produce (signature_b64, timestamp_str) the way Whoop does."""
    ts = ts if ts is not None else int(time.time())
    msg = f"{ts}.".encode() + body
    sig = hmac.new(secret.encode(), msg, hashlib.sha256).digest()
    return base64.b64encode(sig).decode(), str(ts)


def test_verify_signature_accepts_valid(monkeypatch: pytest.MonkeyPatch):
    secret = "abc-shared-secret"
    monkeypatch.setattr(webhooks.settings, "WHOOP_WEBHOOK_SECRET", secret)
    body = b'{"type":"recovery.updated","id":42}'
    sig, ts = _sign(secret, body)
    assert webhooks._verify_signature(body, ts, sig) is True


def test_verify_signature_rejects_wrong_secret(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(webhooks.settings, "WHOOP_WEBHOOK_SECRET", "expected")
    body = b"x"
    sig, ts = _sign("attacker", body)
    assert webhooks._verify_signature(body, ts, sig) is False


def test_verify_signature_rejects_replay_too_old(monkeypatch: pytest.MonkeyPatch):
    secret = "s"
    monkeypatch.setattr(webhooks.settings, "WHOOP_WEBHOOK_SECRET", secret)
    body = b"x"
    old_ts = int(time.time()) - (webhooks.MAX_AGE_SEC + 5)
    sig, ts = _sign(secret, body, ts=old_ts)
    assert webhooks._verify_signature(body, ts, sig) is False


def test_verify_signature_rejects_when_secret_unset(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(webhooks.settings, "WHOOP_WEBHOOK_SECRET", "")
    body = b"x"
    sig, ts = _sign("anything", body)
    assert webhooks._verify_signature(body, ts, sig) is False


def test_verify_signature_rejects_tampered_body(monkeypatch: pytest.MonkeyPatch):
    secret = "s"
    monkeypatch.setattr(webhooks.settings, "WHOOP_WEBHOOK_SECRET", secret)
    body = b'{"a":1}'
    sig, ts = _sign(secret, body)
    tampered = b'{"a":2}'
    assert webhooks._verify_signature(tampered, ts, sig) is False


def test_verify_signature_rejects_non_numeric_timestamp(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(webhooks.settings, "WHOOP_WEBHOOK_SECRET", "s")
    assert webhooks._verify_signature(b"x", "not-a-number", "sig") is False
