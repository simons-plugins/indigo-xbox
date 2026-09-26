"""Tests for the xboxConsole device layer in plugin.py: the separate console
registry (never _tracked), console-list -> per-device sync (change-only writes,
power transitions, focused-title resolution + caching), degradation paths
(status/installedApps failure, console-list failure + backoff, console missing
from the list), Status Request, and the power on/off actions."""
import logging

import pytest

import indigo   # the conftest fake
import plugin
import xb_constants as xc
from support import FakeAPI, oauth_error
from xb_consoles import ConsoleInfo


def _plugin():
    return plugin.Plugin("com.simons-plugins.indigo-xbox", "Xbox", "2026.2.0", {})


def _console(console_id, name="Living room Xbox", console_type="XboxSeriesS",
            power_state="On"):
    return ConsoleInfo(id=console_id, name=name, console_type=console_type,
                      power_state=power_state, remote_management_enabled=True)


def _console_device(dev_id, console_id, states=None):
    dev = indigo.Device(id=dev_id, name=f"console{dev_id}", deviceTypeId="xboxConsole",
                        pluginProps={"consoleId": console_id, "consoleName": "Living room Xbox"},
                        states=states)
    indigo.devices.add(dev)
    return dev


class _FakeAuth:
    """Stand-in for XboxAuth: authorized, fixed header, counts invalidations."""

    def __init__(self, header="XBL3.0 x=uhs;tok"):
        self._header = header
        self.invalidated = 0

    def is_authorized(self):
        return True

    def xbl_header(self):
        return self._header

    def invalidate_xbl(self):
        self.invalidated += 1

    def state(self):
        return xc.STATE_AUTHORIZED

    def refresh_if_needed(self):
        return False


def setup_function(_func):
    indigo.devices._devices.clear()   # pylint: disable=protected-access


def _list_payload(console):
    return {"result": [{"id": console.id, "name": console.name,
                        "consoleType": console.console_type,
                        "powerState": console.power_state,
                        "remoteManagementEnabled": console.remote_management_enabled}],
            "status": {"errorCode": "OK", "errorMessage": None}}


# -- device lifecycle: consoles go in a SEPARATE registry ---------------------

def test_device_start_routes_console_to_console_registry_not_tracked():
    p = _plugin()
    dev = _console_device(1, "C1")
    p.deviceStartComm(dev)
    assert p._consoles[dev.id] == "C1"
    assert dev.id not in p._tracked


def test_device_stop_removes_console_registration():
    p = _plugin()
    dev = _console_device(2, "C1")
    p.deviceStartComm(dev)
    p.deviceStopComm(dev)
    assert dev.id not in p._consoles


def test_comm_property_change_on_console_id():
    p = _plugin()
    old = indigo.Device(id=3, deviceTypeId="xboxConsole", pluginProps={"consoleId": "A"})
    new = indigo.Device(id=3, deviceTypeId="xboxConsole", pluginProps={"consoleId": "B"})
    assert p.didDeviceCommPropertyChange(old, new) is True
    same = indigo.Device(id=3, deviceTypeId="xboxConsole", pluginProps={"consoleId": "A"})
    assert p.didDeviceCommPropertyChange(old, same) is False


def test_presence_device_start_still_routes_to_tracked_not_consoles():
    """Regression guard: adding the console branch must not change the
    existing xboxPresence routing."""
    p = _plugin()
    dev = indigo.Device(id=4, deviceTypeId="xboxPresence", pluginProps={"xuid": "X1"})
    indigo.devices.add(dev)
    p.deviceStartComm(dev)
    assert p._tracked[dev.id] == "X1"
    assert dev.id not in p._consoles


# -- validateDeviceConfigUi ----------------------------------------------------

def test_validate_console_config_requires_selection():
    p = _plugin()
    ok, _values, errors = p.validateDeviceConfigUi({"console": ""}, "xboxConsole", 0)
    assert ok is False
    assert "console" in errors


def test_validate_console_config_stores_id_and_name():
    p = _plugin()
    p._last_consoles = [_console("C1", name="Kid's Xbox")]
    ok, values = p.validateDeviceConfigUi({"console": "C1"}, "xboxConsole", 0)
    assert ok is True
    assert values["consoleId"] == "C1"
    assert values["consoleName"] == "Kid's Xbox"


