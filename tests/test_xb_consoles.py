"""Tests for xb_consoles.py — pure parsing of xccs console list/status/apps,
power-state display mapping, and fetch/send orchestration (401 retry, status
errorCode -> XboxError). Fixtures for console_list/status are real, id-stripped
xccs responses; ``console_installed_apps.json`` is hand-built (synthetic) since
no live installedApps capture was taken."""
import pytest

import xb_constants as xc
import xb_consoles
from xb_api import XboxError
from support import FakeAPI, load_fixture, oauth_error


# -- power-state display mapping ------------------------------------------------

def test_is_on_true_only_for_on():
    assert xb_consoles.is_on("On") is True
    assert xb_consoles.is_on("ConnectedStandby") is False
    assert xb_consoles.is_on("Off") is False
    assert xb_consoles.is_on("SystemUpdate") is False
    assert xb_consoles.is_on("Unknown") is False
    assert xb_consoles.is_on("") is False
    assert xb_consoles.is_on(None) is False


@pytest.mark.parametrize("power_state,expected", [
    ("On", "On"),
    ("ConnectedStandby", "Standby"),
    ("Off", "Off"),
    ("SystemUpdate", "Updating"),
])
def test_display_text_known_states(power_state, expected):
    assert xb_consoles.display_text(power_state) == expected


@pytest.mark.parametrize("power_state", ["Unknown", "", None, "SomethingNew"])
def test_display_text_unavailable_for_unknown_or_missing(power_state):
    assert xb_consoles.display_text(power_state) is None


# -- parse_console_list ----------------------------------------------------------

def test_parse_console_list_on():
    consoles = xb_consoles.parse_console_list(load_fixture("console_list_on.json"))
    assert len(consoles) == 1
    console = consoles[0]
    assert console.id == "CONSOLE_ID_1"
    assert console.name == "Living room Xbox"
    assert console.console_type == "XboxSeriesS"
    assert console.power_state == "On"
    assert console.remote_management_enabled is True


def test_parse_console_list_standby():
    consoles = xb_consoles.parse_console_list(load_fixture("console_list_standby.json"))
    assert consoles[0].power_state == "ConnectedStandby"


def test_parse_console_list_explicit_empty_result_is_empty():
    assert xb_consoles.parse_console_list({"result": [],
                                           "status": {"errorCode": "OK"}}) == []


def test_parse_console_list_missing_result_raises():
    with pytest.raises(XboxError):
        xb_consoles.parse_console_list({"status": {"errorCode": "OK"}})
    with pytest.raises(XboxError):
        xb_consoles.parse_console_list({})
    with pytest.raises(XboxError):
        xb_consoles.parse_console_list(None)


def test_parse_console_list_non_dict_payload_raises():
    with pytest.raises(XboxError):
        xb_consoles.parse_console_list(["not", "a", "dict"])
    with pytest.raises(XboxError):
        xb_consoles.parse_console_list("also not a dict")


def test_parse_console_list_non_dict_items_skipped():
    payload = {"result": [{"id": "C1", "name": "Zed", "consoleType": "XboxOne",
                          "powerState": "On"}, "garbage", 42, None],
              "status": {"errorCode": "OK"}}
    consoles = xb_consoles.parse_console_list(payload)
    assert len(consoles) == 1
    assert consoles[0].id == "C1"


def test_parse_console_list_error_code_raises_with_code_in_message():
    payload = {"result": [], "status": {"errorCode": "RemoteManagementDisabled",
                                        "errorMessage": "disabled"}}
    with pytest.raises(XboxError) as excinfo:
        xb_consoles.parse_console_list(payload)
    assert "RemoteManagementDisabled" in str(excinfo.value)
    assert excinfo.value.key == "RemoteManagementDisabled"


# -- parse_console_status ---------------------------------------------------------

def test_parse_console_status_on():
    status = xb_consoles.parse_console_status(load_fixture("console_status_on.json"))
    assert status.power_state == "On"
    assert status.focus_app_aumid == ""
    assert status.playback_state == "Stopped"


def test_parse_console_status_standby():
    status = xb_consoles.parse_console_status(load_fixture("console_status_standby.json"))
    assert status.power_state == "ConnectedStandby"


def test_parse_console_status_error_code_raises():
    payload = {"status": {"errorCode": "CurrentConsoleNotFound", "errorMessage": None}}
    with pytest.raises(XboxError) as excinfo:
        xb_consoles.parse_console_status(payload)
    assert "CurrentConsoleNotFound" in str(excinfo.value)


def test_parse_console_status_missing_power_state_raises():
    with pytest.raises(XboxError):
        xb_consoles.parse_console_status({})
    with pytest.raises(XboxError):
        xb_consoles.parse_console_status({"focusAppAumid": "GAME.Halo!App"})


