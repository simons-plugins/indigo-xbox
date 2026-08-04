"""Play-session accounting: turn a per-poll stream of "in a game?" booleans
into live and historical play-time, per xuid.

Pure and ``indigo``-free. The clock is injected (a callable returning a
``datetime``) so tests can drive time deterministically; the plugin wires in
``datetime.now`` (naive **server-local** time — "today" for the day-rollover
logic must be the server's local date, unlike ``lastSeen``/``lastPoll`` which
stay UTC).

A *session* is a contiguous run of ``in_game == True``. A title change while
still in a game does NOT end the session (the accountant only ever sees the
boolean, never the title). Going to dashboard-only / online-not-playing /
offline flips the boolean to ``False`` and ends the session.

Persistence is a sidecar JSON keyed by xuid, written atomically by
:class:`SessionStore` (mkstemp → chmod 0600 → os.replace, mirroring
``xb_auth.TokenStore``). The persisted shape per the plugin contract is exactly::

    {"date": "YYYY-MM-DD", "accumulated_minutes": int, "session_started_at": iso-or-null}

``last_session_minutes`` is deliberately NOT persisted (it is a minor display
stat); it lives in memory only and resets to 0 across a restart until the next
session completes.
"""
import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime


@dataclass
class SessionStats:
    """The four computed values written to device states each poll."""

    session_started_at: str    # ISO string, "" when not playing
    session_minutes: int       # live minutes of the current session, 0 when idle
    last_session_minutes: int  # length of the most recently completed session
    today_minutes: int         # completed-today minutes + live session so far


def _minutes_between(start, end):
    """Whole minutes from ``start`` to ``end`` (floored, never negative)."""
    seconds = (end - start).total_seconds()
    return int(seconds // 60) if seconds > 0 else 0


def _start_of_day(moment):
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def _parse_iso(text):
    """Parse an ISO stamp we wrote via ``datetime.isoformat`` (naive local)."""
    try:
        return datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None


class SessionAccountant:
    """Per-xuid session bookkeeping. Feed it ``update(xuid, in_game)`` once per
    poll; it returns the :class:`SessionStats` to write. Records live in memory;
    :meth:`snapshot` / :meth:`load` bridge to the on-disk sidecar."""

    def __init__(self, clock=None):
        self._clock = clock or datetime.now
        self._records = {}

    def _record(self, xuid, today):
        rec = self._records.get(xuid)
        if rec is None:
            rec = {"date": today, "accumulated_minutes": 0,
                   "session_started_at": None, "last_session_minutes": 0}
            self._records[xuid] = rec
        return rec

    def update(self, xuid, in_game):
        """Advance the session model for ``xuid`` and return its stats."""
        now = self._clock()
        today = now.date().isoformat()
        rec = self._record(xuid, today)

        # Local-midnight rollover: yesterday's accumulated minutes do not count
        # toward today. A session in progress is NOT ended — the split is
        # handled by clamping today's live credit to start-of-day below, so the
        # minutes before midnight are simply left with yesterday.
        if rec["date"] != today:
            rec["date"] = today
            rec["accumulated_minutes"] = 0

        start_iso = rec["session_started_at"]
        if in_game:
            if not start_iso:
                start_iso = now.isoformat()          # session begins now
                rec["session_started_at"] = start_iso
            start_dt = _parse_iso(start_iso) or now
            session_minutes = _minutes_between(start_dt, now)
            today_live = _minutes_between(max(start_dt, _start_of_day(now)), now)
            today_minutes = rec["accumulated_minutes"] + today_live
            return SessionStats(start_iso, session_minutes,
                                rec["last_session_minutes"], today_minutes)

        if start_iso:
            # Session ends. Credit only today's portion to the accumulator (a
            # session that spanned midnight already had its pre-midnight minutes
            # dropped by the rollover reset). ``last_session_minutes`` records
            # the full contiguous length. On a restart *mid-session* the record
            # is resumed from the persisted start; if the first post-restart
            # poll shows not-playing, this same path closes it crediting
            # start→now — best effort, since no last-poll timestamp is persisted
            # it may over-count any offline downtime during the restart.
            start_dt = _parse_iso(start_iso)
            if start_dt is not None:
                rec["accumulated_minutes"] += _minutes_between(
                    max(start_dt, _start_of_day(now)), now)
                rec["last_session_minutes"] = _minutes_between(start_dt, now)
            rec["session_started_at"] = None
        return SessionStats("", 0, rec["last_session_minutes"],
                            rec["accumulated_minutes"])

    def snapshot(self):
        """The persist-projection: only the three contract keys, per xuid."""
        return {
            xuid: {
                "date": rec["date"],
                "accumulated_minutes": rec["accumulated_minutes"],
                "session_started_at": rec["session_started_at"],
            }
            for xuid, rec in self._records.items()
        }

    def load(self, data):
        """Restore records from a sidecar dict (ignores malformed entries)."""
        if not isinstance(data, dict):
            return
        for xuid, entry in data.items():
            if not isinstance(entry, dict):
                continue
            self._records[str(xuid)] = {
                "date": entry.get("date") or "",
                "accumulated_minutes": _coerce_int(entry.get("accumulated_minutes")),
                "session_started_at": entry.get("session_started_at") or None,
                "last_session_minutes": 0,   # not persisted; see module docstring
            }


def _coerce_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


class SessionStore:
    """Atomic reader/writer for the session sidecar JSON (0600).

    Mirrors ``xb_auth.TokenStore``'s write pattern (mkstemp → chmod 0600 →
    os.replace) so a reader ever sees the old file or the new one, never a
    partial write. One store per absolute path."""

    def __init__(self, path, logger=None):
        self._path = os.path.abspath(path)
        self._logger = logger or logging.getLogger("xb_sessions")
        self._lock = threading.Lock()

    @property
    def path(self):
        return self._path

    def read(self):
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def write(self, data):
        directory = os.path.dirname(self._path) or "."
        with self._lock:
            try:
                os.makedirs(directory, exist_ok=True)
                fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        json.dump(data, handle)
                    os.chmod(tmp, 0o600)          # restrict BEFORE it goes live
                    os.replace(tmp, self._path)   # atomic swap
                except OSError:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                    raise
            except OSError as exc:
                self._logger.warning("Xbox session save failed (left intact): %s", exc)
