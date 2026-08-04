"""Tests for xb_presence.py — pure parsing of peoplehub presence + fetch."""
import pytest

import xb_constants as xc
import xb_presence
from xb_api import XboxError
from support import FakeAPI, load_fixture, oauth_error


def _by_gt(people):
    return {p.gamertag: p for p in people}


# -- parse_people against the fixture -----------------------------------------

def test_parse_people_count():
    people = xb_presence.parse_people(load_fixture("people_presence.json"))
    assert len(people) == 5


def test_online_in_game_primary_detail():
    people = _by_gt(xb_presence.parse_people(load_fixture("people_presence.json")))
    ari = people["AriPlays"]
    assert ari.xuid == "1111111111111111"
    assert ari.online is True
    assert ari.is_game is True
    assert ari.in_game is True
    assert ari.primary_title_text == "Halo Infinite"
    assert ari.primary_title_id == "2043051478"
    assert ari.device == "Scarlett"
    assert ari.state == xc.PRESENCE_ONLINE


def test_online_not_in_game():
    bex = _by_gt(xb_presence.parse_people(load_fixture("people_presence.json")))["BexTheBest"]
    assert bex.online is True
    assert bex.is_game is False
    assert bex.in_game is False       # online, but not a game detail


def test_offline_with_null_details():
    cai = _by_gt(xb_presence.parse_people(load_fixture("people_presence.json")))["CaiOffline"]
    assert cai.online is False
    assert cai.state == xc.PRESENCE_OFFLINE
    assert cai.device == ""
    assert cai.primary_title_id == ""
    assert cai.is_game is False


def test_offline_with_empty_details():
    dee = _by_gt(xb_presence.parse_people(load_fixture("people_presence.json")))["DeeAway"]
    assert dee.online is False
    assert dee.primary_title_text == ""


def test_selects_game_detail_when_no_primary_game():
    """Eli has two non-primary details; the game one (Minecraft) must win."""
    eli = _by_gt(xb_presence.parse_people(load_fixture("people_presence.json")))["EliMulti"]
    assert eli.is_game is True
    assert eli.primary_title_text == "Minecraft"
    assert eli.primary_title_id == "1810924247"
    assert eli.in_game is True


# -- parse robustness ---------------------------------------------------------

def test_parse_people_missing_array_is_empty():
    assert xb_presence.parse_people({}) == []
    assert xb_presence.parse_people({"people": None}) == []
    assert xb_presence.parse_people(None) == []


def test_parse_person_missing_fields_defaults_offline():
    person = xb_presence.parse_person({"xuid": "9", "gamertag": "G"})
    assert person.state == xc.PRESENCE_OFFLINE
    assert person.display_name == ""
    assert person.is_game is False


# -- parse_own_presence (userpresence /users/me) -------------------------------

def _device_record(device_type, titles):
    return {"type": device_type, "titles": titles}


def _title(title_id, name, state="Active", placement="Full"):
    return {"id": title_id, "name": name, "state": state, "placement": placement}


def test_own_presence_in_game():
    payload = {
        "xuid": "9999999999999999",
        "state": "Online",
        "devices": [_device_record("XboxSeriesX", [
            _title(xc.DASHBOARD_TITLE_ID, "Home", placement="Fill"),
            _title("219630713", "Halo Infinite"),
        ])],
    }
    me = xb_presence.parse_own_presence(payload, own_gamertag="SimonG")
    assert me.xuid == "9999999999999999"
    assert me.gamertag == "SimonG"
    assert me.online is True
    assert me.is_game is True
    assert me.in_game is True
    assert me.primary_title_text == "Halo Infinite"
    assert me.primary_title_id == "219630713"
    assert me.device == "XboxSeriesX"


