"""Unit tests for xb_auth.py — device flow, MSA refresh, XSTS chain, storage."""
import json
import os
import threading
from unittest.mock import Mock

import pytest

import xb_auth
import xb_constants as xc
from xb_auth import XboxAuth, TokenStore, parse_not_after
from support import FakeAPI, oauth_error, xsts_denied

CLIENT_A = "client-aaaa-0001"
CLIENT_B = "client-bbbb-0002"

DEVICE_AUTH = {
    "device_code": "DEV-CODE-XYZ",
    "user_code": "ABCD-1234",
    "verification_uri": "https://microsoft.com/link",
    "expires_in": 900,
    "interval": 5,
}


def token_response(access="ACCESS-1", refresh="REFRESH-1", expires_in=3600, scope=xc.SCOPE):
    return {"access_token": access, "refresh_token": refresh,
            "expires_in": expires_in, "scope": scope}


def make_auth(api, tmp_path, client_id=CLIENT_A, now=None):
    token_file = os.path.join(str(tmp_path), "tokens.json")
    return XboxAuth(api, token_file, client_id,
                    logger=Mock(), now=now or (lambda: 1_000.0), sleep=lambda s: None)


# -- Device Flow --------------------------------------------------------------

def test_device_flow_happy_path(tmp_path):
    api = FakeAPI()
    api.queue_post_form(DEVICE_AUTH).queue_post_form(token_response())
    auth = make_auth(api, tmp_path)

    info = auth.start_device_flow()
    assert info["user_code"] == "ABCD-1234"
    assert info["verification_uri"] == "https://microsoft.com/link"
    assert auth.state() == xc.STATE_PENDING

    status, _ = auth.poll_device_flow()
    assert status == "success"
    assert auth.is_authorized()
    assert auth.access_token() == "ACCESS-1"

    with open(auth._token_path, encoding="utf-8") as handle:  # pylint: disable=protected-access
        stored = json.load(handle)
    assert stored[CLIENT_A]["refresh_token"] == "REFRESH-1"
    assert stored[CLIENT_A]["expires_at"] == 1_000.0 + 3600


def test_device_flow_uses_urn_grant_type(tmp_path):
    api = FakeAPI()
    api.queue_post_form(DEVICE_AUTH).queue_post_form(token_response())
    auth = make_auth(api, tmp_path)
    auth.start_device_flow()
    auth.poll_device_flow()
    token_post = [c for c in api.post_form_calls if c["url"] == xc.TOKEN_URL][0]
    assert token_post["form"]["grant_type"] == "urn:ietf:params:oauth:grant-type:device_code"


def test_device_flow_pending_then_success(tmp_path):
    api = FakeAPI()
    (api.queue_post_form(DEVICE_AUTH)
        .queue_post_form(oauth_error("authorization_pending"))
        .queue_post_form(token_response()))
    auth = make_auth(api, tmp_path)
    status, _ = auth.run_device_flow()
    assert status == "success"
    assert auth.is_authorized()


def test_device_flow_slow_down_increases_interval(tmp_path):
    api = FakeAPI()
    (api.queue_post_form(DEVICE_AUTH)
        .queue_post_form(oauth_error("slow_down"))
        .queue_post_form(token_response()))
    auth = make_auth(api, tmp_path)
    status, _ = auth.run_device_flow()
    assert status == "success"
    assert auth._device_interval == 10     # pylint: disable=protected-access


def test_device_flow_declined(tmp_path):
    api = FakeAPI()
    (api.queue_post_form(DEVICE_AUTH)
        .queue_post_form(oauth_error("authorization_declined", description="user said no")))
    auth = make_auth(api, tmp_path)
    status, detail = auth.run_device_flow()
    assert status == "denied"
    assert detail == "user said no"
    assert auth.state() == xc.STATE_UNAUTHORIZED
    assert not auth.is_authorized()


