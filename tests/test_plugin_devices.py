"""Tests for the device layer in plugin.py: bridge tracking, presence→state
sync (change-only writes, transitions, unknown xuid), and the ConfigUI."""
import logging
from datetime import datetime

import indigo   # the conftest fake
import plugin
import xb_constants as xc
import xb_sessions
from support import FakeAPI, oauth_error
from xb_presence import PersonPresence


def _plugin():
    return plugin.Plugin("com.simons-plugins.indigo-xbox", "Xbox", "2026.0.1", {})


def _presence(xuid, gamertag="Gamer", state="Online", title="", title_id="",
              is_game=False, device="", ptext=""):
    return PersonPresence(xuid=xuid, gamertag=gamertag, display_name=gamertag, state=state,
                          primary_title_text=title, primary_title_id=title_id,
                          is_game=is_game, device=device, presence_text=ptext)


def _device(dev_id, xuid):
    dev = indigo.Device(id=dev_id, name=f"dev{dev_id}", deviceTypeId="xboxPresence",
                        pluginProps={"xuid": xuid, "gamertag": "G"})
    indigo.devices.add(dev)
    return dev


def _start(p, dev):
    p.deviceStartComm(dev)


class _FakeAuth:
    """Stand-in for XboxAuth: authorized, with a fixed own identity."""

    def __init__(self, own=None, header="XBL3.0 x=uhs;tok"):
        self._own = own
        self._header = header
        self.invalidated = 0

    def is_authorized(self):
        return True

    def own_identity(self):
        return self._own

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


# -- tracking lifecycle -------------------------------------------------------

def test_device_start_and_stop_tracking():
    p = _plugin()
    dev = _device(1, "X1")
    _start(p, dev)
    assert p._tracked[dev.id] == "X1"
    p.deviceStopComm(dev)
    assert dev.id not in p._tracked


def test_comm_property_change_on_xuid():
    p = _plugin()
    old = indigo.Device(id=2, deviceTypeId="xboxPresence", pluginProps={"xuid": "A"})
    new = indigo.Device(id=2, deviceTypeId="xboxPresence", pluginProps={"xuid": "B"})
    assert p.didDeviceCommPropertyChange(old, new) is True
    same = indigo.Device(id=2, deviceTypeId="xboxPresence", pluginProps={"xuid": "A"})
    assert p.didDeviceCommPropertyChange(old, same) is False


# -- presence → state sync ----------------------------------------------------

def test_sync_in_game_sets_on_state_and_title():
    p = _plugin()
    dev = _device(3, "X1")
    _start(p, dev)
    p._sync_all([_presence("X1", state="Online", title="Halo Infinite",
                           title_id="111", is_game=True, device="Scarlett")])
    assert dev.states["onOffState"] is True
    assert dev.states["online"] is True
    assert dev.states["presenceState"] == "Online"
    assert dev.states["titleName"] == "Halo Infinite"
    assert dev.states["titleId"] == "111"
    assert dev.states["device"] == "Scarlett"
    on_item = [i for i in dev.batches[-1] if i["key"] == "onOffState"][0]
    assert on_item["uiValue"] == "Halo Infinite"        # display shows the game
    assert dev.states["lastPoll"]                        # heartbeat written


def test_sync_online_not_in_game_display_is_presence_state():
    p = _plugin()
    dev = _device(4, "X1")
    _start(p, dev)
    p._sync_all([_presence("X1", state="Online", is_game=False)])
    assert dev.states["onOffState"] is False
    on_item = [i for i in dev.batches[-1] if i["key"] == "onOffState"][0]
    assert on_item["uiValue"] == "Online"


def test_sync_only_writes_changed_states():
    p = _plugin()
    dev = _device(5, "X1")
    _start(p, dev)
    presence = _presence("X1", state="Online", title="Halo", title_id="1", is_game=True)
    p._sync_all([presence])
    batches_after_first = len(dev.batches)
    # An identical second poll must not write another state batch (SQL noise).
    p._sync_all([presence])
    assert len(dev.batches) == batches_after_first


def test_sync_title_change_refreshes_states():
    p = _plugin()
    dev = _device(6, "X1")
    _start(p, dev)
    p._sync_all([_presence("X1", state="Online", title="Halo", title_id="1", is_game=True)])
    batches = len(dev.batches)
    p._sync_all([_presence("X1", state="Online", title="Forza", title_id="2", is_game=True)])
    assert len(dev.batches) == batches + 1
    assert dev.states["titleName"] == "Forza"
    on_item = [i for i in dev.batches[-1] if i["key"] == "onOffState"][0]
    assert on_item["uiValue"] == "Forza"