def test_own_presence_dashboard_only_is_online_not_in_game():
    """Online with only the dashboard Active must not read as 'in a game'."""
    payload = {
        "xuid": "9999999999999999",
        "state": "Online",
        "devices": [_device_record("XboxSeriesX", [
            _title(xc.DASHBOARD_TITLE_ID, "Home"),
        ])],
    }
    me = xb_presence.parse_own_presence(payload, own_gamertag="SimonG")
    assert me.online is True
    assert me.is_game is False
    assert me.in_game is False
    assert me.primary_title_text == ""
    assert me.device == "XboxSeriesX"          # still reports which console is on


def test_own_presence_offline_with_last_seen():
    payload = {
        "xuid": "9999999999999999",
        "state": "Offline",
        "lastSeen": {
            "deviceType": "XboxOne",
            "titleId": "1234500000",
            "titleName": "Forza Horizon 5",
            "timestamp": "2026-08-01T12:00:00.0000000Z",
        },
    }
    me = xb_presence.parse_own_presence(payload, own_gamertag="SimonG")
    assert me.online is False
    assert me.is_game is False
    assert me.primary_title_text == "Forza Horizon 5"
    assert me.primary_title_id == "1234500000"
    assert me.device == "XboxOne"


def test_own_presence_offline_without_last_seen():
    me = xb_presence.parse_own_presence({"xuid": "1", "state": "Offline"},
                                        own_gamertag="SimonG")
    assert me.online is False
    assert me.primary_title_text == ""
    assert me.primary_title_id == ""
    assert me.device == ""


def test_own_presence_online_without_devices():
    me = xb_presence.parse_own_presence({"xuid": "1", "state": "Online"},
                                        own_gamertag="SimonG")
    assert me.online is True
    assert me.is_game is False
    assert me.device == ""


def test_own_presence_falls_back_to_own_xuid_when_payload_omits_it():
    me = xb_presence.parse_own_presence({"state": "Offline"}, own_xuid="42", own_gamertag="G")
    assert me.xuid == "42"


# -- fetch_own_presence orchestration ------------------------------------------

def test_fetch_own_presence_returns_none_when_unauthorized():
    api = FakeAPI()
    assert xb_presence.fetch_own_presence(api, _Auth(header=None)) is None


def test_fetch_own_presence_parses_payload_and_sends_headers():
    api = FakeAPI().queue_get({"xuid": "42", "state": "Offline"})
    me = xb_presence.fetch_own_presence(api, _Auth(), own_gamertag="SimonG")
    assert me.xuid == "42"
    assert me.gamertag == "SimonG"
    call = api.get_calls[0]
    assert call["url"] == xc.OWN_PRESENCE_URL
    assert call["headers"]["Authorization"] == "XBL3.0 x=uhs;tok"
    assert call["headers"]["x-xbl-contract-version"] == xc.OWN_PRESENCE_CONTRACT_VERSION


def test_fetch_own_presence_retries_once_on_401():
    api = FakeAPI()
    api.queue_get(oauth_error("unauthorized", status=401))
    api.queue_get({"xuid": "42", "state": "Online"})
    auth = _Auth()
    me = xb_presence.fetch_own_presence(api, auth)
    assert me.xuid == "42"
    assert auth.invalidated == 1


def test_fetch_own_presence_reraises_non_401():
    api = FakeAPI().queue_get(oauth_error("too_many", status=429, retry_after=30))
    with pytest.raises(XboxError) as excinfo:
        xb_presence.fetch_own_presence(api, _Auth())
    assert excinfo.value.status == 429


# -- fetch_presence orchestration ---------------------------------------------

class _Auth:
    def __init__(self, header="XBL3.0 x=uhs;tok"):
        self._header = header
        self.invalidated = 0

    def xbl_header(self):
        return self._header

    def invalidate_xbl(self):
        self.invalidated += 1


def test_fetch_presence_returns_none_when_unauthorized():
    api = FakeAPI()
    assert xb_presence.fetch_presence(api, _Auth(header=None)) is None