# -- listConsoles ---------------------------------------------------------------

def test_list_consoles_empty_without_auth():
    p = _plugin()
    p._auth = None
    assert p.listConsoles() == []


def test_list_consoles_formats_name_and_type():
    p = _plugin()
    p._auth = _FakeAuth()
    p._api = FakeAPI().queue_get(_list_payload(_console("C1", name="Zed", console_type="XboxOne")))
    options = p.listConsoles()
    assert options == [("C1", "Zed (XboxOne)")]
    assert p._last_consoles[0].id == "C1"


def test_list_consoles_returns_empty_and_warns_on_error(caplog):
    p = _plugin()
    p._auth = _FakeAuth()
    p._api = FakeAPI().queue_get(oauth_error("boom", status=500))
    with caplog.at_level(logging.WARNING):
        options = p.listConsoles()
    assert options == []
    assert any("could not load console list" in r.getMessage() for r in caplog.records)


# -- console poll: sync writes -------------------------------------------------

def test_poll_writes_power_state_and_console_fields():
    p = _plugin()
    p._auth = _FakeAuth()
    console = _console("C1", power_state="On")
    p._api = FakeAPI().queue_get(_list_payload(console)).queue_get(
        {"powerState": "On", "focusAppAumid": "", "status": {"errorCode": "OK"}})
    dev = _console_device(10, "C1")
    p.deviceStartComm(dev)
    p._poll_consoles()
    assert dev.states["powerState"] == "On"
    assert dev.states["consoleName"] == "Living room Xbox"
    assert dev.states["consoleType"] == "XboxSeriesS"
    assert dev.states["onOffState"] is True
    assert dev.states["lastPowerChange"]          # stamped on the first-ever poll
    assert dev.states["lastPoll"]


def test_poll_second_identical_poll_writes_no_batch_but_heartbeats():
    p = _plugin()
    p._auth = _FakeAuth()
    console = _console("C1", power_state="ConnectedStandby")
    p._api = FakeAPI().queue_get(_list_payload(console)).queue_get(_list_payload(console))
    dev = _console_device(11, "C1")
    p.deviceStartComm(dev)
    p._poll_consoles()
    batches_after_first = len(dev.batches)
    p._poll_consoles()
    assert len(dev.batches) == batches_after_first     # change-only: no new batch
    assert dev.states["lastPoll"]                       # heartbeat still written


def test_poll_on_to_standby_transition_writes_off_and_power_change():
    p = _plugin()
    p._auth = _FakeAuth()
    on_console = _console("C1", power_state="On")
    standby_console = _console("C1", power_state="ConnectedStandby")
    p._api = (FakeAPI()
             .queue_get(_list_payload(on_console))
             .queue_get({"powerState": "On", "focusAppAumid": "", "status": {"errorCode": "OK"}})
             .queue_get(_list_payload(standby_console)))
    dev = _console_device(12, "C1")
    p.deviceStartComm(dev)
    p._poll_consoles()
    assert dev.states["onOffState"] is True
    p._poll_consoles()          # standby: status endpoint must NOT be called (nothing queued)
    assert dev.states["onOffState"] is False
    assert dev.states["powerState"] == "ConnectedStandby"
    assert dev.states["lastPowerChange"]
    on_item = [i for i in dev.batches[-1] if i["key"] == "onOffState"][0]
    assert on_item["uiValue"] == "Standby"


def test_status_endpoint_not_called_when_standby():
    """Fatal-if-touched: only the console-list call is queued, so a status
    fetch for a standby console would raise IndexError on an empty deque."""
    p = _plugin()
    p._auth = _FakeAuth()
    console = _console("C1", power_state="ConnectedStandby")
    p._api = FakeAPI().queue_get(_list_payload(console))
    dev = _console_device(13, "C1")
    p.deviceStartComm(dev)
    p._poll_consoles()             # must not raise — status never fetched
    assert dev.states["onOffState"] is False
    assert len(p._api.get_calls) == 1


# -- focused title resolution + aumid cache ------------------------------------

def _apps_payload():
    return {"result": [
        {"aumid": "GAME.Halo!App", "titleId": 111, "name": "Halo Infinite", "isGame": True},
    ], "status": {"errorCode": "OK"}}


