"""Tests for the usage-charts layer: the plugin sync feeding the segment tracker,
the chart_data JSON endpoint shape, and static-page sanity."""
import json
from datetime import datetime
from pathlib import Path

import indigo   # the conftest fake
import plugin
import xb_history
from xb_presence import PersonPresence

PAGE = (Path(__file__).parent.parent / "Xbox.indigoPlugin" / "Contents" /
        "Resources" / "static" / "charts" / "index.html")


class _Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def _plugin():
    return plugin.Plugin("com.simons-plugins.indigo-xbox", "Xbox", "2026.1.0", {})


def _presence(xuid, gamertag="Gamer", state="Online", title="", title_id="",
              is_game=False):
    return PersonPresence(xuid=xuid, gamertag=gamertag, display_name=gamertag, state=state,
                          primary_title_text=title, primary_title_id=title_id,
                          is_game=is_game, device="", presence_text="")


def _device(dev_id, xuid, states=None, gamertag="G"):
    dev = indigo.Device(id=dev_id, name=f"dev{dev_id}", deviceTypeId="xboxPresence",
                        pluginProps={"xuid": xuid, "gamertag": gamertag}, states=states)
    indigo.devices.add(dev)
    return dev


def setup_function(_func):
    indigo.devices._devices.clear()   # pylint: disable=protected-access


# -- plugin sync feeds the segment tracker ------------------------------------

def test_sync_sequence_records_expected_segment_rows(tmp_path):
    p = _plugin()
    clock = _Clock(datetime(2026, 8, 4, 10, 0, 0))
    store = xb_history.HistoryStore(str(tmp_path / "history.sqlite"))
    p._history = store
    p._segments = xb_history.SegmentTracker(store=store, clock=clock)

    dev = _device(1, "X1")
    p.deviceStartComm(dev)

    # 10:00 start Halo → 10:20 switch to Forza → 10:35 stop
    p._sync_all([_presence("X1", title="Halo", title_id="1", is_game=True)])
    clock.now = datetime(2026, 8, 4, 10, 20, 0)
    p._sync_all([_presence("X1", title="Forza", title_id="2", is_game=True)])
    clock.now = datetime(2026, 8, 4, 10, 35, 0)
    p._sync_all([_presence("X1", state="Online", is_game=False)])

    rows = store.segments_since("2000-01-01")
    assert [(r["title_name"], r["minutes"]) for r in rows] == [("Halo", 20), ("Forza", 15)]
    assert all(r["xuid"] == "X1" for r in rows)


# -- chart_data endpoint shape ------------------------------------------------

def test_chart_data_shape_merges_states_and_history(tmp_path):
    p = _plugin()
    store = xb_history.HistoryStore(str(tmp_path / "history.sqlite"))
    p._history = store
    today = datetime.now().date().isoformat()
    store.insert_segment(xb_history.Segment("X1", "Ari", today + "T10:00:00",
                                            today + "T10:30:00", 30, "1", "Halo", today))

    dev = _device(1, "X1", gamertag="Ari", states={
        "online": True, "onOffState": True, "titleName": "Halo",
        "titleImageUrl": "boxart-from-data", "gamerPicUrl": "pic-from-data",
        "displayName": "Ari", "sessionMinutes": 12, "todayMinutes": 30,
    })
    p.deviceStartComm(dev)

    reply = p.http_chart_data(None)
    assert reply["status"] == 200
    body = json.loads(reply["content"])
    assert set(body) == {"people", "segments", "generated_at"}
    assert body["generated_at"]
    assert len(body["people"]) == 1
    person = body["people"][0]
    assert person["xuid"] == "X1"
    assert person["gamertag"] == "Ari"
    assert person["gamerPicUrl"] == "pic-from-data"
    assert person["live"] == {
        "online": True, "inGame": True, "titleName": "Halo",
        "titleImageUrl": "boxart-from-data", "sessionMinutes": 12, "todayMinutes": 30,
    }
    assert len(body["segments"]) == 1
    assert body["segments"][0]["title_name"] == "Halo"


def test_chart_data_no_history_store_returns_empty_segments():
    p = _plugin()
    p._history = None
    _device(1, "X1")
    p.deviceStartComm(indigo.devices[1])
    body = json.loads(p.http_chart_data(None)["content"])
    assert body["segments"] == []
    assert len(body["people"]) == 1


def test_chart_data_contains_no_secrets(tmp_path):
    p = _plugin()
    p._history = xb_history.HistoryStore(str(tmp_path / "history.sqlite"))
    _device(1, "X1", states={"online": True})
    p.deviceStartComm(indigo.devices[1])
    raw = p.http_chart_data(None)["content"].lower()
    for banned in ("token", "refresh", "secret", "bearer", "client_id", "password"):
        assert banned not in raw


# -- static page sanity -------------------------------------------------------

def test_charts_page_exists():
    assert PAGE.is_file()


def test_charts_page_has_no_external_urls():
    """The page must be fully self-contained: box art / gamerpics arrive in the
    JSON at runtime, so the HTML source itself carries no external URLs."""
    html = PAGE.read_text(encoding="utf-8")
    assert "http://" not in html
    assert "https://" not in html
    assert "//cdn" not in html and "src=\"//" not in html


def test_charts_page_targets_the_relative_endpoint():
    html = PAGE.read_text(encoding="utf-8")
    assert "message/com.simons-plugins.indigo-xbox/chart_data" in html
    assert "../../../message/com.simons-plugins.indigo-xbox/chart_data/" in html