def test_fetch_presence_parses_payload():
    api = FakeAPI().queue_get(load_fixture("people_presence.json"))
    people = xb_presence.fetch_presence(api, _Auth())
    assert len(people) == 5
    call = api.get_calls[0]
    assert call["headers"]["Authorization"] == "XBL3.0 x=uhs;tok"
    assert call["headers"]["x-xbl-contract-version"] == xc.PEOPLE_CONTRACT_VERSION
    assert call["headers"]["Accept-Language"] == xc.ACCEPT_LANGUAGE


def test_fetch_presence_retries_once_on_401():
    api = FakeAPI()
    api.queue_get(oauth_error("unauthorized", status=401))
    api.queue_get(load_fixture("people_presence.json"))
    auth = _Auth()
    people = xb_presence.fetch_presence(api, auth)
    assert len(people) == 5
    assert auth.invalidated == 1           # dropped the stale XSTS token before retry


def test_fetch_presence_reraises_non_401():
    api = FakeAPI().queue_get(oauth_error("too_many", status=429, retry_after=30))
    with pytest.raises(XboxError) as excinfo:
        xb_presence.fetch_presence(api, _Auth())
    assert excinfo.value.status == 429


def test_fetch_presence_uses_expanded_decoration_url():
    api = FakeAPI().queue_get(load_fixture("people_decorated.json"))
    xb_presence.fetch_presence(api, _Auth())
    url = api.get_calls[0]["url"]
    assert "detail,multiplayersummary,presencedetail" in url


# -- new decoration parsing (detail / multiplayersummary + PascalCase nested) --

def test_parse_person_rich_presence_and_broadcast_and_multiplayer():
    ari = _by_gt(xb_presence.parse_people(load_fixture("people_decorated.json")))["AriPlays"]
    assert ari.in_game is True
    assert ari.rich_presence_text == "Big Team Battle"    # PascalCase RichPresenceText
    assert ari.is_broadcasting is True                    # PascalCase IsBroadcasting
    assert ari.in_multiplayer is True                     # InMultiplayerSession == 1 > 0
    assert ari.gamer_score == 27210                       # camelCase gamerScore string → int
    assert ari.account_tier == "Gold"                     # camelCase detail.accountTier
    assert ari.gamer_pic_url == "http://pic/ari.png"      # camelCase displayPicRaw


def test_parse_person_rich_presence_falls_back_to_presence_text():
    """When a game detail has no RichPresenceText, the PresenceText stands in."""
    person = xb_presence.parse_person({
        "xuid": "7", "gamertag": "G", "presenceState": "Online",
        "presenceDetails": [{"Device": "Scarlett", "PresenceText": "Forza",
                             "State": "Active", "TitleId": "42",
                             "IsPrimary": True, "IsGame": True}],
    })
    assert person.rich_presence_text == "Forza"
    assert person.is_broadcasting is False


def test_parse_person_not_in_multiplayer_when_zero():
    bex = _by_gt(xb_presence.parse_people(load_fixture("people_decorated.json")))["BexTheBest"]
    assert bex.in_multiplayer is False                    # InMultiplayerSession == 0
    assert bex.account_tier == "Silver"
    assert bex.gamer_score == 3802


def test_parse_person_defaults_when_decorations_absent():
    """The bare fixture (no detail / multiplayerSummary) yields empty extras."""
    ari = _by_gt(xb_presence.parse_people(load_fixture("people_presence.json")))["AriPlays"]
    assert ari.gamer_score == 0
    assert ari.account_tier == ""
    assert ari.in_multiplayer is False
    assert ari.rich_presence_text == "Big Team Battle"    # RichPresenceText present here


def test_own_presence_rich_presence_from_activity():
    payload = {
        "xuid": "9", "state": "Online",
        "devices": [{"type": "XboxSeriesX", "titles": [
            {"id": "219630713", "name": "Halo Infinite", "state": "Active",
             "placement": "Full", "activity": [{"richPresence": "Ranked Arena"}]},
        ]}],
    }
    me = xb_presence.parse_own_presence(payload, own_gamertag="SimonG")
    assert me.rich_presence_text == "Ranked Arena"


# -- profile extras (parse + fetch) -------------------------------------------

