"""Tests for the mitmproxy-flow bootstrap CLI.

Doesn't actually run mitmproxy — synthesizes minimal fake-flow objects with
the attributes the parser reads. Exercises:
  - extracting the latest AuthenticationResult when multiple flows match
  - rejecting captures that don't contain a RespondToAuthChallenge response
  - the sidecar text file's exact bytes (no trailing newline)
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ingest_whoop_journal import bootstrap_from_capture as boot


def _flow(target: str, body: str | None, ts: float) -> SimpleNamespace:
    """Mimic enough of mitmproxy's HTTPFlow shape for the parser."""
    req = SimpleNamespace(
        headers={"x-amz-target": target},
        timestamp_start=ts,
    )
    resp = SimpleNamespace(get_text=lambda: body)
    return SimpleNamespace(request=req, response=resp, timestamp_created=ts)


_AUTH_RESULT_JSON = (
    '{"AuthenticationResult": {'
    '  "AccessToken": "eyJ.A.B",'
    '  "RefreshToken": "rrrrrrrr",'
    '  "IdToken": "eyJ.I.B",'
    '  "ExpiresIn": 3600'
    '}}'
)


def test_picks_latest_matching_flow():
    flows = [
        _flow("AWSCognitoIdentityProviderService.RespondToAuthChallenge",
              '{"AuthenticationResult": {"AccessToken": "old", "RefreshToken": "old-r"}}',
              ts=1000.0),
        _flow("AWSCognitoIdentityProviderService.InitiateAuth",
              '{"ChallengeName": "SMS_MFA"}', ts=1500.0),
        _flow("AWSCognitoIdentityProviderService.RespondToAuthChallenge",
              _AUTH_RESULT_JSON, ts=2000.0),
    ]
    result, ignored = boot._extract_latest_auth_result(flows)
    assert result["AccessToken"] == "eyJ.A.B"
    assert result["RefreshToken"] == "rrrrrrrr"
    assert ignored == 1  # the older RespondToAuthChallenge


def test_no_matching_flow_raises():
    flows = [
        _flow("AWSCognitoIdentityProviderService.InitiateAuth",
              '{"ChallengeName": "SMS_MFA"}', ts=1.0),
    ]
    with pytest.raises(boot.BootstrapError) as ei:
        boot._extract_latest_auth_result(flows)
    assert "No RespondToAuthChallenge" in str(ei.value)


def test_skips_challenge_responses_without_auth_result():
    """A flow whose response is a challenge ('SMS_MFA') and not the final
    token bundle shouldn't be picked up."""
    flows = [
        _flow("AWSCognitoIdentityProviderService.RespondToAuthChallenge",
              '{"ChallengeName": "SMS_MFA", "Session": "abc"}', ts=10.0),
    ]
    with pytest.raises(boot.BootstrapError):
        boot._extract_latest_auth_result(flows)


def test_skips_non_json_responses():
    flows = [
        _flow("AWSCognitoIdentityProviderService.RespondToAuthChallenge",
              "<html>cloudflare 403</html>", ts=10.0),
    ]
    with pytest.raises(boot.BootstrapError):
        boot._extract_latest_auth_result(flows)


def test_sidecar_has_no_trailing_newline(tmp_path):
    out = boot._write_sidecar("rrrr-token", tmp_path)
    raw = out.read_bytes()
    # Exact bytes — the iOS Shortcut substitutes the entire file content.
    assert raw == b"rrrr-token"
    assert not raw.endswith(b"\n")


def test_sidecar_filename_and_path(tmp_path):
    out = boot._write_sidecar("anything", tmp_path)
    assert out.name == boot.SIDECAR_FILENAME
    assert out.parent == tmp_path.resolve()
