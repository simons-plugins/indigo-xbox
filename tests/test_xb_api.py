"""Tests for xb_api.py — the stdlib multi-host HTTP layer, driven by a scripted
transport (no network). Covers URL splitting, JSON parse, and error envelopes."""
import json

import pytest

from xb_api import XboxAPI, XboxError, redact
from support import ScriptedTransport


def _api(transport):
    return XboxAPI(connection_factory=transport.factory)


def test_get_json_parses_body_and_splits_host():
    transport = ScriptedTransport().queue(200, body=json.dumps({"ok": True}))
    api = _api(transport)
    data = api.get_json("https://peoplehub.xboxlive.com/users/me/people?x=1",
                        headers={"Authorization": "XBL3.0 x=u;t"})
    assert data == {"ok": True}
    req = transport.requests[0]
    assert req["host"] == "peoplehub.xboxlive.com"
    assert req["url"] == "/users/me/people?x=1"          # path + query preserved
    assert req["headers"]["Authorization"] == "XBL3.0 x=u;t"


def test_post_form_encodes_body():
    transport = ScriptedTransport().queue(200, body=json.dumps({"access_token": "A"}))
    api = _api(transport)
    api.post_form("https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
                  {"grant_type": "refresh_token", "client_id": "cid"})
    body = transport.requests[0]["body"]
    assert "grant_type=refresh_token" in body
    assert "client_id=cid" in body


def test_oauth_error_envelope_parsed():
    body = json.dumps({"error": "authorization_pending",
                       "error_description": "waiting for user"})
    transport = ScriptedTransport().queue(400, body=body)
    api = _api(transport)
    with pytest.raises(XboxError) as excinfo:
        api.post_form("https://login.microsoftonline.com/x", {})
    assert excinfo.value.status == 400
    assert excinfo.value.key == "authorization_pending"
    assert excinfo.value.description == "waiting for user"


def test_xsts_xerr_parsed():
    transport = ScriptedTransport().queue(401, body=json.dumps({"XErr": 2148916238}))
    api = _api(transport)
    with pytest.raises(XboxError) as excinfo:
        api.post_json("https://xsts.auth.xboxlive.com/xsts/authorize", {})
    assert excinfo.value.status == 401
    assert excinfo.value.xerr == 2148916238


def test_retry_after_parsed():
    transport = ScriptedTransport().queue(429, headers={"Retry-After": "30"}, body="{}")
    api = _api(transport)
    with pytest.raises(XboxError) as excinfo:
        api.get_json("https://peoplehub.xboxlive.com/x")
    assert excinfo.value.status == 429
    assert excinfo.value.retry_after == 30


def test_transport_error_wrapped():
    transport = ScriptedTransport().queue_exception(OSError("connection refused"))
    api = _api(transport)
    with pytest.raises(XboxError):
        api.get_json("https://peoplehub.xboxlive.com/x")


def test_invalid_json_raises():
    transport = ScriptedTransport().queue(200, body="not json")
    api = _api(transport)
    with pytest.raises(XboxError):
        api.get_json("https://peoplehub.xboxlive.com/x")


def test_redact():
    assert redact("abcdefghijklmnop") == "abcd…mnop"
    assert redact("short") == "…"
    assert redact("") == ""