def test_focused_title_resolved_via_installed_apps():
    p = _plugin()
    p._auth = _FakeAuth()
    console = _console("C1", power_state="On")
    p._api = (FakeAPI()
             .queue_get(_list_payload(console))
             .queue_get({"powerState": "On", "focusAppAumid": "GAME.Halo!App",
                        "status": {"errorCode": "OK"}})
             .queue_get(_apps_payload()))
    dev = _console_device(20, "C1")
    p.deviceStartComm(dev)
    p._poll_consoles()
    assert dev.states["focusedTitleName"] == "Halo Infinite"
    assert dev.states["focusedTitleId"] == "111"
    on_item = [i for i in dev.batches[-1] if i["key"] == "onOffState"][0]
    assert on_item["uiValue"] == "Halo Infinite"


def test_focused_title_blank_aumid_is_blank_no_installed_apps_call():
    p = _plugin()
    p._auth = _FakeAuth()
    console = _console("C1", power_state="On")
    p._api = (FakeAPI()
             .queue_get(_list_payload(console))
             .queue_get({"powerState": "On", "focusAppAumid": "", "status": {"errorCode": "OK"}}))
    dev = _console_device(21, "C1")
    p.deviceStartComm(dev)
    p._poll_consoles()             # must not raise — no installedApps call queued
    assert dev.states["focusedTitleName"] == ""
    assert dev.states["focusedTitleId"] == ""
    assert len(p._api.get_calls) == 2      # list + status only


def test_aumid_cache_second_poll_same_aumid_makes_no_installed_apps_call():
    p = _plugin()
    p._auth = _FakeAuth()
    console = _console("C1", power_state="On")
    p._api = (FakeAPI()
             .queue_get(_list_payload(console))
             .queue_get({"powerState": "On", "focusAppAumid": "GAME.Halo!App",
                        "status": {"errorCode": "OK"}})
             .queue_get(_apps_payload())
             .queue_get(_list_payload(console))
             .queue_get({"powerState": "On", "focusAppAumid": "GAME.Halo!App",
                        "status": {"errorCode": "OK"}}))
    dev = _console_device(22, "C1")
    p.deviceStartComm(dev)
    p._poll_consoles()
    calls_after_first = len(p._api.get_calls)
    p._poll_consoles()             # same aumid -> cached, no installedApps call queued/needed
    assert len(p._api.get_calls) == calls_after_first + 2   # list + status only, no apps call
    assert dev.states["focusedTitleName"] == "Halo Infinite"


def test_title_change_while_on_refreshes_display_text():
    """Still On, but the focused title changed: onOffState's uiValue must follow
    the title even though neither onOffState nor powerState changed."""
    p = _plugin()
    p._auth = _FakeAuth()
    console = _console("C1", power_state="On")
    p._api = (FakeAPI()
             .queue_get(_list_payload(console))
             .queue_get({"powerState": "On", "focusAppAumid": "", "status": {"errorCode": "OK"}})
             .queue_get(_list_payload(console))
             .queue_get({"powerState": "On", "focusAppAumid": "GAME.Halo!App",
                        "status": {"errorCode": "OK"}})
             .queue_get(_apps_payload()))
    dev = _console_device(23, "C1")
    p.deviceStartComm(dev)
    p._poll_consoles()             # On at the home menu
    p._poll_consoles()             # same power state, game now focused
    on_item = [i for i in dev.batches[-1] if i["key"] == "onOffState"][0]
    assert on_item["value"] is True
    assert on_item["uiValue"] == "Halo Infinite"


def test_status_fetch_failure_keeps_power_update_and_leaves_focus_as_is():
    p = _plugin()
    p._auth = _FakeAuth()
    on_console = _console("C1", power_state="On")
    p._api = (FakeAPI()
             .queue_get(_list_payload(on_console))
             .queue_get(oauth_error("boom", status=500)))   # status fetch fails
    dev = _console_device(23, "C1", states={"focusedTitleName": "Old Game",
                                            "focusedTitleId": "999"})
    p.deviceStartComm(dev)
    p._poll_consoles()
    assert dev.states["powerState"] == "On"       # power update NOT broken by the failure
    assert dev.states["onOffState"] is True
    assert dev.states["focusedTitleName"] == "Old Game"   # left exactly as it was
    assert dev.states["focusedTitleId"] == "999"