def test_parse_console_status_non_dict_payload_raises():
    with pytest.raises(XboxError):
        xb_consoles.parse_console_status(["not", "a", "dict"])


def test_parse_console_status_aumid_absent_is_none_unknown_focus():
    """No ``focusAppAumid`` key at all -> None, meaning "unknown, leave the
    caller's focused states as they were" — distinct from an explicit blank
    string (the dashboard)."""
    status = xb_consoles.parse_console_status({"powerState": "On"})
    assert status.focus_app_aumid is None


def test_parse_console_status_aumid_blank_is_dashboard():
    status = xb_consoles.parse_console_status({"powerState": "On", "focusAppAumid": ""})
    assert status.focus_app_aumid == ""


# -- parse_installed_apps ----------------------------------------------------------

def test_parse_installed_apps():
    apps = xb_consoles.parse_installed_apps(load_fixture("console_installed_apps.json"))
    assert len(apps) == 2
    by_aumid = {a.aumid: a for a in apps}
    halo = by_aumid["GAMESTUDIO.HaloInfinite_8wekyb3d8bbwe!HaloInfinite"]
    assert halo.name == "Halo Infinite"
    assert halo.title_id == "2050424108"       # int on the wire -> str
    assert halo.is_game is True
    home = by_aumid["Microsoft.Xbox.HomeApp_8wekyb3d8bbwe!Xbox.Home.Application"]
    assert home.is_game is False


def test_parse_installed_apps_explicit_empty_result_is_empty():
    assert xb_consoles.parse_installed_apps({"result": [],
                                             "status": {"errorCode": "OK"}}) == []


def test_parse_installed_apps_non_dict_payload_raises():
    with pytest.raises(XboxError):
        xb_consoles.parse_installed_apps(["not", "a", "dict"])
    with pytest.raises(XboxError):
        xb_consoles.parse_installed_apps("also not a dict")


def test_parse_installed_apps_missing_result_raises():
    """A missing/malformed ``result`` is a failed call, not "no apps installed"
    — the caller (``_resolve_focused_title``) must treat it as a failure and
    cache nothing, not bless it as a real (empty) app list."""
    with pytest.raises(XboxError):
        xb_consoles.parse_installed_apps({})
    with pytest.raises(XboxError):
        xb_consoles.parse_installed_apps({"result": None})
    with pytest.raises(XboxError):
        xb_consoles.parse_installed_apps(None)


def test_parse_installed_apps_error_code_raises():
    payload = {"result": [], "status": {"errorCode": "InvalidDeviceId",
                                        "errorMessage": "bad id"}}
    with pytest.raises(XboxError) as excinfo:
        xb_consoles.parse_installed_apps(payload)
    assert "InvalidDeviceId" in str(excinfo.value)


# -- fetch_consoles ----------------------------------------------------------------

class _Auth:
    def __init__(self, header="XBL3.0 x=uhs;tok"):
        self._header = header
        self.invalidated = 0

    def xbl_header(self):
        return self._header

    def invalidate_xbl(self):
        self.invalidated += 1


def test_fetch_consoles_returns_none_when_unauthorized():
    assert xb_consoles.fetch_consoles(FakeAPI(), _Auth(header=None)) is None


def test_fetch_consoles_parses_and_sends_headers():
    api = FakeAPI().queue_get(load_fixture("console_list_on.json"))
    consoles = xb_consoles.fetch_consoles(api, _Auth())
    assert len(consoles) == 1
    call = api.get_calls[0]
    assert call["url"] == xc.XCCS_CONSOLE_LIST_URL
    assert call["headers"]["Authorization"] == "XBL3.0 x=uhs;tok"
    assert call["headers"]["x-xbl-contract-version"] == xc.XCCS_CONTRACT_VERSION
    assert call["headers"]["skillplatform"] == xc.XCCS_SKILL_PLATFORM


def test_fetch_consoles_retries_once_on_401():
    api = FakeAPI()
    api.queue_get(oauth_error("unauthorized", status=401))
    api.queue_get(load_fixture("console_list_on.json"))
    auth = _Auth()
    consoles = xb_consoles.fetch_consoles(api, auth)
    assert len(consoles) == 1
    assert auth.invalidated == 1


def test_fetch_consoles_reraises_non_401():
    api = FakeAPI().queue_get(oauth_error("too_many", status=429, retry_after=30))
    with pytest.raises(XboxError) as excinfo:
        xb_consoles.fetch_consoles(api, _Auth())
    assert excinfo.value.status == 429


# -- fetch_console_status -----------------------------------------------------------

def test_fetch_console_status_returns_none_when_unauthorized():
    assert xb_consoles.fetch_console_status(FakeAPI(), _Auth(header=None), "C1") is None