def test_device_flow_expired_token_restarts_once(tmp_path):
    api = FakeAPI()
    (api.queue_post_form(DEVICE_AUTH)
        .queue_post_form(oauth_error("expired_token"))
        .queue_post_form(DEVICE_AUTH)
        .queue_post_form(token_response()))
    auth = make_auth(api, tmp_path)
    status, _ = auth.run_device_flow(max_restarts=1)
    assert status == "success"
    device_posts = [c for c in api.post_form_calls if c["url"] == xc.DEVICE_CODE_URL]
    assert len(device_posts) == 2          # restarted once


def test_run_device_flow_reuses_started_code(tmp_path):
    api = FakeAPI()
    api.queue_post_form(DEVICE_AUTH).queue_post_form(token_response())
    auth = make_auth(api, tmp_path)
    auth.start_device_flow()
    status, _ = auth.run_device_flow()
    assert status == "success"
    device_posts = [c for c in api.post_form_calls if c["url"] == xc.DEVICE_CODE_URL]
    assert len(device_posts) == 1          # no second flow started


# -- MSA refresh --------------------------------------------------------------

def test_refresh_persists_new_refresh_token(tmp_path):
    api = FakeAPI()
    auth = make_auth(api, tmp_path)
    auth._store_token(token_response(access="ACCESS-1", refresh="REFRESH-1"))  # pylint: disable=protected-access

    api.queue_post_form(token_response(access="ACCESS-2", refresh="REFRESH-2"))
    assert auth.refresh_if_needed(force=True) is True
    assert auth.access_token() == "ACCESS-2"
    with open(auth._token_path, encoding="utf-8") as handle:  # pylint: disable=protected-access
        stored = json.load(handle)
    assert stored[CLIENT_A]["refresh_token"] == "REFRESH-2"
    refresh_post = [c for c in api.post_form_calls if c["form"].get("grant_type") == "refresh_token"][0]
    assert refresh_post["form"]["scope"] == xc.SCOPE       # scope re-sent on refresh


def test_refresh_429_sets_backoff_and_keeps_token(tmp_path):
    api = FakeAPI()
    auth = make_auth(api, tmp_path)
    auth._store_token(token_response())                    # pylint: disable=protected-access

    api.queue_post_form(oauth_error("too_many", status=429, retry_after=30))
    assert auth.refresh_if_needed(force=True) is False
    assert auth.state() == xc.STATE_AUTHORIZED
    assert auth._refresh_backoff_until == 1_000.0 + 30     # pylint: disable=protected-access


def test_refresh_invalid_grant_sets_auth_required_and_halts(tmp_path):
    api = FakeAPI()
    auth = make_auth(api, tmp_path)
    auth._store_token(token_response())                    # pylint: disable=protected-access

    api.queue_post_form(oauth_error("invalid_grant", status=400))
    assert auth.refresh_if_needed(force=True) is False
    assert auth.state() == xc.STATE_AUTH_REQUIRED
    posts_after = len(api.post_form_calls)

    for _ in range(3):
        assert auth.refresh_if_needed(force=True) is False
    assert len(api.post_form_calls) == posts_after          # zero further HTTP

    # A successful re-auth recovers.
    auth._store_token(token_response(access="new", refresh="new-r"))  # pylint: disable=protected-access
    assert auth.state() == xc.STATE_AUTHORIZED
    api.queue_post_form(token_response(access="newer", refresh="newer-r"))
    assert auth.refresh_if_needed(force=True) is True


def test_next_refresh_due_ten_minutes_before_expiry(tmp_path):
    api = FakeAPI()
    auth = make_auth(api, tmp_path)
    auth._store_token(token_response(expires_in=3600))     # pylint: disable=protected-access
    assert auth.next_refresh_due() == 1_000.0 + 3600 - 600


def test_refresh_respects_min_interval(tmp_path):
    api = FakeAPI()
    auth = make_auth(api, tmp_path)
    auth._store_token(token_response())                    # pylint: disable=protected-access
    entry = auth._store.get(CLIENT_A)                      # pylint: disable=protected-access
    entry["expires_at"] = 1_000.0                          # past the refresh window
    auth._store.set(CLIENT_A, entry)                       # pylint: disable=protected-access
    auth._last_refresh_attempt = 1_000.0                   # pylint: disable=protected-access
    assert auth.refresh_if_needed(force=False) is False
    assert not any(c["form"].get("grant_type") == "refresh_token" for c in api.post_form_calls)


