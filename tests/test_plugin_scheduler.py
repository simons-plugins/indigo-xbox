"""Tests for runConcurrentThread's scheduling: presence and console polling
run on independent cadences, sleeps stay short enough for StopThread to be
responsive, a console-poll exception never escapes the loop, and a
pull-forward that happens during a poll is never clobbered by the poll's own
post-poll due-time update.

The fake clock is driven entirely through ``p.sleep`` (monkeypatched to
advance ``plugin.time.time`` and raise ``p.StopThread`` once the simulated
run has gone on long enough) rather than real time — these tests take
microseconds, not seconds.
"""
import plugin


def _plugin():
    return plugin.Plugin("com.simons-plugins.indigo-xbox", "Xbox", "2026.2.0", {})


def _fake_clock(monkeypatch, start=0.0):
    """Installs a fake ``plugin.time.time`` backed by a mutable box, and
    returns it so tests can read/advance it directly if needed."""
    clock = {"t": start}
    monkeypatch.setattr(plugin.time, "time", lambda: clock["t"])
    return clock


def _fake_sleep(p, clock, stop_after):
    """Advances the fake clock by the requested amount on every ``p.sleep``
    call, and raises StopThread once the clock passes ``stop_after`` — the
    only way the tests below end the (otherwise infinite) run loop."""
    calls = []

    def sleep(seconds):
        calls.append(seconds)
        clock["t"] += seconds
        if clock["t"] > stop_after:
            raise p.StopThread()
    p.sleep = sleep
    return calls


# -- (a) presence cadence, despite short sleep slices --------------------------

def test_presence_polls_exactly_at_expected_ticks_despite_short_slices(monkeypatch):
    p = _plugin()
    clock = _fake_clock(monkeypatch)
    supervise_calls = []
    monkeypatch.setattr(p, "_supervise", lambda: supervise_calls.append(clock["t"]))
    monkeypatch.setattr(p, "_poll_consoles", lambda: (_ for _ in ()).throw(
        AssertionError("_poll_consoles must not be called — no console devices")))
    p._poll_interval = 60
    sleeps = _fake_sleep(p, clock, stop_after=130)

    p.runConcurrentThread()

    assert supervise_calls == [0.0, 60.0, 120.0]
    assert all(s <= plugin.MAX_SLEEP_SLICE for s in sleeps)


# -- (b) no console devices: _poll_consoles never called, sleeps stay short ---

def test_no_console_devices_never_polls_consoles(monkeypatch):
    p = _plugin()
    clock = _fake_clock(monkeypatch)
    monkeypatch.setattr(p, "_supervise", lambda: None)
    monkeypatch.setattr(p, "_poll_consoles", lambda: (_ for _ in ()).throw(
        AssertionError("_poll_consoles must not be called — no console devices")))
    p._poll_interval = 60
    sleeps = _fake_sleep(p, clock, stop_after=200)

    p.runConcurrentThread()          # must not raise the AssertionError above

    assert all(s <= plugin.MAX_SLEEP_SLICE for s in sleeps)


# -- (c) independent cadences: presence 60s / console 15s over 61s ------------

def test_presence_and_console_poll_on_independent_cadences(monkeypatch):
    p = _plugin()
    clock = _fake_clock(monkeypatch)
    supervise_calls = []
    console_calls = []
    monkeypatch.setattr(p, "_supervise", lambda: supervise_calls.append(clock["t"]))
    monkeypatch.setattr(p, "_poll_consoles", lambda: console_calls.append(clock["t"]))
    p._poll_interval = 60
    p._console_poll_interval = 15
    p._consoles = {1: "C1"}          # any non-empty registry — has_consoles
    _fake_sleep(p, clock, stop_after=61)

    p.runConcurrentThread()

    assert supervise_calls == [0.0, 60.0]
    assert console_calls == [0.0, 15.0, 30.0, 45.0, 60.0]


# -- (d) console backoff delays consoles only ----------------------------------

def test_console_backoff_delays_console_polling_only(monkeypatch):
    p = _plugin()
    clock = _fake_clock(monkeypatch)
    supervise_calls = []
    console_calls = []

    def fake_poll_consoles():
        console_calls.append(clock["t"])
        if len(console_calls) == 1:
            # Simulate a failed poll backing off hard — far past the plain
            # console_poll_interval, and past the run's own end time.
            p._console_backoff_until = clock["t"] + 900
    monkeypatch.setattr(p, "_supervise", lambda: supervise_calls.append(clock["t"]))
    monkeypatch.setattr(p, "_poll_consoles", fake_poll_consoles)
    p._poll_interval = 60
    p._console_poll_interval = 15
    p._consoles = {1: "C1"}
    _fake_sleep(p, clock, stop_after=130)

    p.runConcurrentThread()

    assert supervise_calls == [0.0, 60.0, 120.0]   # presence unaffected by console backoff
    assert console_calls == [0.0]                  # backed off past the run's end — no 2nd poll


# -- (e) a pull-forward during a poll must not be clobbered -------------------

def test_pull_forward_during_poll_is_not_overwritten(monkeypatch):
    """``_pull_console_poll_forward`` itself only ever moves the due time
    EARLIER than its current value, so it cannot demonstrate this race
    directly while the loop's own due is already <= now (that's why the poll
    fired in the first place — "now + any positive delay" can never be
    earlier than an already-due time). We instead simulate the *effect* of
    a concurrent pull-forward — self._next_console_due changing to an
    earlier future value while ``_poll_consoles`` is running — to test
    runConcurrentThread's own "keep the earlier of the two" logic directly."""
    p = _plugin()
    clock = _fake_clock(monkeypatch)

    def fake_poll_consoles():
        p._next_console_due = clock["t"] + 3   # as if pulled forward mid-poll
    monkeypatch.setattr(p, "_supervise", lambda: None)
    monkeypatch.setattr(p, "_poll_consoles", fake_poll_consoles)
    p._poll_interval = 9999
    p._console_poll_interval = 60          # normal post-poll due would be now+60
    p._consoles = {1: "C1"}
    # Stop before the (pulled-forward) due at t=3 would trigger a second
    # poll — this test is about the single race, not a second cycle.
    _fake_sleep(p, clock, stop_after=2)

    p.runConcurrentThread()

    # The pulled-forward due time (now(0) + 3 = 3) must win over the
    # normal post-poll advance (now(0) + 60 = 60).
    assert p._next_console_due == 3.0


# -- (f) an exception from _poll_consoles never escapes, and due advances -----

def test_poll_consoles_exception_does_not_escape_and_due_advances(monkeypatch):
    p = _plugin()
    clock = _fake_clock(monkeypatch)

    def fake_poll_consoles():
        raise RuntimeError("boom")
    monkeypatch.setattr(p, "_supervise", lambda: None)
    monkeypatch.setattr(p, "_poll_consoles", fake_poll_consoles)
    p._poll_interval = 9999
    p._console_poll_interval = 60
    p._consoles = {1: "C1"}
    _fake_sleep(p, clock, stop_after=4)

    p.runConcurrentThread()            # must not raise RuntimeError

    assert p._next_console_due == 60.0     # due still advanced despite the exception