def test_installed_apps_failure_keeps_power_update_and_leaves_focus_as_is():
    p = _plugin()
    p._auth = _FakeAuth()
    console = _console("C1", power_state="On")
    p._api = (FakeAPI()
             .queue_get(_list_payload(console))
             .queue_get({"powerState": "On", "focusAppAumid": "GAME.New!App",
                        "status": {"errorCode": "OK"}})
             .queue_get(oauth_error("boom", status=500)))   # installedApps fetch fails
    dev = _console_device(24, "C1", states={"focusedTitleName": "Old Game",
                                            "focusedTitleId": "999"})
    p.deviceStartComm(dev)
    p._poll_consoles()
    assert dev.states["powerState"] == "On"
    assert dev.states["focusedTitleName"] == "Old Game"
    assert dev.states["focusedTitleId"] == "999"


# -- console missing from list / Unknown power state --------------------------

def test_console_missing_from_list_sets_error_not_off():
    p = _plugin()
    p._auth = _FakeAuth()
    p._api = FakeAPI().queue_get({"result": [], "status": {"errorCode": "OK"}})
    dev = _console_device(30, "MISSING-ID")
    p.deviceStartComm(dev)
    p._poll_consoles()
    assert dev.error_state == "console not found"
    assert "powerState" not in dev.states          # states untouched, not forced Off


def test_console_unknown_power_state_sets_unavailable_error():
    p = _plugin()
    p._auth = _FakeAuth()
    console = _console("C1", power_state="Unknown")
    p._api = FakeAPI().queue_get(_list_payload(console))
    dev = _console_device(31, "C1")
    p.deviceStartComm(dev)
    p._poll_consoles()
    assert dev.error_state == "unavailable"
    assert "powerState" not in dev.states


# -- console-list failure: error + backoff -------------------------------------

def test_console_list_transport_failure_errors_device_keeps_last_state():
    p = _plugin()
    p._auth = _FakeAuth()
    console = _console("C1", power_state="On")
    p._api = (FakeAPI()
             .queue_get(_list_payload(console))
             .queue_get({"powerState": "On", "focusAppAumid": "", "status": {"errorCode": "OK"}})
             .queue_get(oauth_error("boom", status=500)))
    dev = _console_device(40, "C1")
    p.deviceStartComm(dev)
    p._poll_consoles()
    assert dev.states["onOffState"] is True
    p._poll_consoles()             # console-list call now fails
    assert dev.states["onOffState"] is True        # last known state kept, not reset
    assert dev.error_state == "console list unavailable"


def test_console_list_failure_error_set_only_once_then_cleared_on_recovery():
    p = _plugin()
    p._auth = _FakeAuth()
    console = _console("C1", power_state="On")
    p._api = (FakeAPI()
             .queue_get(oauth_error("boom", status=500))
             .queue_get(oauth_error("boom", status=500))
             .queue_get(_list_payload(console))
             .queue_get({"powerState": "On", "focusAppAumid": "", "status": {"errorCode": "OK"}}))
    dev = _console_device(41, "C1")
    p.deviceStartComm(dev)
    p._poll_consoles()
    p._poll_consoles()
    assert dev.error_calls == ["console list unavailable"]   # set once, not every poll
    p._poll_consoles()             # recovers
    assert dev.error_state is None


def test_console_list_429_honours_retry_after():
    p = _plugin()
    p._auth = _FakeAuth()
    p._api = FakeAPI().queue_get(oauth_error("too_many", status=429, retry_after=45))
    dev = _console_device(42, "C1")
    p.deviceStartComm(dev)
    p._poll_consoles()
    assert p._console_backoff_until > 0
    delay = p._console_poll_delay()
    assert 44 <= delay <= 45.5


def test_console_list_failure_backoff_is_exponential_and_capped():
    p = _plugin()
    p._auth = _FakeAuth()
    p._api = FakeAPI()
    for _ in range(6):
        p._api.queue_get(oauth_error("boom", status=500))
    dev = _console_device(43, "C1")
    p.deviceStartComm(dev)
    delays = []
    for _ in range(6):
        p._poll_consoles()
        delays.append(p._console_poll_delay())
    # each failure's backoff should not decrease, and never exceed the cap
    assert all(d <= plugin.CONSOLE_BACKOFF_MAX for d in delays)
    assert delays[-1] == pytest.approx(plugin.CONSOLE_BACKOFF_MAX, abs=1)


