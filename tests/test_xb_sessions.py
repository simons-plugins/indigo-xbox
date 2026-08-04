"""Tests for xb_sessions.py — the pure session accountant (driven by an injected
clock) and the atomic sidecar store."""
import json
import os
import stat
from datetime import datetime

import xb_sessions


class _Clock:
    """A settable clock: tests move ``now`` and the accountant reads it."""

    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def _at(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, 0)


# -- session start / live / stop ----------------------------------------------

def test_session_start_live_and_stop():
    clock = _Clock(_at(2026, 8, 4, 10, 0))
    acc = xb_sessions.SessionAccountant(clock=clock)

    # not playing yet
    stats = acc.update("X", False)
    assert stats.session_started_at == ""
    assert stats.session_minutes == 0
    assert stats.today_minutes == 0

    # session begins
    stats = acc.update("X", True)
    assert stats.session_started_at == _at(2026, 8, 4, 10, 0).isoformat()
    assert stats.session_minutes == 0

    # 30 live minutes in
    clock.now = _at(2026, 8, 4, 10, 30)
    stats = acc.update("X", True)
    assert stats.session_minutes == 30
    assert stats.today_minutes == 30
    assert stats.last_session_minutes == 0

    # session ends
    clock.now = _at(2026, 8, 4, 10, 45)
    stats = acc.update("X", False)
    assert stats.session_started_at == ""
    assert stats.session_minutes == 0
    assert stats.last_session_minutes == 45          # full 10:00 → 10:45
    assert stats.today_minutes == 45                 # accumulated for today


def test_today_accumulates_across_two_sessions():
    clock = _Clock(_at(2026, 8, 4, 9, 0))
    acc = xb_sessions.SessionAccountant(clock=clock)
    acc.update("X", True)
    clock.now = _at(2026, 8, 4, 9, 20)
    acc.update("X", False)                           # +20 today
    clock.now = _at(2026, 8, 4, 12, 0)
    acc.update("X", True)
    clock.now = _at(2026, 8, 4, 12, 10)
    stats = acc.update("X", True)                    # live 10
    assert stats.today_minutes == 30                 # 20 completed + 10 live
    assert stats.last_session_minutes == 20


def test_title_switch_mid_session_does_not_reset():
    """The accountant only sees the in-game boolean, so a title change (still
    in_game=True) keeps the same session running from its original start."""
    clock = _Clock(_at(2026, 8, 4, 20, 0))
    acc = xb_sessions.SessionAccountant(clock=clock)
    start = acc.update("X", True).session_started_at
    clock.now = _at(2026, 8, 4, 20, 15)
    a = acc.update("X", True)                         # "switched game" — still True
    clock.now = _at(2026, 8, 4, 20, 40)
    b = acc.update("X", True)
    assert a.session_started_at == start
    assert b.session_started_at == start             # unchanged across the switch
    assert b.session_minutes == 40


# -- midnight split -----------------------------------------------------------

def test_midnight_split_credits_only_after_midnight_today():
    clock = _Clock(_at(2026, 8, 4, 23, 40))
    acc = xb_sessions.SessionAccountant(clock=clock)
    acc.update("X", True)                             # starts 23:40
    clock.now = _at(2026, 8, 4, 23, 50)
    assert acc.update("X", True).today_minutes == 10  # day-4 so far

    # cross local midnight, still playing
    clock.now = _at(2026, 8, 5, 0, 10)
    stats = acc.update("X", True)
    assert stats.session_minutes == 30                # full contiguous 23:40 → 00:10
    assert stats.today_minutes == 10                  # only 00:00 → 00:10 counts today
    assert stats.session_started_at == _at(2026, 8, 4, 23, 40).isoformat()


def test_idle_day_rollover_resets_today():
    clock = _Clock(_at(2026, 8, 4, 22, 0))
    acc = xb_sessions.SessionAccountant(clock=clock)
    acc.update("X", True)
    clock.now = _at(2026, 8, 4, 22, 30)
    acc.update("X", False)                            # 30 min today
    clock.now = _at(2026, 8, 5, 8, 0)                 # next day, idle
    stats = acc.update("X", False)
    assert stats.today_minutes == 0                   # yesterday's minutes dropped


# -- restart resume from sidecar ----------------------------------------------

def test_snapshot_has_only_contract_keys():
    clock = _Clock(_at(2026, 8, 4, 10, 0))
    acc = xb_sessions.SessionAccountant(clock=clock)
    acc.update("X", True)
    snap = acc.snapshot()["X"]
    assert set(snap) == {"date", "accumulated_minutes", "session_started_at"}
    assert snap["session_started_at"] == _at(2026, 8, 4, 10, 0).isoformat()


def test_restart_resumes_in_flight_session():
    clock = _Clock(_at(2026, 8, 4, 10, 0))
    first = xb_sessions.SessionAccountant(clock=clock)
    first.update("X", True)                           # mid-session at snapshot
    snap = first.snapshot()

    # New process: load the snapshot, next poll 20 min later still playing.
    later = _Clock(_at(2026, 8, 4, 10, 20))
    second = xb_sessions.SessionAccountant(clock=later)
    second.load(snap)
    stats = second.update("X", True)
    assert stats.session_started_at == _at(2026, 8, 4, 10, 0).isoformat()
    assert stats.session_minutes == 20                # resumed from persisted start


def test_restart_then_not_playing_closes_session_best_effort():
    clock = _Clock(_at(2026, 8, 4, 10, 0))
    first = xb_sessions.SessionAccountant(clock=clock)
    first.update("X", True)
    snap = first.snapshot()

    later = _Clock(_at(2026, 8, 4, 10, 5))
    second = xb_sessions.SessionAccountant(clock=later)
    second.load(snap)
    stats = second.update("X", False)                # first post-restart poll: idle
    assert stats.session_started_at == ""            # closed
    assert stats.last_session_minutes == 5           # best-effort start → now
    assert stats.today_minutes == 5


# -- atomic sidecar store -----------------------------------------------------

def test_store_write_read_roundtrip_and_perms(tmp_path):
    path = tmp_path / "sub" / "sessions.json"         # dir created on write
    store = xb_sessions.SessionStore(str(path))
    data = {"X": {"date": "2026-08-04", "accumulated_minutes": 42,
                  "session_started_at": None}}
    store.write(data)
    assert store.read() == data
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600                              # atomic write chmod'd 0600
    # file is valid JSON on disk (no partial write left behind)
    with open(path, encoding="utf-8") as handle:
        assert json.load(handle) == data


def test_store_read_missing_file_is_empty(tmp_path):
    store = xb_sessions.SessionStore(str(tmp_path / "nope.json"))
    assert store.read() == {}