def test_fetch_console_status_sends_url_and_headers():
    api = FakeAPI().queue_get(load_fixture("console_status_on.json"))
    status = xb_consoles.fetch_console_status(api, _Auth(), "CONSOLE_ID_1")
    assert status.power_state == "On"
    call = api.get_calls[0]
    assert call["url"] == "https://xccs.xboxlive.com/consoles/CONSOLE_ID_1"


def test_fetch_console_status_retries_once_on_401():
    api = FakeAPI()
    api.queue_get(oauth_error("unauthorized", status=401))
    api.queue_get(load_fixture("console_status_on.json"))
    auth = _Auth()
    status = xb_consoles.fetch_console_status(api, auth, "CONSOLE_ID_1")
    assert status.power_state == "On"
    assert auth.invalidated == 1


# -- fetch_installed_apps -----------------------------------------------------------

def test_fetch_installed_apps_returns_none_when_unauthorized():
    assert xb_consoles.fetch_installed_apps(FakeAPI(), _Auth(header=None), "C1") is None


def test_fetch_installed_apps_sends_url():
    api = FakeAPI().queue_get(load_fixture("console_installed_apps.json"))
    apps = xb_consoles.fetch_installed_apps(api, _Auth(), "CONSOLE_ID_1")
    assert len(apps) == 2
    call = api.get_calls[0]
    assert call["url"] == "https://xccs.xboxlive.com/lists/installedApps?deviceId=CONSOLE_ID_1"


def test_fetch_installed_apps_retries_once_on_401():
    api = FakeAPI()
    api.queue_get(oauth_error("unauthorized", status=401))
    api.queue_get(load_fixture("console_installed_apps.json"))
    auth = _Auth()
    apps = xb_consoles.fetch_installed_apps(api, auth, "CONSOLE_ID_1")
    assert len(apps) == 2
    assert auth.invalidated == 1


# -- send_power_command --------------------------------------------------------------

def test_send_power_command_returns_none_when_unauthorized():
    assert xb_consoles.send_power_command(FakeAPI(), _Auth(header=None), "C1",
                                          xc.POWER_COMMAND_WAKE_UP, "sess") is None


def test_send_power_command_sends_expected_body_and_headers():
    api = FakeAPI().queue_post_json({"opId": "op1", "status": {"errorCode": "OK"}, "result": None})
    result = xb_consoles.send_power_command(api, _Auth(), "CONSOLE_ID_1",
                                            xc.POWER_COMMAND_WAKE_UP, "session-123")
    assert result["opId"] == "op1"
    call = api.post_json_calls[0]
    assert call["url"] == xc.XCCS_COMMANDS_URL
    assert call["payload"]["destination"] == "Xbox"
    assert call["payload"]["type"] == "Power"
    assert call["payload"]["command"] == xc.POWER_COMMAND_WAKE_UP
    assert call["payload"]["sessionId"] == "session-123"
    assert call["payload"]["sourceId"] == xc.POWER_COMMAND_SOURCE_ID
    assert call["payload"]["linkedXboxId"] == "CONSOLE_ID_1"
    assert call["headers"]["Authorization"] == "XBL3.0 x=uhs;tok"
    assert call["headers"]["x-xbl-contract-version"] == xc.XCCS_CONTRACT_VERSION


def test_send_power_command_retries_once_on_401():
    api = FakeAPI()
    api.queue_post_json(oauth_error("unauthorized", status=401))
    api.queue_post_json({"opId": "op1", "status": {"errorCode": "OK"}})
    auth = _Auth()
    result = xb_consoles.send_power_command(api, auth, "C1", xc.POWER_COMMAND_TURN_OFF, "sess")
    assert result["opId"] == "op1"
    assert auth.invalidated == 1


def test_send_power_command_non_dict_response_raises():
    """A JSON ``null`` body (present header, unexpected response shape) must
    raise, not be mistaken for the "no auth header" ``None`` sentinel."""
    api = FakeAPI().queue_post_json(None)
    with pytest.raises(XboxError) as excinfo:
        xb_consoles.send_power_command(api, _Auth(), "C1", xc.POWER_COMMAND_WAKE_UP, "sess")
    assert "unexpected power command response" in str(excinfo.value)


def test_send_power_command_raises_on_error_code():
    api = FakeAPI().queue_post_json({"opId": "op1",
                                     "status": {"errorCode": "XboxDataNotFound",
                                               "errorMessage": "not found"}})
    with pytest.raises(XboxError) as excinfo:
        xb_consoles.send_power_command(api, _Auth(), "C1", xc.POWER_COMMAND_WAKE_UP, "sess")
    assert "XboxDataNotFound" in str(excinfo.value)