def test_transition_to_offline_stamps_last_seen():
    p = _plugin()
    dev = _device(7, "X1")
    _start(p, dev)
    p._sync_all([_presence("X1", state="Online", title="Halo", is_game=True)])
    assert not dev.states.get("lastSeen")
    p._sync_all([_presence("X1", state="Offline", is_game=False)])
    assert dev.states["online"] is False
    assert dev.states["onOffState"] is False
    assert dev.states["presenceState"] == "Offline"
    assert dev.states["lastSeen"]                        # stamped on the online→offline edge


def test_unknown_xuid_sets_error_state():
    p = _plugin()
    dev = _device(8, "MISSING")
    _start(p, dev)
    p._sync_all([_presence("SOMEONE-ELSE")])
    assert dev.error_state == "gamertag not visible on this account"


def test_unknown_then_visible_clears_error():
    p = _plugin()
    dev = _device(9, "X1")
    _start(p, dev)
    p._sync_all([])                                      # not in the list → error
    assert dev.error_state is not None
    p._sync_all([_presence("X1", state="Online", is_game=False)])
    assert dev.error_state is None                       # cleared on recovery
    assert dev.states["presenceState"] == "Online"


def test_error_state_set_only_once():
    p = _plugin()
    dev = _device(10, "MISSING")
    _start(p, dev)
    p._sync_all([])
    p._sync_all([])
    # setErrorStateOnServer called once for the error (not every poll).
    assert dev.error_calls == ["gamertag not visible on this account"]


# -- ConfigUI -----------------------------------------------------------------

def test_list_people_from_last_poll():
    p = _plugin()
    p._last_people = [_presence("X2", gamertag="Zed"), _presence("X1", gamertag="Abe")]
    options = p.listPeople()
    assert [xuid for xuid, _ in options] == ["X1", "X2"]   # sorted by label
    assert options[0][1] == "Abe (X1)"


def test_list_people_empty_without_auth():
    p = _plugin()
    p._auth = None
    assert p.listPeople() == []


def test_list_people_prepends_me_when_authorized():
    p = _plugin()
    p._auth = _FakeAuth(own=("OWNXUID", "SimonG"))
    p._last_people = [_presence("X1", gamertag="Abe")]
    options = p.listPeople()
    assert options[0] == ("OWNXUID", "Me — SimonG")
    assert ("X1", "Abe (X1)") in options[1:]


def test_list_people_no_me_entry_without_own_identity():
    p = _plugin()
    p._auth = _FakeAuth(own=None)
    p._last_people = [_presence("X1", gamertag="Abe")]
    options = p.listPeople()
    assert options == [("X1", "Abe (X1)")]


def test_list_people_dedupes_own_xuid_from_peoplehub():
    """Defensive: peoplehub should never include the caller, but if it ever
    did, the 'Me' entry must win rather than showing the account twice."""
    p = _plugin()
    p._auth = _FakeAuth(own=("OWNXUID", "SimonG"))
    p._last_people = [_presence("OWNXUID", gamertag="SimonG"), _presence("X1", gamertag="Abe")]
    options = p.listPeople()
    assert options.count(("OWNXUID", "Me — SimonG")) == 1
    assert all(xuid != "OWNXUID" or label == "Me — SimonG" for xuid, label in options)


def test_validate_device_manual_xuid_wins():
    p = _plugin()
    ok, values = p.validateDeviceConfigUi(
        {"person": "FROM-MENU", "manualXuid": " 999 "}, "xboxPresence", 0)
    assert ok is True
    assert values["xuid"] == "999"


def test_validate_device_uses_menu_and_looks_up_gamertag():
    p = _plugin()
    p._last_people = [_presence("X1", gamertag="AriPlays")]
    ok, values = p.validateDeviceConfigUi(
        {"person": "X1", "manualXuid": ""}, "xboxPresence", 0)
    assert ok is True
    assert values["xuid"] == "X1"
    assert values["gamertag"] == "AriPlays"


def test_validate_device_requires_selection():
    p = _plugin()
    ok, _values, errors = p.validateDeviceConfigUi(
        {"person": "", "manualXuid": ""}, "xboxPresence", 0)
    assert ok is False
    assert "person" in errors


# -- self-tracking (own presence) ---------------------------------------------

