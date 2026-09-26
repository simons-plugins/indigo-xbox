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

EXPECTED_CONSOLE_STATES = {"powerState", "consoleName", "consoleType",
                           "focusedTitleName", "focusedTitleId", "lastPoll",
                           "lastPowerChange"}


def _root(path):
    return ET.parse(path).getroot()


def _device(device_id):
    for dev in _root(DEVICES_XML).findall("Device"):
        if dev.get("id") == device_id:
            return dev
    raise AssertionError(f"no Device id={device_id!r} in Devices.xml")


def test_devices_xml_parses():
    assert _root(DEVICES_XML).tag == "Devices"


def test_two_sensor_device_types():
    devices = _root(DEVICES_XML).findall("Device")
    assert len(devices) == 2
    assert {d.get("id") for d in devices} == {"xboxPresence", "xboxConsole"}
    assert all(d.get("type") == "sensor" for d in devices)


def test_supports_on_state_prop_present():
    for device_id in ("xboxPresence", "xboxConsole"):
        dev = _device(device_id)
        fields = {f.get("id") for f in dev.findall("ConfigUI/Field")}
        assert "SupportsOnState" in fields
        on_field = dev.find("ConfigUI/Field[@id='SupportsOnState']")
        assert on_field.get("defaultValue") == "true"


def test_config_ui_has_person_and_manual_fields():
    dev = _device("xboxPresence")
    fields = {f.get("id") for f in dev.findall("ConfigUI/Field")}
    assert "person" in fields
    assert "manualXuid" in fields
    person = dev.find("ConfigUI/Field[@id='person']")
    assert person.find("List").get("method") == "listPeople"


def test_config_ui_has_console_field():
    dev = _device("xboxConsole")
    fields = {f.get("id") for f in dev.findall("ConfigUI/Field")}
    assert "console" in fields
    console = dev.find("ConfigUI/Field[@id='console']")
    assert console.find("List").get("method") == "listConsoles"


def test_expected_states_present_and_unique():
    dev = _device("xboxPresence")
    ids = [s.get("id") for s in dev.findall("States/State")]
    assert len(ids) == len(set(ids))                      # unique
    assert set(ids) == EXPECTED_STATES


def test_expected_console_states_present_and_unique():
    dev = _device("xboxConsole")
    ids = [s.get("id") for s in dev.findall("States/State")]
    assert len(ids) == len(set(ids))                      # unique
    assert set(ids) == EXPECTED_CONSOLE_STATES


def test_state_ids_are_valid_identifiers():
    for device_id in ("xboxPresence", "xboxConsole"):
        dev = _device(device_id)
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