def test_console_list_failure_resets_backoff_and_failure_count_on_success():
    p = _plugin()
    p._auth = _FakeAuth()
    console = _console("C1", power_state="On")
    p._api = (FakeAPI()
             .queue_get(oauth_error("boom", status=500))
             .queue_get(_list_payload(console))
             .queue_get({"powerState": "On", "focusAppAumid": "", "status": {"errorCode": "OK"}}))
    dev = _console_device(44, "C1")
    p.deviceStartComm(dev)
    p._poll_consoles()
    assert p._console_poll_failures == 1
    p._poll_consoles()
    assert p._console_poll_failures == 0
    assert p._console_backoff_until == 0.0


# -- no console devices: no API call at all ------------------------------------

def test_poll_consoles_skips_entirely_with_no_console_devices():
    p = _plugin()
    p._auth = _FakeAuth()
    p._api = FakeAPI()          # nothing queued — any call would raise IndexError
    p._poll_consoles()          # must not raise
    assert p._api.get_calls == []


# -- Status Request (actionControlUniversal) -----------------------------------

class _Action:
    def __init__(self, device_action):
        self.deviceAction = device_action


def test_status_request_polls_console_device_immediately():
    p = _plugin()
    p._auth = _FakeAuth()
    console = _console("C1", power_state="On")
    p._api = FakeAPI().queue_get(_list_payload(console)).queue_get(
        {"powerState": "On", "focusAppAumid": "", "status": {"errorCode": "OK"}})
    dev = _console_device(50, "C1")
    p.deviceStartComm(dev)
    p.actionControlUniversal(_Action(indigo.kUniversalAction.RequestStatus), dev)
    assert dev.states["onOffState"] is True


def test_status_request_for_presence_device_is_a_no_op():
    p = _plugin()
    dev = indigo.Device(id=51, deviceTypeId="xboxPresence", pluginProps={"xuid": "X1"})
    indigo.devices.add(dev)
    p.deviceStartComm(dev)
    # No api/auth wired — must not raise or attempt any polling.
    p.actionControlUniversal(_Action(indigo.kUniversalAction.RequestStatus), dev)


# -- power on / off actions -----------------------------------------------------

def test_power_on_sends_wake_up_and_pulls_poll_forward():
    p = _plugin()
    p._auth = _FakeAuth()
    p._api = FakeAPI().queue_post_json({"opId": "op1", "status": {"errorCode": "OK"}})
    dev = _console_device(60, "C1")
    p._next_console_due = 9999999999.0
    p.powerOnConsole(_Action(None), dev)
    call = p._api.post_json_calls[0]
    assert call["payload"]["command"] == xc.POWER_COMMAND_WAKE_UP
    assert call["payload"]["linkedXboxId"] == "C1"
    assert p._next_console_due < 9999999999.0     # pulled forward


def test_power_off_sends_turn_off():
    p = _plugin()
    p._auth = _FakeAuth()
    p._api = FakeAPI().queue_post_json({"opId": "op1", "status": {"errorCode": "OK"}})
    dev = _console_device(61, "C1")
    p.powerOffConsole(_Action(None), dev)
    call = p._api.post_json_calls[0]
    assert call["payload"]["command"] == xc.POWER_COMMAND_TURN_OFF


def test_power_action_logs_error_on_failure_and_does_not_raise(caplog):
    p = _plugin()
    p._auth = _FakeAuth()
    p._api = FakeAPI().queue_post_json(oauth_error("boom", status=500))
    dev = _console_device(62, "C1")
    with caplog.at_level(logging.ERROR):
        p.powerOnConsole(_Action(None), dev)      # must not raise
    assert any("power on failed" in r.getMessage() for r in caplog.records)


def test_power_action_without_console_id_logs_error_and_sends_nothing(caplog):
    p = _plugin()
    p._auth = _FakeAuth()
    p._api = FakeAPI()          # nothing queued — a send would raise IndexError
    dev = indigo.Device(id=63, name="dev63", deviceTypeId="xboxConsole", pluginProps={})
    with caplog.at_level(logging.ERROR):
        p.powerOnConsole(_Action(None), dev)
    assert p._api.post_json_calls == []