def test_supervise_fetches_own_presence_for_self_device():
    p = _plugin()
    dev_self = _device(20, "OWNXUID")
    _start(p, dev_self)
    p._auth = _FakeAuth(own=("OWNXUID", "SimonG"))
    api = FakeAPI()
    api.queue_get({"people": []})              # peoplehub never lists the caller
    api.queue_get({"xuid": "OWNXUID", "state": "Online", "devices": [
        {"type": "XboxSeriesX", "titles": [
            {"id": "219630713", "name": "Halo Infinite", "placement": "Full", "state": "Active"},
        ]},
    ]})
    # A self device now also costs a profile call (gamerscore/tier/pic/name)…
    api.queue_get({"profileUsers": [{"id": "OWNXUID", "settings": [
        {"id": "Gamerscore", "value": "54321"},
        {"id": "AccountTier", "value": "Gold"},
    ]}]})
    # …and a titlehub box-art call (in-game, non-dashboard title).
    api.queue_get({"titles": [{"titleId": "219630713",
                               "displayImage": "http://img/halo.png"}]})
    p._api = api
    p._supervise()
    # peoplehub + own presence + profile + titlehub box art = 4 gets.
    assert len(api.get_calls) == 4
    assert dev_self.states["onOffState"] is True
    assert dev_self.states["titleName"] == "Halo Infinite"
    assert dev_self.states["gamerScore"] == 54321      # merged from profile
    assert dev_self.states["accountTier"] == "Gold"
    assert dev_self.states["titleImageUrl"] == "http://img/halo.png"


def test_supervise_skips_own_presence_when_no_self_device():
    p = _plugin()
    dev = _device(21, "SOMEONE")
    _start(p, dev)
    p._auth = _FakeAuth(own=("OWNXUID", "SimonG"))
    api = FakeAPI()
    api.queue_get({"people": [
        {"xuid": "SOMEONE", "gamertag": "G", "presenceState": "Offline"},
    ]})
    p._api = api
    p._supervise()
    assert len(api.get_calls) == 1             # no tracked device is the own xuid — no extra call


def test_supervise_skips_own_presence_without_own_identity():
    p = _plugin()
    dev_self = _device(22, "OWNXUID")
    _start(p, dev_self)
    p._auth = _FakeAuth(own=None)              # chain not derivable yet
    api = FakeAPI()
    api.queue_get({"people": []})
    p._api = api
    p._supervise()
    assert len(api.get_calls) == 1


def test_log_tracked_people_logs_own_presence_first_marked_me(caplog):
    p = _plugin()
    p._auth = _FakeAuth(own=("OWNXUID", "SimonG"))
    api = FakeAPI()
    api.queue_get({"xuid": "OWNXUID", "state": "Online", "devices": [
        {"type": "XboxSeriesX", "titles": [
            {"id": "219630713", "name": "Halo Infinite", "placement": "Full", "state": "Active"},
        ]},
    ]})
    api.queue_get({"people": [
        {"xuid": "X1", "gamertag": "Abe", "presenceState": "Offline"},
    ]})
    p._api = api
    with caplog.at_level(logging.INFO):
        p.logTrackedPeople()
    lines = [r.getMessage() for r in caplog.records]
    me_line = next(line for line in lines if "SimonG" in line)
    abe_line = next(line for line in lines if "Abe" in line)
    assert "(me)" in me_line
    assert "Halo Infinite" in me_line
    assert lines.index(me_line) < lines.index(abe_line)   # own presence logged first


# -- self-profile fetch cadence -----------------------------------------------

class _Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def test_self_profile_fetch_only_every_nth_poll():
    p = _plugin()
    p._auth = _FakeAuth(own=("OWN", "SimonG"))
    api = FakeAPI()
    profile_body = {"profileUsers": [{"id": "OWN", "settings": [
        {"id": "Gamerscore", "value": "100"}]}]}
    api.queue_get(profile_body)          # only two live fetches should ever occur
    api.queue_get(profile_body)

    # first call fetches
    info = p._maybe_fetch_self_profile(api, p._auth, "OWN")
    assert info.gamer_score == 100
    assert len(api.get_calls) == 1

    # the next N calls are served from cache (no extra gets)
    for _ in range(plugin.SELF_PROFILE_REFRESH_EVERY):
        p._maybe_fetch_self_profile(api, p._auth, "OWN")
    assert len(api.get_calls) == 1

    # the following call refreshes again
    p._maybe_fetch_self_profile(api, p._auth, "OWN")
    assert len(api.get_calls) == 2


def test_self_profile_fetch_failure_keeps_cache_and_never_raises():
    p = _plugin()
    p._auth = _FakeAuth(own=("OWN", "SimonG"))
    api = FakeAPI().queue_get(oauth_error("boom", status=500))
    # Must not raise, returns the (empty) cache.
    assert p._maybe_fetch_self_profile(api, p._auth, "OWN") is None


# -- title box art cache ------------------------------------------------------

def _self_plugin_with_api(api):
    p = _plugin()
    p._auth = _FakeAuth(own=("OWN", "SimonG"))
    p._api = api
    return p


def test_title_image_fetched_once_per_title_and_cached():
    api = FakeAPI().queue_get({"titles": [{"titleId": "111",
                                           "displayImage": "http://box/111.png"}]})
    p = _self_plugin_with_api(api)
    dev = _device(30, "X1")
    _start(p, dev)
    presence = _presence("X1", state="Online", title="Halo", title_id="111", is_game=True)
    p._sync_all([presence])
    assert dev.states["titleImageUrl"] == "http://box/111.png"
    assert len(api.get_calls) == 1
    # second poll, same titleId → served from cache, no new titlehub call
    p._sync_all([presence])
    assert len(api.get_calls) == 1


