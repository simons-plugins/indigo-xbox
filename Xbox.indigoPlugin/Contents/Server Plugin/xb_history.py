"""Play-segment history: a finer-grained record than :mod:`xb_sessions`.

A *play segment* is contiguous in-game time on ONE title. Unlike
:class:`xb_sessions.SessionAccountant` — which deliberately treats a title
switch mid-session as one continuous session — this layer closes the current
segment and opens a new one whenever the title changes, so charts can break a
session down per game. The two layers are independent; nothing here changes
session accounting.

Design (mirrors ``xb_sessions``): pure and ``indigo``-free, with an injected
clock (a callable returning a naive **server-local** ``datetime``, so "which
day" a segment belongs to matches the session model). Open (in-progress)
segments live in memory in :class:`SegmentTracker`; a completed segment is
written to a stdlib :mod:`sqlite3` store on close only.

Persistence of *open* segments across a restart is deliberately NOT provided: a
plugin restart loses at most the currently-open segment's tail (its minutes
since the last title change / start). ``xb_sessions`` already keeps
``todayMinutes`` continuity through its sidecar, so the visible "today" total is
unaffected; only the fine-grained history row for the interrupted segment is
lost.

Thread-safety: the store opens a short-lived connection per operation (open →
execute → close) so the poll thread (writing on close) and the IWS handler
thread (reading for the chart endpoint) never share a connection. WAL is not
needed for this access pattern.
"""
import logging
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta

DEFAULT_KEEP_DAYS = 365


@dataclass
class Segment:
    """One completed play segment — a single row in the ``segments`` table."""

    xuid: str
    gamertag: str
    started_at: str    # ISO, naive local
    ended_at: str      # ISO, naive local
    minutes: int
    title_id: str
    title_name: str
    date: str          # 'YYYY-MM-DD' local date of started_at


def _minutes_between(start, end):
    """Whole minutes from ``start`` to ``end`` (floored, never negative)."""
    seconds = (end - start).total_seconds()
    return int(seconds // 60) if seconds > 0 else 0


def _start_of_day(moment):
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def _split_at_midnight(start, end):
    """Split a ``[start, end)`` span into one piece per local calendar day.

    Yields ``(piece_start, piece_end)`` tuples; a midnight-spanning span becomes
    two (or more) pieces so each is credited to its own local date. A zero-length
    trailing piece (``end`` exactly on midnight) is not emitted."""
    pieces = []
    cursor = start
    while cursor.date() < end.date():
        next_midnight = _start_of_day(cursor) + timedelta(days=1)
        pieces.append((cursor, next_midnight))
        cursor = next_midnight
    if cursor < end:
        pieces.append((cursor, end))
    return pieces


class HistoryStore:
    """SQLite store for completed play segments — one short-lived connection per
    operation (no shared handle, so it is safe across the poll and IWS threads)."""

    def __init__(self, path, logger=None):
        self._path = os.path.abspath(path)
        self._logger = logger or logging.getLogger("xb_history")
        self.ensure_schema()

    @property
    def path(self):
        return self._path

    def _connect(self):
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def ensure_schema(self):
        directory = os.path.dirname(self._path) or "."
        os.makedirs(directory, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS segments ("
                "id INTEGER PRIMARY KEY, "
                "xuid TEXT, gamertag TEXT, "
                "started_at TEXT, ended_at TEXT, "
                "minutes INTEGER, title_id TEXT, title_name TEXT, "
                "date TEXT)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_segments_xuid_date "
                "ON segments(xuid, date)")
            conn.commit()

    def insert_segment(self, segment):
        """Persist one completed :class:`Segment`. Never raises — a write failure
        (e.g. full disk) is logged and swallowed so a poll is never broken."""
        try:
            with closing(self._connect()) as conn:
                conn.execute(
                    "INSERT INTO segments "
                    "(xuid, gamertag, started_at, ended_at, minutes, title_id, "
                    "title_name, date) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (segment.xuid, segment.gamertag, segment.started_at,
                     segment.ended_at, segment.minutes, segment.title_id,
                     segment.title_name, segment.date))
                conn.commit()
        except sqlite3.Error as exc:
            self._logger.warning("Xbox history write failed (segment dropped): %s", exc)

    def segments_since(self, date_str):
        """All segment rows on or after local ``date_str`` ('YYYY-MM-DD'),
        oldest first, as plain dicts. Raises :class:`sqlite3.Error` on failure so
        the caller (the IWS handler) can map it to a 500."""
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                "SELECT xuid, gamertag, started_at, ended_at, minutes, title_id, "
                "title_name, date FROM segments WHERE date >= ? "
                "ORDER BY started_at ASC", (date_str,))
            return [dict(row) for row in cursor.fetchall()]

    def prune(self, keep_days=DEFAULT_KEEP_DAYS, now=None):
        """Delete rows older than ``keep_days`` local days; return rows removed.
        Never raises — pruning is best-effort housekeeping."""
        moment = now or datetime.now()
        cutoff = (moment.date() - timedelta(days=keep_days)).isoformat()
        try:
            with closing(self._connect()) as conn:
                cursor = conn.execute("DELETE FROM segments WHERE date < ?", (cutoff,))
                conn.commit()
                return cursor.rowcount
        except sqlite3.Error as exc:
            self._logger.warning("Xbox history prune failed: %s", exc)
            return 0


