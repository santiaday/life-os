"""Pure-logic tests for lifeos_core.whoop_cognito.

The HTTP layer (`_call`) is monkeypatched so these run with no network — they
assert the request shapes (AuthFlow, ClientId='', MFA challenge handling) and
token extraction that the Whoop Cognito proxy expects.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC

import pytest

from lifeos_core import whoop_cognito as wc


def _jwt(exp: int) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"hdr.{payload}.sig"


def test_decode_jwt_exp():
    assert wc.decode_jwt_exp(_jwt(1900000000)) == 1900000000
    assert wc.decode_jwt_exp("not-a-jwt") == 0
    assert wc.decode_jwt_exp("") == 0


def test_refresh_session_shape(monkeypatch):
    captured = {}

    def fake_call(target, body):
        captured["target"] = target
        captured["body"] = body
        return {"AuthenticationResult": {"AccessToken": _jwt(1900000000), "IdToken": "id"}}

    monkeypatch.setattr(wc, "_call", fake_call)
    bundle = wc.refresh_session("REFRESH123")

    assert captured["target"] == "InitiateAuth"
    assert captured["body"]["AuthFlow"] == "REFRESH_TOKEN_AUTH"
    assert captured["body"]["AuthParameters"]["REFRESH_TOKEN"] == "REFRESH123"
    assert captured["body"]["ClientId"] == ""
    assert bundle["access_token"] == _jwt(1900000000)
    assert bundle["refresh_token"] == "REFRESH123"  # not rotated -> fallback kept
    assert bundle["expires_at"].tzinfo == UTC


def test_bootstrap_login_no_mfa(monkeypatch):
    def fake_call(target, body):
        assert body["AuthFlow"] == "USER_PASSWORD_AUTH"
        assert body["AuthParameters"] == {"USERNAME": "e@x.com", "PASSWORD": "pw"}
        return {"AuthenticationResult": {
            "AccessToken": _jwt(1900000000), "RefreshToken": "NEWREFRESH", "IdToken": "id"}}

    monkeypatch.setattr(wc, "_call", fake_call)
    bundle = wc.bootstrap_login("e@x.com", "pw", lambda c: "000000")
    assert bundle["refresh_token"] == "NEWREFRESH"


def test_bootstrap_login_sms_mfa(monkeypatch):
    calls = []

    def fake_call(target, body):
        calls.append((target, body))
        if target == "InitiateAuth":
            return {"ChallengeName": "SMS_MFA", "Session": "SESS"}
        return {"AuthenticationResult": {
            "AccessToken": _jwt(1900000000), "RefreshToken": "R2", "IdToken": "id"}}

    monkeypatch.setattr(wc, "_call", fake_call)
    seen = {}

    def mfa(challenge: str) -> str:
        seen["c"] = challenge
        return "123456"

    bundle = wc.bootstrap_login("e@x.com", "pw", mfa)

    assert seen["c"] == "SMS_MFA"
    assert calls[1][0] == "RespondToAuthChallenge"
    cr = calls[1][1]["ChallengeResponses"]
    assert cr["SMS_MFA_CODE"] == "123456"
    assert cr["USERNAME"] == "e@x.com"
    assert calls[1][1]["ClientId"] == ""
    assert bundle["refresh_token"] == "R2"


def test_bootstrap_login_unexpected_challenge(monkeypatch):
    monkeypatch.setattr(wc, "_call", lambda t, b: {"ChallengeName": "NEW_PASSWORD_REQUIRED"})
    with pytest.raises(wc.WhoopCognitoError):
        wc.bootstrap_login("e@x.com", "pw", lambda c: "x")