def test_title_image_failure_leaves_state_empty_and_caches():
    api = FakeAPI().queue_get(oauth_error("nope", status=404))
    p = _self_plugin_with_api(api)
    dev = _device(31, "X1")
    _start(p, dev)
    presence = _presence("X1", state="Online", title="Halo", title_id="222", is_game=True)
    p._sync_all([presence])
    assert dev.states["titleImageUrl"] == ""      # failure → empty, poll not broken
    assert len(api.get_calls) == 1
    p._sync_all([presence])
    assert len(api.get_calls) == 1                # failure cached; not retried


def test_title_image_skipped_for_dashboard_title():
    api = FakeAPI()
    p = _self_plugin_with_api(api)
    dev = _device(32, "X1")
    _start(p, dev)
    # dashboard-only online is not in_game, so no box art fetch at all
    p._sync_all([_presence("X1", state="Online", title_id=xc.DASHBOARD_TITLE_ID,
                           is_game=False)])
    assert dev.states["titleImageUrl"] == ""
    assert len(api.get_calls) == 0


# -- session stats written into device states (change-only) -------------------

def test_session_states_written_and_change_only():
    p = _plugin()
    clock = _Clock(datetime(2026, 8, 4, 10, 0, 0))
    p._sessions = xb_sessions.SessionAccountant(clock=clock)
    dev = _device(40, "X1")
    _start(p, dev)

    presence = _presence("X1", state="Online", title="Halo", title_id="1", is_game=True)
    p._sync_all([presence])
    assert dev.states["sessionStartedAt"] == datetime(2026, 8, 4, 10, 0, 0).isoformat()
    assert dev.states["sessionMinutes"] == 0
    assert dev.states["todayMinutes"] == 0

    # same minute, same presence → no new state batch (change-only)
    batches = len(dev.batches)
    p._sync_all([presence])
    assert len(dev.batches) == batches

    # 15 live minutes later → sessionMinutes updates
    clock.now = datetime(2026, 8, 4, 10, 15, 0)
    p._sync_all([presence])
    assert dev.states["sessionMinutes"] == 15
    assert dev.states["todayMinutes"] == 15

    # stop playing → session closes, last/today recorded
    clock.now = datetime(2026, 8, 4, 10, 20, 0)
    p._sync_all([_presence("X1", state="Online", is_game=False)])
    assert dev.states["sessionStartedAt"] == ""
    assert dev.states["sessionMinutes"] == 0
    assert dev.states["lastSessionMinutes"] == 20
    assert dev.states["todayMinutes"] == 20


# -- poll interval coercion ---------------------------------------------------

def test_poll_interval_clamped():
    assert plugin._coerce_interval("5") == plugin.MIN_POLL_INTERVAL
    assert plugin._coerce_interval("9999") == plugin.MAX_POLL_INTERVAL
    assert plugin._coerce_interval("nonsense") == plugin.DEFAULT_POLL_INTERVAL
    assert plugin._coerce_interval("120") == 120


# -- consolePollInterval validation --------------------------------------------

def _base_prefs(**overrides):
    values = {"clientId": "abc", "pollInterval": "60", "consolePollInterval": "60"}
    values.update(overrides)
    return values


def test_console_poll_interval_valid_passes():
    p = _plugin()
    result = p.validatePrefsConfigUi(_base_prefs(consolePollInterval="30"))
    assert result[0] is True


def test_console_poll_interval_missing_defaults_ok():
    """No consolePollInterval key at all (e.g. an old prefs dict) must not
    fail validation — the code falls back to the default before checking."""
    p = _plugin()
    values = _base_prefs()
    del values["consolePollInterval"]
    result = p.validatePrefsConfigUi(values)
    assert result[0] is True


def test_console_poll_interval_non_int_fails():
    p = _plugin()
    ok, _values, errors = p.validatePrefsConfigUi(_base_prefs(consolePollInterval="nonsense"))
    assert ok is False
    assert "consolePollInterval" in errors


def test_console_poll_interval_below_min_fails():
    p = _plugin()
    ok, _values, errors = p.validatePrefsConfigUi(_base_prefs(consolePollInterval="14"))
    assert ok is False
    assert "consolePollInterval" in errors


def test_console_poll_interval_above_max_fails():
    p = _plugin()
    ok, _values, errors = p.validatePrefsConfigUi(_base_prefs(consolePollInterval="601"))
    assert ok is False
    assert "consolePollInterval" in errors


def test_closed_prefs_config_ui_updates_console_poll_interval():
    p = _plugin()
    p.closedPrefsConfigUi(_base_prefs(consolePollInterval="45"), userCancelled=False)
    assert p._console_poll_interval == 45
