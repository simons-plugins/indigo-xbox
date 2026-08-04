"""Structural validation of Devices.xml, PluginConfig.xml and MenuItems.xml."""
import xml.etree.ElementTree as ET
from pathlib import Path

SERVER_PLUGIN = (
    Path(__file__).parent.parent / "Xbox.indigoPlugin" / "Contents" / "Server Plugin"
)
DEVICES_XML = SERVER_PLUGIN / "Devices.xml"
CONFIG_XML = SERVER_PLUGIN / "PluginConfig.xml"
MENU_XML = SERVER_PLUGIN / "MenuItems.xml"

EXPECTED_STATES = {"online", "presenceState", "titleName", "titleId",
                   "device", "lastSeen", "lastPoll",
                   # session stats
                   "sessionStartedAt", "sessionMinutes", "lastSessionMinutes",
                   "todayMinutes",
                   # rich presence detail
                   "richPresenceText", "isBroadcasting", "inMultiplayer",
                   # profile extras
                   "gamerScore", "accountTier", "gamerPicUrl", "displayName",
                   # title box art
                   "titleImageUrl"}


def _root(path):
    return ET.parse(path).getroot()


def test_devices_xml_parses():
    assert _root(DEVICES_XML).tag == "Devices"


def test_single_sensor_device_type():
    devices = _root(DEVICES_XML).findall("Device")
    assert len(devices) == 1
    dev = devices[0]
    assert dev.get("id") == "xboxPresence"
    assert dev.get("type") == "sensor"


def test_supports_on_state_prop_present():
    dev = _root(DEVICES_XML).find("Device")
    fields = {f.get("id") for f in dev.findall("ConfigUI/Field")}
    assert "SupportsOnState" in fields
    on_field = dev.find("ConfigUI/Field[@id='SupportsOnState']")
    assert on_field.get("defaultValue") == "true"


def test_config_ui_has_person_and_manual_fields():
    dev = _root(DEVICES_XML).find("Device")
    fields = {f.get("id") for f in dev.findall("ConfigUI/Field")}
    assert "person" in fields
    assert "manualXuid" in fields
    person = dev.find("ConfigUI/Field[@id='person']")
    assert person.find("List").get("method") == "listPeople"


def test_expected_states_present_and_unique():
    dev = _root(DEVICES_XML).find("Device")
    ids = [s.get("id") for s in dev.findall("States/State")]
    assert len(ids) == len(set(ids))                      # unique
    assert set(ids) == EXPECTED_STATES


def test_state_ids_are_valid_identifiers():
    dev = _root(DEVICES_XML).find("Device")
    for state in dev.findall("States/State"):
        sid = state.get("id")
        assert sid and sid[0].isascii() and sid[0].isalpha(), sid
        assert all(ch.isascii() and ch.isalnum() for ch in sid), sid


def test_plugin_config_has_client_and_auth_fields():
    fields = {f.get("id") for f in _root(CONFIG_XML).findall("Field")}
    for required in ("clientId", "pollInterval", "authButton",
                     "authInstructions", "authUserCode", "authStatus"):
        assert required in fields, required
    button = _root(CONFIG_XML).find("Field[@id='authButton']")
    assert button.find("CallbackMethod").text == "authorizeButtonPressed"


def test_menu_item_present():
    items = _root(MENU_XML).findall("MenuItem")
    assert len(items) == 1
    assert items[0].get("id") == "logTrackedPeople"
    assert items[0].find("CallbackMethod").text == "logTrackedPeople"