def test_refresh_generic_failure_backs_off_exponentially(tmp_path):
    api = FakeAPI()
    t = {"now": 1_000.0}
    auth = make_auth(api, tmp_path, now=lambda: t["now"])
    auth._store_token(token_response())                    # pylint: disable=protected-access

    api.queue_post_form(oauth_error("server_error", status=500))
    assert auth.refresh_if_needed(force=True) is False
    assert len(api.post_form_calls) == 1

    t["now"] += 10                                         # inside the 60s backoff
    assert auth.refresh_if_needed(force=True) is False
    assert len(api.post_form_calls) == 1

    t["now"] += 60                                         # backoff over: retried
    api.queue_post_form(oauth_error("server_error", status=500))
    assert auth.refresh_if_needed(force=True) is False
    assert len(api.post_form_calls) == 2

    t["now"] += 100                                        # 100 < doubled 120s backoff
    assert auth.refresh_if_needed(force=True) is False
    assert len(api.post_form_calls) == 2

    t["now"] += 30
    api.queue_post_form(token_response(access="ACCESS-2", refresh="REFRESH-2"))
    assert auth.refresh_if_needed(force=True) is True
    assert auth.access_token() == "ACCESS-2"


def test_matches_compares_client_id(tmp_path):
    auth = make_auth(FakeAPI(), tmp_path, client_id=CLIENT_A)
    assert auth.matches(CLIENT_A)
    assert not auth.matches(CLIENT_B)


# -- XSTS token chain ---------------------------------------------------------

def _user_token(uhs="UHS1"):
    return {"Token": "USER-TOKEN", "DisplayClaims": {"xui": [{"uhs": uhs}]}}


def _xsts_token(uhs="UHS1", token="XSTS-TOKEN", not_after="2999-10-10T19:06:35.5251155Z"):
    return {"Token": token, "NotAfter": not_after,
            "DisplayClaims": {"xui": [{"uhs": uhs, "xid": "XID", "gtg": "GamerTag"}]}}


def test_xbl_header_derives_and_caches_chain(tmp_path):
    api = FakeAPI()
    auth = make_auth(api, tmp_path)
    auth._store_token(token_response())                    # pylint: disable=protected-access
    api.queue_post_json(_user_token()).queue_post_json(_xsts_token())

    assert auth.xbl_header() == "XBL3.0 x=UHS1;XSTS-TOKEN"
    # A second call uses the cached XSTS token (NotAfter far away): no new HTTP.
    assert auth.xbl_header() == "XBL3.0 x=UHS1;XSTS-TOKEN"
    assert len(api.post_json_calls) == 2                  # user + xsts, once


def test_xbl_header_none_when_unauthorized(tmp_path):
    auth = make_auth(FakeAPI(), tmp_path)
    assert auth.xbl_header() is None


def test_xbl_user_token_request_shape(tmp_path):
    api = FakeAPI()
    auth = make_auth(api, tmp_path)
    auth._store_token(token_response(access="MSA-ACCESS"))  # pylint: disable=protected-access
    api.queue_post_json(_user_token()).queue_post_json(_xsts_token())
    auth.xbl_header()
    user_call = api.post_json_calls[0]
    assert user_call["url"] == xc.USER_AUTH_URL
    assert user_call["payload"]["Properties"]["RpsTicket"] == "d=MSA-ACCESS"
    assert user_call["headers"]["x-xbl-contract-version"] == xc.XBL_CONTRACT_VERSION
    xsts_call = api.post_json_calls[1]
    assert xsts_call["payload"]["Properties"]["UserTokens"] == ["USER-TOKEN"]
    assert xsts_call["payload"]["Properties"]["SandboxId"] == xc.SANDBOX_ID