class SegmentTracker:
    """Turns a per-poll ``(xuid, in_game, title_id, title_name)`` stream into
    completed :class:`Segment` rows.

    Open segments are held in memory, one per xuid; a completed segment is
    written to ``store`` on close. ``store`` may be ``None`` (unit tests, or
    before the prefs path is known) — segments are still tracked in memory and
    simply not persisted. The clock is injected like :class:`xb_sessions`."""

    def __init__(self, store=None, clock=None):
        self._store = store
        self._clock = clock or datetime.now
        self._open = {}   # xuid -> {started_at, title_id, title_name, gamertag}

    def update(self, xuid, in_game, title_id="", title_name="", gamertag=""):
        """Advance the segment model for ``xuid`` for this poll.

        Opens a segment on the idle→in-game edge, closes it on the in-game→idle
        edge, and on a title switch (still in-game) closes the current segment
        and opens a fresh one — both at "now"."""
        now = self._clock()
        title_id = title_id or ""
        current = self._open.get(xuid)
        if in_game:
            if current is None:
                self._open[xuid] = self._new_open(now, title_id, title_name, gamertag)
            elif title_id != current["title_id"]:
                self._close(xuid, now)
                self._open[xuid] = self._new_open(now, title_id, title_name, gamertag)
            else:
                # Same title continuing — backfill a gamertag/title name that was
                # unknown when the segment opened (peoplehub can lag a poll).
                if gamertag and not current["gamertag"]:
                    current["gamertag"] = gamertag
                if title_name and not current["title_name"]:
                    current["title_name"] = title_name
        elif current is not None:
            self._close(xuid, now)

    @staticmethod
    def _new_open(now, title_id, title_name, gamertag):
        return {"started_at": now, "title_id": title_id,
                "title_name": title_name or "", "gamertag": gamertag or ""}

    def _close(self, xuid, end):
        segment = self._open.pop(xuid, None)
        if segment is None:
            return
        for piece_start, piece_end in _split_at_midnight(segment["started_at"], end):
            minutes = _minutes_between(piece_start, piece_end)
            if minutes <= 0:
                continue                       # sub-minute piece — nothing to record
            row = Segment(
                xuid=xuid, gamertag=segment["gamertag"],
                started_at=piece_start.isoformat(), ended_at=piece_end.isoformat(),
                minutes=minutes, title_id=segment["title_id"],
                title_name=segment["title_name"],
                date=piece_start.date().isoformat())
            if self._store is not None:
                self._store.insert_segment(row)

    def flush(self):
        """Close every open segment at "now" (best-effort, on shutdown). Any open
        segment not flushed is simply lost, never double-counted."""
        now = self._clock()
        for xuid in list(self._open):
            self._close(xuid, now)

    @property
    def open_xuids(self):
        """The xuids with a segment currently in progress (for tests/inspection)."""
        return set(self._open)