def test_parse_profile_reads_settings_case_insensitively():
    payload = {"profileUsers": [{"id": "9", "settings": [
        {"id": "Gamerscore", "value": "12345"},
        {"id": "AccountTier", "value": "Gold"},
        {"id": "GameDisplayPicRaw", "value": "http://pic/me.png"},   # capital P on the wire
        {"id": "GameDisplayName", "value": "Simon"},
    ]}]}
    info = xb_presence.parse_profile(payload)
    assert info.gamer_score == 12345
    assert info.account_tier == "Gold"
    assert info.gamer_pic_url == "http://pic/me.png"
    assert info.display_name == "Simon"


def test_parse_profile_empty_shape_returns_none():
    assert xb_presence.parse_profile({}) is None
    assert xb_presence.parse_profile({"profileUsers": []}) is None
    assert xb_presence.parse_profile({"profileUsers": [{"id": "9"}]}) is None


def test_fetch_profile_sends_headers_and_url():
    api = FakeAPI().queue_get({"profileUsers": [{"id": "9", "settings": [
        {"id": "Gamerscore", "value": "7"}]}]})
    info = xb_presence.fetch_profile(api, _Auth(), "9")
    assert info.gamer_score == 7
    call = api.get_calls[0]
    assert "profile.xboxlive.com/users/xuid(9)/profile/settings" in call["url"]
    assert call["headers"]["x-xbl-contract-version"] == xc.PROFILE_CONTRACT_VERSION


def test_fetch_profile_retries_once_on_401():
    api = FakeAPI()
    api.queue_get(oauth_error("unauthorized", status=401))
    api.queue_get({"profileUsers": [{"id": "9", "settings": [
        {"id": "Gamerscore", "value": "5"}]}]})
    auth = _Auth()
    info = xb_presence.fetch_profile(api, auth, "9")
    assert info.gamer_score == 5
    assert auth.invalidated == 1


def test_apply_profile_overlays_without_clobbering():
    base = xb_presence.PersonPresence(
        xuid="9", gamertag="G", display_name="G", state="Online",
        primary_title_text="", primary_title_id="", is_game=False, device="",
        presence_text="")
    merged = xb_presence.apply_profile(base, xb_presence.ProfileInfo(
        gamer_score=99, account_tier="Gold", gamer_pic_url="http://p", display_name="Simon"))
    assert merged.gamer_score == 99
    assert merged.account_tier == "Gold"
    assert merged.display_name == "Simon"
    # empty profile fields never overwrite existing values
    kept = xb_presence.apply_profile(base, xb_presence.ProfileInfo())
    assert kept.display_name == "G"


# -- title box art (parse + fetch) --------------------------------------------

def test_parse_title_image_prefers_display_image():
    url = xb_presence.parse_title_image(load_fixture_local("titlehub_titleinfo.json"))
    assert url.startswith("http")


def test_parse_title_image_falls_back_to_box_art_by_type():
    payload = {"titles": [{"titleId": "1", "images": [
        {"url": "http://logo", "type": "Logo"},
        {"url": "http://box", "type": "BoxArt"},
    ]}]}
    assert xb_presence.parse_title_image(payload) == "http://box"   # BoxArt beats Logo


def test_parse_title_image_empty_when_no_titles():
    assert xb_presence.parse_title_image({"titles": []}) == ""
    assert xb_presence.parse_title_image({}) == ""


def test_fetch_title_image_sends_titlehub_headers():
    api = FakeAPI().queue_get({"titles": [{"titleId": "42",
                                           "displayImage": "http://img"}]})
    url = xb_presence.fetch_title_image(api, _Auth(), "OWNXUID", "42")
    assert url == "http://img"
    call = api.get_calls[0]
    assert "titlehub.xboxlive.com/users/xuid(OWNXUID)/titles/titleid(42)" in call["url"]
    assert call["headers"]["x-xbl-contract-version"] == xc.TITLEHUB_CONTRACT_VERSION
    assert call["headers"]["x-xbl-client-name"] == xc.TITLEHUB_CLIENT_NAME