def test_invalidate_xbl_forces_rederive(tmp_path):
    api = FakeAPI()
    auth = make_auth(api, tmp_path)
    auth._store_token(token_response())                    # pylint: disable=protected-access
    api.queue_post_json(_user_token()).queue_post_json(_xsts_token())
    auth.xbl_header()
    auth.invalidate_xbl()
    api.queue_post_json(_user_token()).queue_post_json(_xsts_token(token="XSTS-2"))
    assert auth.xbl_header() == "XBL3.0 x=UHS1;XSTS-2"
    assert len(api.post_json_calls) == 4


def test_xsts_child_account_is_terminal(tmp_path):
    api = FakeAPI()
    auth = make_auth(api, tmp_path)
    auth._store_token(token_response())                    # pylint: disable=protected-access
    api.queue_post_json(_user_token()).queue_post_json(xsts_denied(2148916238))
    with pytest.raises(Exception):
        auth.xbl_header()
    assert auth.state() == xc.STATE_AUTH_REQUIRED


def test_xsts_no_profile_not_terminal(tmp_path):
    api = FakeAPI()
    auth = make_auth(api, tmp_path)
    auth._store_token(token_response())                    # pylint: disable=protected-access
    api.queue_post_json(_user_token()).queue_post_json(xsts_denied(2148916233))
    with pytest.raises(Exception):
        auth.xbl_header()
    assert auth.state() == xc.STATE_AUTHORIZED             # recoverable, not latched


# -- Own identity (self-tracking) ----------------------------------------------

def test_own_identity_none_when_unauthorized(tmp_path):
    auth = make_auth(FakeAPI(), tmp_path)
    assert auth.own_identity() is None


def test_own_identity_derives_chain_and_returns_xuid_gamertag(tmp_path):
    api = FakeAPI()
    auth = make_auth(api, tmp_path)
    auth._store_token(token_response())                    # pylint: disable=protected-access
    api.queue_post_json(_user_token()).queue_post_json(_xsts_token())
    assert auth.own_identity() == ("XID", "GamerTag")
    assert len(api.post_json_calls) == 2                   # user + xsts, derived once


def test_own_identity_reuses_cached_chain(tmp_path):
    api = FakeAPI()
    auth = make_auth(api, tmp_path)
    auth._store_token(token_response())                    # pylint: disable=protected-access
    api.queue_post_json(_user_token()).queue_post_json(_xsts_token())
    auth.xbl_header()                                       # derives + caches
    assert auth.own_identity() == ("XID", "GamerTag")
    assert len(api.post_json_calls) == 2                   # no new HTTP — used the cache


def test_own_identity_none_on_chain_failure(tmp_path):
    api = FakeAPI()
    auth = make_auth(api, tmp_path)
    auth._store_token(token_response())                    # pylint: disable=protected-access
    api.queue_post_json(_user_token()).queue_post_json(xsts_denied(2148916233))
    assert auth.own_identity() is None


# -- NotAfter parsing ---------------------------------------------------------

def test_parse_not_after_seven_fractional_digits_and_z():
    epoch = parse_not_after("2999-10-10T19:06:35.5251155Z")
    assert epoch > 32_000_000_000                          # well into the future
    assert isinstance(epoch, float)


def test_parse_not_after_no_fraction():
    assert parse_not_after("2010-10-10T03:06:35Z") > 0


def test_parse_not_after_bad_value_is_zero():
    assert parse_not_after("not-a-date") == 0.0
    assert parse_not_after("") == 0.0
    assert parse_not_after(None) == 0.0


# -- Token store persistence --------------------------------------------------

def test_token_store_keyed_per_client(tmp_path):
    token_file = os.path.join(str(tmp_path), "tokens.json")
    auth_a = XboxAuth(FakeAPI(), token_file, CLIENT_A, logger=Mock(), now=lambda: 1.0)
    auth_a._store_token(token_response(access="A-ACCESS", refresh="A-REFRESH"))  # pylint: disable=protected-access
    auth_b = XboxAuth(FakeAPI(), token_file, CLIENT_B, logger=Mock(), now=lambda: 1.0)
    auth_b._store_token(token_response(access="B-ACCESS", refresh="B-REFRESH"))  # pylint: disable=protected-access

    auth_c = XboxAuth(FakeAPI(), token_file, CLIENT_A, logger=Mock(), now=lambda: 1.0)
    assert auth_c.access_token() == "A-ACCESS"             # B did not clobber A
    with open(token_file, encoding="utf-8") as handle:
        assert set(json.load(handle)) == {CLIENT_A, CLIENT_B}


