"""Tests for xb_history.py — the play-segment tracker (title switch closes a
segment, midnight split, per-op sqlite connections) and the SQLite store."""
import sqlite3
import threading
from datetime import datetime

import xb_history


class _Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def _at(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, 0)


def _store(tmp_path):
    return xb_history.HistoryStore(str(tmp_path / "sub" / "history.sqlite"))


# -- segment lifecycle --------------------------------------------------------

def test_no_row_written_while_segment_open(tmp_path):
    clock = _Clock(_at(2026, 8, 4, 10, 0))
    store = _store(tmp_path)
    tracker = xb_history.SegmentTracker(store=store, clock=clock)
    tracker.update("X", True, "1", "Halo", "Ari")
    clock.now = _at(2026, 8, 4, 10, 30)
    tracker.update("X", True, "1", "Halo", "Ari")           # still playing, same title
    assert store.segments_since("2000-01-01") == []          # nothing on close yet
    assert tracker.open_xuids == {"X"}


def test_stop_playing_writes_one_segment(tmp_path):
    clock = _Clock(_at(2026, 8, 4, 10, 0))
    store = _store(tmp_path)
    tracker = xb_history.SegmentTracker(store=store, clock=clock)
    tracker.update("X", True, "1", "Halo", "Ari")
    clock.now = _at(2026, 8, 4, 10, 45)
    tracker.update("X", False, "", "", "Ari")                # closes 10:00 → 10:45
    rows = store.segments_since("2000-01-01")
    assert len(rows) == 1
    row = rows[0]
    assert row["minutes"] == 45
    assert row["title_id"] == "1"
    assert row["title_name"] == "Halo"
    assert row["gamertag"] == "Ari"
    assert row["date"] == "2026-08-04"
    assert row["started_at"] == _at(2026, 8, 4, 10, 0).isoformat()
    assert row["ended_at"] == _at(2026, 8, 4, 10, 45).isoformat()


def test_title_switch_closes_and_opens_new_segment(tmp_path):
    clock = _Clock(_at(2026, 8, 4, 20, 0))
    store = _store(tmp_path)
    tracker = xb_history.SegmentTracker(store=store, clock=clock)
    tracker.update("X", True, "1", "Halo", "Ari")
    clock.now = _at(2026, 8, 4, 20, 20)
    tracker.update("X", True, "2", "Forza", "Ari")           # switch → close Halo (20m)
    assert [r["title_name"] for r in store.segments_since("2000-01-01")] == ["Halo"]
    assert store.segments_since("2000-01-01")[0]["minutes"] == 20
    clock.now = _at(2026, 8, 4, 20, 50)
    tracker.update("X", False, "", "", "Ari")                # close Forza (30m)
    rows = store.segments_since("2000-01-01")
    assert [r["title_name"] for r in rows] == ["Halo", "Forza"]
    assert rows[1]["minutes"] == 30
    assert rows[1]["title_id"] == "2"


def test_midnight_spanning_segment_splits_into_two_rows(tmp_path):
    clock = _Clock(_at(2026, 8, 4, 23, 40))
    store = _store(tmp_path)
    tracker = xb_history.SegmentTracker(store=store, clock=clock)
    tracker.update("X", True, "1", "Halo", "Ari")
    clock.now = _at(2026, 8, 5, 0, 20)
    tracker.update("X", False, "", "", "Ari")                # 23:40 → 00:20 spans midnight
    rows = store.segments_since("2000-01-01")
    assert len(rows) == 2
    assert rows[0]["date"] == "2026-08-04" and rows[0]["minutes"] == 20   # 23:40 → 00:00
    assert rows[1]["date"] == "2026-08-05" and rows[1]["minutes"] == 20   # 00:00 → 00:20
    assert rows[0]["ended_at"] == _at(2026, 8, 5, 0, 0).isoformat()
    assert rows[1]["started_at"] == _at(2026, 8, 5, 0, 0).isoformat()


