"""Lose It token handling.

The behaviour worth pinning: the token endpoint returns 200 with a body of
`{"user_id", "username"}` and delivers the JWT as a Set-Cookie. An
implementation that reads only the body treats a perfectly good login as a
malformed response — which is exactly what happened the first time.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from ingest_loseit import auth


def _jwt(exp: datetime, sub: str = "50958839") -> str:
    def seg(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return (seg({"alg": "ES384", "typ": "JWT"})
            + "." + seg({"sub": sub, "iss": "Lose It!",
                         "exp": int(exp.timestamp()),
                         "iat": int((exp - timedelta(days=14)).timestamp())})
            + ".sig")


def _mock_post(monkeypatch, *, status=200, body=None, cookies=None):
    """Stand in for the token endpoint."""
    def handler(request: httpx.Request) -> httpx.Response:
        headers = []
        for name, value in (cookies or {}).items():
            headers.append(("set-cookie", f"{name}={value}; Path=/; Domain=.loseit.com"))
        return httpx.Response(status, json=body if body is not None else {},
                              headers=headers)

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def patched(*a, **kw):
        kw["transport"] = transport
        return real_client(*a, **kw)

    monkeypatch.setattr(auth.httpx, "Client", patched)


def test_token_is_read_from_the_set_cookie_not_the_body(monkeypatch):
    exp = datetime.now(UTC) + timedelta(days=14)
    token = _jwt(exp)
    _mock_post(monkeypatch,
               body={"user_id": 50958839, "username": "santi"},
               cookies={"liauth": token, "fn_auth": token})

    out = auth._post({"grant_type": "password"})
    assert out["access_token"] == token
    assert out["_token_from_cookie"] == "liauth"
    # the body fields survive alongside it
    assert out["user_id"] == 50958839


def test_liauth_is_preferred_over_fn_auth(monkeypatch):
    exp = datetime.now(UTC) + timedelta(days=14)
    li, fn = _jwt(exp), _jwt(exp, sub="other")
    _mock_post(monkeypatch, body={}, cookies={"fn_auth": fn, "liauth": li})
    assert auth._post({})["access_token"] == li


def test_body_access_token_wins_when_present(monkeypatch):
    exp = datetime.now(UTC) + timedelta(days=14)
    body_tok, cookie_tok = _jwt(exp), _jwt(exp, sub="cookie")
    _mock_post(monkeypatch, body={"access_token": body_tok},
               cookies={"liauth": cookie_tok})
    out = auth._post({})
    assert out["access_token"] == body_tok
    assert "_token_from_cookie" not in out


def test_cookieless_and_bodyless_response_is_an_error(monkeypatch):
    _mock_post(monkeypatch, body={"user_id": 1, "username": "x"}, cookies={})
    payload = auth._post({})
    with pytest.raises(auth.LoseItAuthError, match="carried no token"):
        auth._persist(payload)


def test_rate_limit_is_reported_distinctly(monkeypatch):
    _mock_post(monkeypatch, status=429, body={})
    with pytest.raises(auth.LoseItAuthError, match="rate-limited"):
        auth._post({})


def test_non_200_surfaces_the_status(monkeypatch):
    _mock_post(monkeypatch, status=401, body={"error": "invalid_grant"})
    with pytest.raises(auth.LoseItAuthError, match="HTTP 401"):
        auth._post({})


def test_jwt_expiry_reads_exp_without_verifying():
    exp = datetime.now(UTC) + timedelta(days=14)
    got = auth.jwt_expiry(_jwt(exp))
    assert abs((got - exp).total_seconds()) < 1


def test_jwt_expiry_tolerates_garbage():
    assert auth.jwt_expiry("not-a-jwt") is None
    assert auth.jwt_expiry("") is None