def test_token_file_permissions_0600(tmp_path):
    api = FakeAPI()
    auth = make_auth(api, tmp_path)
    auth._store_token(token_response())                    # pylint: disable=protected-access
    mode = os.stat(auth._token_path).st_mode & 0o777      # pylint: disable=protected-access
    assert mode == 0o600


def test_atomic_save_does_not_corrupt_on_replace_failure(tmp_path, monkeypatch):
    path = os.path.join(str(tmp_path), "tokens.json")
    store = TokenStore(path, logger=Mock())
    store.set(CLIENT_A, {"access_token": "A1", "refresh_token": "R1", "expires_at": 1.0})

    def boom(_src, _dst):
        raise OSError("simulated crash during replace")

    monkeypatch.setattr(xb_auth.os, "replace", boom)
    store.set(CLIENT_B, {"access_token": "B1", "refresh_token": "R2", "expires_at": 1.0})  # no raise

    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    assert data[CLIENT_A]["access_token"] == "A1"          # original intact
    assert CLIENT_B not in data
    assert [f for f in os.listdir(str(tmp_path)) if f.endswith(".tmp")] == []


def test_late_save_from_stale_instance_does_not_clobber(tmp_path):
    token_file = os.path.join(str(tmp_path), "tokens.json")
    api2 = FakeAPI()
    auth2 = XboxAuth(api2, token_file, CLIENT_A, logger=Mock(), now=lambda: 1.0)
    api2.queue_post_form(DEVICE_AUTH).queue_post_form(token_response(access="NEW-ACCESS"))
    auth2.start_device_flow()
    auth2.poll_device_flow()

    api1 = FakeAPI()
    auth1 = XboxAuth(api1, token_file, CLIENT_A, logger=Mock(), now=lambda: 1.0)
    auth1.mark_stale()
    api1.queue_post_form(DEVICE_AUTH).queue_post_form(token_response(access="STALE-ACCESS"))
    auth1.start_device_flow()
    status, _ = auth1.poll_device_flow()

    assert status == "cancelled"
    assert auth2.access_token() == "NEW-ACCESS"
    with open(token_file, encoding="utf-8") as handle:
        assert json.load(handle)[CLIENT_A]["access_token"] == "NEW-ACCESS"


def test_concurrent_refresh_submits_exactly_once(tmp_path):
    api = FakeAPI()
    auth = make_auth(api, tmp_path)
    auth._store_token(token_response(access="A1", refresh="R1"))  # pylint: disable=protected-access
    api.queue_post_form(token_response(access="A2", refresh="R2"))   # only ONE response queued

    barrier = threading.Barrier(2)
    results = []
    errors = []

    def worker():
        try:
            barrier.wait()
            results.append(auth.refresh_if_needed(force=True))
        except Exception as exc:  # pylint: disable=broad-except
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert errors == []
    refresh_posts = [c for c in api.post_form_calls if c["form"].get("grant_type") == "refresh_token"]
    assert len(refresh_posts) == 1
    assert all(results)
    assert auth.access_token() == "A2"


@pytest.mark.parametrize("entry", [
    {"access_token": "A", "refresh_token": "R"},                       # missing expires_at
    {"access_token": "A", "refresh_token": "R", "expires_at": "soon"},  # non-numeric
])
def test_corrupt_expires_at_refreshes_not_crashes(tmp_path, entry):
    api = FakeAPI()
    auth = make_auth(api, tmp_path)
    auth._store._entries[CLIENT_A] = dict(entry)          # pylint: disable=protected-access
    assert auth.next_refresh_due() is not None
    api.queue_post_form(token_response(access="ACCESS-2", refresh="REFRESH-2"))
    assert auth.refresh_if_needed() is True
    assert isinstance(auth.access_expires_at(), float)