def test_sub_minute_segment_writes_no_row(tmp_path):
    clock = _Clock(datetime(2026, 8, 4, 10, 0, 0))
    store = _store(tmp_path)
    tracker = xb_history.SegmentTracker(store=store, clock=clock)
    tracker.update("X", True, "1", "Halo", "Ari")
    clock.now = datetime(2026, 8, 4, 10, 0, 40)              # 40 seconds
    tracker.update("X", False, "", "", "Ari")
    assert store.segments_since("2000-01-01") == []


def test_flush_closes_open_segments(tmp_path):
    clock = _Clock(_at(2026, 8, 4, 10, 0))
    store = _store(tmp_path)
    tracker = xb_history.SegmentTracker(store=store, clock=clock)
    tracker.update("X", True, "1", "Halo", "Ari")
    clock.now = _at(2026, 8, 4, 10, 12)
    tracker.flush()
    assert store.segments_since("2000-01-01")[0]["minutes"] == 12
    assert tracker.open_xuids == set()


def test_store_none_tracks_but_does_not_persist():
    clock = _Clock(_at(2026, 8, 4, 10, 0))
    tracker = xb_history.SegmentTracker(store=None, clock=clock)
    tracker.update("X", True, "1", "Halo", "Ari")           # tracked in memory
    assert tracker.open_xuids == {"X"}
    clock.now = _at(2026, 8, 4, 10, 30)
    tracker.update("X", False, "", "", "Ari")               # close is a no-op write
    assert tracker.open_xuids == set()                      # no crash without a store


# -- store: prune, per-op connections -----------------------------------------

def test_prune_drops_rows_older_than_keep_days(tmp_path):
    store = _store(tmp_path)
    old = xb_history.Segment("X", "Ari", "2025-01-01T10:00:00", "2025-01-01T11:00:00",
                             60, "1", "Halo", "2025-01-01")
    recent = xb_history.Segment("X", "Ari", "2026-08-01T10:00:00", "2026-08-01T11:00:00",
                                60, "1", "Halo", "2026-08-01")
    store.insert_segment(old)
    store.insert_segment(recent)
    removed = store.prune(keep_days=30, now=datetime(2026, 8, 4, 12, 0))
    assert removed == 1
    remaining = store.segments_since("2000-01-01")
    assert [r["date"] for r in remaining] == ["2026-08-01"]


def test_segments_since_filters_by_date(tmp_path):
    store = _store(tmp_path)
    for date in ("2026-07-01", "2026-08-01", "2026-08-03"):
        store.insert_segment(xb_history.Segment("X", "Ari", date + "T10:00:00",
                                                date + "T10:30:00", 30, "1", "Halo", date))
    got = [r["date"] for r in store.segments_since("2026-08-01")]
    assert got == ["2026-08-01", "2026-08-03"]


def test_store_uses_short_lived_connections_no_shared_handle(tmp_path):
    """Each op opens+closes its own connection — there is no cached connection
    attribute the poll and IWS threads could share."""
    store = _store(tmp_path)
    assert not any(isinstance(v, sqlite3.Connection) for v in vars(store).values())
    # writes from one thread are visible to a read on another thread.
    seg = xb_history.Segment("X", "Ari", "2026-08-04T10:00:00", "2026-08-04T10:30:00",
                             30, "1", "Halo", "2026-08-04")
    writer = threading.Thread(target=store.insert_segment, args=(seg,))
    writer.start()
    writer.join()
    result = {}
    reader = threading.Thread(target=lambda: result.update(rows=store.segments_since("2000-01-01")))
    reader.start()
    reader.join()
    assert len(result["rows"]) == 1


def test_insert_failure_is_swallowed(tmp_path, caplog):
    """A write failure logs and is swallowed so a poll is never broken."""
    store = _store(tmp_path)
    store._path = str(tmp_path)          # a directory path → sqlite open/write fails
    store.insert_segment(xb_history.Segment("X", "Ari", "2026-08-04T10:00:00",
                                            "2026-08-04T10:30:00", 30, "1", "Halo", "2026-08-04"))
    # no exception raised; nothing crashed
