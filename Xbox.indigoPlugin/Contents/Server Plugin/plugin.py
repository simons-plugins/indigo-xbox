"""Xbox Live presence plugin for Indigo.

Tracks the Xbox Live presence of family members (one device per gamertag) so
Indigo's SQL Logger accumulates play-session history. Auth is MSA Device Flow
(``xb_auth``); presence is polled from peoplehub (``xb_presence``). All
Indigo-touching code lives here; the ``xb_*`` modules are pure stdlib and never
import ``indigo``.
"""
import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

try:
    import indigo
except ImportError:  # pragma: no cover - only importable inside the Indigo server
    indigo = None

import xb_constants as xc
import xb_consoles
import xb_history
import xb_presence
import xb_sessions
from xb_api import XboxAPI, XboxError, redact
from xb_auth import XboxAuth

TOKEN_FILENAME = "com.simons-plugins.indigo-xbox.tokens.json"
SESSIONS_FILENAME = "com.simons-plugins.indigo-xbox.sessions.json"
HISTORY_FILENAME = "com.simons-plugins.indigo-xbox.history.sqlite"

# The usage-charts page (static file, IWS-served) and its JSON endpoint (hidden
# action). Paths are relative to the IWS root; the page fetches the endpoint via
# an absolute same-origin URL so it works both from the plugin bundle and from
# the copy in Web Assets/static/pages (see startup() for the Domio copy step).
CHARTS_PAGE_PATH = "/com.simons-plugins.indigo-xbox/static/pages/xbox-charts.html"
CHARTS_PAGE_FILENAME = "xbox-charts.html"
CHART_DATA_DAYS = 30                 # segment window returned to the charts page

DEFAULT_POLL_INTERVAL = 60
MIN_POLL_INTERVAL = 15
MAX_POLL_INTERVAL = 600

# Console polling (xboxConsole devices) shares the same range/default as
# presence, so it reuses _coerce_interval() and the same validation text.
# Backoff (list-call failures) starts at this many seconds and doubles up to
# the cap; a power action pulls the next poll forward by a short, fixed delay
# so the state catches up without polling on every action.
CONSOLE_BACKOFF_BASE = 60
CONSOLE_BACKOFF_MAX = 900
CONSOLE_POWER_ACTION_FOLLOWUP_DELAY = 10
MAX_SLEEP_SLICE = 5
# XboxError.key marking a wrapped non-Xbox exception (logged with a traceback).
UNEXPECTED_ERROR_KEY = "unexpected_exception"

# Gamerscore/tier move slowly, and the profile service is a whole extra call, so
# the self device refreshes its profile extras only every Nth poll (plus once at
# startup when the cache is empty).
SELF_PROFILE_REFRESH_EVERY = 10

_STATE_TEXT = {
    xc.STATE_AUTHORIZED: "Authorized.",
    xc.STATE_PENDING: "Authorization in progress — check the Event Log for the link.",
    xc.STATE_AUTH_REQUIRED: "Authorization required — please Authorize again.",
    xc.STATE_UNAUTHORIZED: "Not authorized.",
}


class Plugin(indigo.PluginBase):
    """Main plugin class."""

    def __init__(self, plugin_id, plugin_display_name, plugin_version, plugin_prefs):
        super().__init__(plugin_id, plugin_display_name, plugin_version, plugin_prefs)
        self.debug = plugin_prefs.get("showDebugInfo", False)
        self._api = None
        self._auth = None
        self._poll_interval = _coerce_interval(plugin_prefs.get("pollInterval"))
        self._console_poll_interval = _coerce_interval(plugin_prefs.get("consolePollInterval"))
        self._auth_thread = None
        self._stop_auth = threading.Event()
        # Guards the api/auth pair against races between the config UI thread
        # (rebuild) and runConcurrentThread (poll every pollInterval).
        self._client_lock = threading.RLock()
        # dev.id -> xuid for every enabled tracked device; and the ids currently
        # showing an error, so we clear it exactly once on recovery. Console
        # devices (dev.id -> consoleId) live in a SEPARATE registry — they must
        # never enter self._tracked (presence sync / charts payload).
        self._tracked = {}
        self._consoles = {}
        # dev.id -> last error message set on that device, so a DIFFERENT
        # message (e.g. "console list unavailable" -> "not authorized")
        # still reaches setErrorStateOnServer instead of being swallowed by
        # the once-per-episode dedup.
        self._errored = {}
        self._dev_lock = threading.RLock()
        # Last console list from a successful poll — feeds the device-config
        # menu without an extra API call while the dialog is open.
        self._last_consoles = []
        # Focused-title resolution cache: console_id -> {aumid: (name, titleId)
        # or None for a cached miss}. A miss is cached too so an unresolved
        # aumid never re-fetches installedApps every poll.
        self._console_apps = {}
        # Due-time scheduling for console polling (separate cadence from
        # presence): 0.0 = due on the next runConcurrentThread tick. A Status
        # Request pulls this to "now" and polls synchronously; a power action
        # pulls it a short, fixed delay forward so the state catches up.
        self._next_console_due = 0.0
        self._console_poll_lock = threading.Lock()
        self._console_backoff_until = 0.0
        self._console_poll_failures = 0
        self._console_poll_error_logged = False
        # (console_id, kind) -> already logged this episode, for the
        # per-console status/installedApps failure log-once (kind is
        # "status" or "installed apps"; reset to "not logged" on success).
        self._console_detail_error_logged = {}
        # console_id -> already logged this episode, for the "focusAppAumid
        # absent from status" debug log-once (reset once a real aumid, blank
        # or non-blank, is seen again).
        self._console_focus_unknown_logged = set()
        # dev.id -> already logged this episode, for the per-device
        # state-write failure traceback log-once (reset on the next
        # successful sync).
        self._console_sync_error_logged = {}
        # One session id per plugin run, reused for every power command sent
        # this run (per the xccs /commands contract).
        self._session_id = str(uuid.uuid4())
        # Last people list from a successful poll — feeds the device-config menu
        # and the "Log Tracked People" action without an extra API call.
        self._last_people = []
        # Session accounting (pure, indigo-free; clock = server-local now). The
        # sidecar store is created in startup() once the Indigo prefs path is
        # available; None here means "don't persist" (unit tests).
        self._sessions = xb_sessions.SessionAccountant(clock=datetime.now)
        self._session_store = None
        self._last_session_snapshot = {}
        # Play-segment history (finer-grained than sessions: one segment per
        # title). Open segments live in the tracker's memory; the SQLite store
        # is created in startup() once the prefs path is available (None until
        # then / in unit tests → segments tracked but not persisted).
        self._history = None
        self._segments = xb_history.SegmentTracker(store=None, clock=datetime.now)
        # Self-device profile extras: cached ProfileInfo + a poll countdown.
        self._self_profile = None
        self._self_profile_countdown = 0
        # Title box art, cached per titleId for the plugin's lifetime (a cached
        # "" means "already tried, none available" so we never re-hit the API).
        self._title_images = {}

    # -- Lifecycle -----------------------------------------------------------
    def startup(self):
        self.logger.info("Xbox plugin starting")
        self._rebuild_client()
        self._load_sessions()
        self._load_history()
        device_count = len(list(indigo.devices.iter("self")))
        self.logger.info("Xbox %s started with %d device(s) configured",
                         self.pluginVersion, device_count)
        self._log_charts_page_paths()

    def _log_charts_page_paths(self):
        """Point at the charts page's plugin-bundle location (served by IWS
        behind auth) and the copy step needed for Domio to list it — Domio's
        HTML-pages scan only looks in Web Assets/static/pages, not inside
        plugin bundles."""
        try:
            install = indigo.server.getInstallFolderPath()
        except Exception:  # pylint: disable=broad-except
            install = "{Indigo install}"
        source = os.path.join(install, "Plugins", "Xbox.indigoPlugin", "Contents",
                              "Resources", "static", "pages", CHARTS_PAGE_FILENAME)
        dest_dir = os.path.join(install, "Web Assets", "static", "pages")
        self.logger.info("Xbox usage charts page: %s — to make it appear in Domio, "
                         "copy it to %s (no restart needed; re-copy after each "
                         "plugin update)", CHARTS_PAGE_PATH, dest_dir)
        self.logger.debug("Xbox charts page source: %s", source)

    def shutdown(self):
        self._stop_auth.set()
        self._persist_sessions()          # flush any in-flight session state
        self._segments.flush()            # close open play segments (best-effort)
        self.logger.info("Xbox plugin stopped")

    def _load_history(self):
        """Open the play-segment store, wire it into the tracker, and prune old
        rows once at startup (best-effort — history is a nice-to-have layer and
        must never block the plugin from coming up)."""
        try:
            self._history = xb_history.HistoryStore(self._history_path(), logger=self.logger)
            self._segments = xb_history.SegmentTracker(store=self._history, clock=datetime.now)
            pruned = self._history.prune()
            if pruned:
                self.logger.debug("Xbox history: pruned %d old segment(s)", pruned)
        except Exception as exc:  # pylint: disable=broad-except
            self.logger.warning("Xbox history unavailable (charts will show no "
                                "past sessions): %s", exc)

    def _load_sessions(self):
        """Restore session records from the sidecar so a plugin restart resumes
        any in-flight session (see xb_sessions for the mid-session behaviour)."""
        self._session_store = xb_sessions.SessionStore(self._sessions_path(),
                                                       logger=self.logger)
        data = self._session_store.read()
        self._sessions.load(data)
        self._last_session_snapshot = self._sessions.snapshot()

    def _persist_sessions(self):
        """Write the session sidecar, but only when the persisted projection
        actually changed (session start/stop, day rollover) — the once-a-minute
        live tick is not part of that projection, so this is quiet."""
        if self._session_store is None:
            return
        snapshot = self._sessions.snapshot()
        if snapshot != self._last_session_snapshot:
            self._session_store.write(snapshot)
            self._last_session_snapshot = snapshot

    def runConcurrentThread(self):
        """Presence runs every ``pollInterval`` exactly as before; consoles run
        every ``consolePollInterval`` on an independent due-time (skipped
        entirely — no API call — while no console device is configured).
        Sleeps in short slices so StopThread stays responsive and a Status
        Request / power action can pull the next console poll forward."""
        next_presence_due = 0.0
        try:
            while True:
                now = time.time()
                if now >= next_presence_due:
                    self._supervise()
                    next_presence_due = time.time() + self._poll_interval
                with self._dev_lock:
                    has_consoles = bool(self._consoles)
                    console_due = self._next_console_due
                next_wake = next_presence_due
                if has_consoles:
                    if time.time() >= console_due:
                        due_before_poll = console_due
                        try:
                            self._poll_consoles()
                        except Exception as exc:  # pylint: disable=broad-except
                            # Must never escape: an uncaught exception here kills
                            # runConcurrentThread, and Indigo restarts it — which
                            # would poll presence + consoles again immediately,
                            # in a loop, unless the due time still advances below.
                            self.logger.exception(exc)
                        with self._dev_lock:
                            candidate = time.time() + self._console_poll_delay()
                            if self._next_console_due == due_before_poll:
                                self._next_console_due = candidate
                            else:
                                # Pulled forward (e.g. by a power action) during
                                # the poll — keep the earlier of the two rather
                                # than clobbering it with a later "now + delay".
                                self._next_console_due = min(self._next_console_due, candidate)
                            console_due = self._next_console_due
                    next_wake = min(next_wake, console_due)
                self.sleep(max(0.1, min(next_wake - time.time(), MAX_SLEEP_SLICE)))
        except self.StopThread:
            pass

    def _supervise(self):
        auth, api = self._auth, self._api
        if not (auth and api and auth.is_authorized()):
            return
        if auth.state() == xc.STATE_AUTH_REQUIRED:
            return
        try:
            auth.refresh_if_needed()
            people = xb_presence.fetch_presence(api, auth, logger=self.logger)
            if people is None:
                return
            own_presence = self._fetch_own_presence_if_tracked(api, auth)
            if own_presence is not None:
                profile = self._maybe_fetch_self_profile(api, auth, own_presence.xuid)
                own_presence = xb_presence.apply_profile(own_presence, profile)
                people = people + [own_presence]
            self._sync_all(people)
        except XboxError as exc:
            if exc.status == 429:
                self.logger.warning("Xbox rate-limited; will retry next poll (%s)", exc)
            else:
                self.logger.error("Xbox presence poll failed: %s", exc)
        except Exception as exc:  # pylint: disable=broad-except
            self.logger.exception(exc)

    def _fetch_own_presence_if_tracked(self, api, auth):
        """Fetch the signed-in account's own presence — but only when a
        tracked device's xuid is the account's own. Peoplehub already covers
        everyone else, so this is the one extra call a self device costs."""
        with self._dev_lock:
            tracked_xuids = set(self._tracked.values())
        own = auth.own_identity()
        if not own:
            return None
        own_xuid, own_gamertag = own
        if own_xuid not in tracked_xuids:
            return None
        return xb_presence.fetch_own_presence(api, auth, own_xuid=own_xuid,
                                              own_gamertag=own_gamertag, logger=self.logger)

    def _maybe_fetch_self_profile(self, api, auth, xuid):
        """Return cached profile extras, refreshing them from the profile
        service only every :data:`SELF_PROFILE_REFRESH_EVERY` polls (and once
        while the cache is empty). Never raises — a failure keeps the last
        cached value (or ``None`` → empty states) and never breaks presence
        sync."""
        if self._self_profile_countdown > 0:
            self._self_profile_countdown -= 1
            return self._self_profile
        try:
            profile = xb_presence.fetch_profile(api, auth, xuid, logger=self.logger)
        except Exception as exc:  # pylint: disable=broad-except
            self.logger.debug("Xbox self-profile fetch failed (states left as-is): %s", exc)
            return self._self_profile
        if profile is not None:
            self._self_profile = profile
            self._self_profile_countdown = SELF_PROFILE_REFRESH_EVERY
        return self._self_profile

    # -- Console polling -------------------------------------------------------
    def _poll_consoles(self):
        """One console-list call per cycle, fanned out to every configured
        console device. Never touches presence state or self._tracked.
        Serialized: a Status Request polls on the UI thread and must not
        interleave with runConcurrentThread's poll (both would diff against
        the same pre-write states)."""
        with self._console_poll_lock:
            self._poll_consoles_locked()

    def _poll_consoles_locked(self):
        with self._dev_lock:
            consoles = dict(self._consoles)
        if not consoles:
            return
        auth, api = self._auth, self._api
        if not (auth and api and auth.is_authorized()):
            self._set_console_devices_not_authorized(consoles.keys())
            return
        if auth.state() == xc.STATE_AUTH_REQUIRED:
            self._set_console_devices_not_authorized(consoles.keys())
            return
        try:
            auth.refresh_if_needed()
            console_list = xb_consoles.fetch_consoles(api, auth, logger=self.logger)
        except XboxError as exc:
            self._handle_console_poll_failure(consoles.keys(), exc)
            return
        except Exception as exc:  # pylint: disable=broad-except
            wrapped = XboxError(f"unexpected {type(exc).__name__}: {exc}",
                                key=UNEXPECTED_ERROR_KEY)
            self._handle_console_poll_failure(consoles.keys(), wrapped)
            return
        if console_list is None:
            self._set_console_devices_not_authorized(consoles.keys())
            return
        with self._dev_lock:
            had_failure = self._console_poll_error_logged
            self._console_poll_failures = 0
            self._console_backoff_until = 0.0
            self._console_poll_error_logged = False
        if had_failure:
            self.logger.info("Xbox console poll recovered")
        self._last_consoles = console_list
        by_id = {console.id: console for console in console_list}
        now_iso = _utc_now_iso()
        for dev_id, console_id in consoles.items():
            try:
                dev = indigo.devices[dev_id]
            except KeyError:
                continue                  # device deleted between poll and lookup
            except Exception as exc:  # pylint: disable=broad-except
                self.logger.exception(exc)      # lookup itself broke, not "gone"
                continue
            try:
                console = by_id.get(console_id)
                if console is None:
                    changed = self._set_device_error(dev, "console not found")
                    if changed:
                        self.logger.warning(
                            "Xbox console '%s': console not found on this account (id=%s) — "
                            "the console may have been removed from the account, or this "
                            "device's console id may be stale; reconfigure the device to pick "
                            "a current console.", dev.name, console_id)
                    continue
                if xb_consoles.display_text(console.power_state) is None:
                    self._set_device_error(dev, "unavailable")
                    continue
                # Sync FIRST: only clear the device error once the write
                # actually succeeds, so a sync failure never masquerades as
                # a healthy device (and never gets faked to Off).
                try:
                    self._sync_console_device(dev, console, now_iso)
                except Exception as exc:  # pylint: disable=broad-except
                    self._set_device_error(dev, "state update failed")
                    self._log_console_sync_failure_once(dev, exc)
                    continue
                self._clear_error(dev)
                self._clear_console_sync_failure(dev.id)
            except Exception as exc:  # pylint: disable=broad-except
                # A failure handling THIS device must not stop the others.
                self.logger.exception(exc)

    def _set_console_devices_not_authorized(self, dev_ids):
        """Auth was lost (not authorized / re-authorization required / no
        header) between polls: error every configured console device once
        per episode, states left exactly as they were — never an API call.

        Also resets the console-LIST failure episode: once auth is lost, a
        list call is never attempted, so a later list failure (after
        re-authorizing) is a fresh episode and must log at ERROR again, not
        be swallowed by a stale "already logged" flag from before auth was
        lost."""
        with self._dev_lock:
            self._console_poll_error_logged = False
            self._console_poll_failures = 0
        for dev_id in dev_ids:
            try:
                dev = indigo.devices[dev_id]
            except KeyError:
                continue
            except Exception as exc:  # pylint: disable=broad-except
                self.logger.exception(exc)
                continue
            try:
                self._set_device_error(dev, "not authorized")
            except Exception as exc:  # pylint: disable=broad-except
                self.logger.exception(exc)

    def _poll_consoles_now(self):
        """Synchronous, immediate console poll (Status Request)."""
        self._poll_consoles()
        with self._dev_lock:
            self._next_console_due = time.time() + self._console_poll_delay()

    def _pull_console_poll_forward(self, delay):
        """Move the next console poll earlier — used after a power command so
        the state catches up without polling every action. An overdue due
        time (a poll is running or about to) is replaced too: otherwise a
        command sent mid-poll is lost when the loop reschedules at
        now + interval (runConcurrentThread keeps the earlier of the two).

        Never undercuts an active backoff: a power action doesn't fix
        whatever made the list call start failing, so the earliest this can
        pull the poll to is the backoff's own end."""
        with self._dev_lock:
            now = time.time()
            target = max(now + delay, self._console_backoff_until)
            if target < self._next_console_due or self._next_console_due <= now:
                self._next_console_due = target

    def _console_poll_delay(self):
        """Seconds until the next console poll: the configured interval, or
        the remaining backoff delay (honours a 429 ``retry_after`` exactly,
        even when it is shorter than the configured interval)."""
        now = time.time()
        with self._dev_lock:
            backoff_until = self._console_backoff_until
        if backoff_until > now:
            return backoff_until - now
        return self._console_poll_interval

    def _handle_console_poll_failure(self, dev_ids, exc):
        """A console-list call failed (transport, 401-after-retry, 429, 5xx,
        bad JSON, an unexpected exception, or a list-level errorCode): back
        off, and error every configured console device once — their last
        known states are left untouched. Logs one line per failure episode
        (a rate limit at WARNING, anything else at ERROR with the traceback),
        debug thereafter."""
        with self._dev_lock:
            self._console_poll_failures += 1
            failures = self._console_poll_failures
        rate_limited = exc.status == 429 and exc.retry_after
        if rate_limited:
            delay = exc.retry_after
        else:
            delay = min(CONSOLE_BACKOFF_BASE * (2 ** (failures - 1)), CONSOLE_BACKOFF_MAX)
        self._log_console_failure_once(exc, rate_limited, delay)
        with self._dev_lock:
            self._console_backoff_until = time.time() + delay
        for dev_id in dev_ids:
            try:
                dev = indigo.devices[dev_id]
            except KeyError:
                continue
            except Exception as lookup_exc:  # pylint: disable=broad-except
                self.logger.exception(lookup_exc)
                continue
            try:
                self._set_device_error(dev, "console list unavailable")
            except Exception as set_exc:  # pylint: disable=broad-except
                self.logger.exception(set_exc)

    def _log_console_failure_once(self, exc, rate_limited, delay):
        with self._dev_lock:
            already = self._console_poll_error_logged
            self._console_poll_error_logged = True
        if rate_limited:
            if already:
                self.logger.debug("Xbox console poll still rate-limited; retrying in %ss", delay)
            else:
                self.logger.warning("Xbox console poll rate-limited; retrying in %ss", delay)
        else:
            if already:
                self.logger.debug("Xbox console poll still failing: %s", exc)
            else:
                # A traceback only for an unexpected (non-Xbox) exception —
                # an HTTP/transport failure is fully described by its message.
                # Called inside the active except block, so exc_info is the
                # real underlying exception.
                self.logger.error("Xbox console poll failed: %s", exc,
                                  exc_info=exc.key == UNEXPECTED_ERROR_KEY)

    def _sync_console_device(self, dev, console, now_iso):
        """Write only changed console states. The focused title is resolved
        only while the console is On (one ``/consoles/{id}`` call, then
        installedApps only for a new aumid); a status or installedApps
        failure leaves the focused states exactly as they were and never
        fails the power-state update."""
        current = dev.states
        on = xb_consoles.is_on(console.power_state)
        display = xb_consoles.display_text(console.power_state)
        focus_name = current.get("focusedTitleName", "")
        focus_id = current.get("focusedTitleId", "")

        if on:
            try:
                status = xb_consoles.fetch_console_status(self._api, self._auth, console.id,
                                                           logger=self.logger)
            except XboxError as exc:
                self._log_console_detail_failure_once(
                    console.id, "status",
                    f"Xbox console status fetch failed for '{dev.name}' (states left "
                    f"as-is): {exc}")
                status = None
            except Exception as exc:  # pylint: disable=broad-except
                # Not an XboxError: a bug in our own code, not a normal API
                # failure — worth a traceback the first time.
                self._log_console_detail_failure_once(
                    console.id, "status",
                    f"Xbox console status fetch failed for '{dev.name}' (unexpected "
                    f"{type(exc).__name__}; states left as-is): {exc}", unexpected=True)
                status = None
            else:
                if status is None:
                    # Auth lost mid-poll — a failure, not a clean "nothing to
                    # report"; the flag must NOT be cleared.
                    self._log_console_detail_failure_once(
                        console.id, "status",
                        f"Xbox console status fetch failed for '{dev.name}' (not authorized; "
                        f"states left as-is)")
                else:
                    self._clear_console_detail_failure(console.id, "status")
            if status is not None:
                aumid = status.focus_app_aumid
                if aumid is None:
                    self._log_focus_unknown_once(dev, console.id)
                elif aumid:
                    self._clear_focus_unknown(console.id)
                    resolved = self._resolve_focused_title(console.id, aumid)
                    if resolved is not None:
                        focus_name, focus_id = resolved
                    # else: installedApps fetch failed — leave as-is (see below)
                else:
                    self._clear_focus_unknown(console.id)
                    focus_name, focus_id = "", ""       # at the home menu / dashboard
            # else: status fetch failed — leave focus_name/focus_id as-is
        else:
            focus_name, focus_id = "", ""

        batch = []
        for key, value in (("powerState", console.power_state),
                           ("consoleName", console.name),
                           ("consoleType", console.console_type),
                           ("focusedTitleName", focus_name),
                           ("focusedTitleId", focus_id)):
            if current.get(key) != value:
                batch.append({"key": key, "value": value})

        # A real transition; also stamped on the very first poll (previous
        # powerState is "" then), so the state is never left blank.
        if current.get("powerState") != console.power_state:
            batch.append({"key": "lastPowerChange", "value": now_iso})

        # Refresh onOffState whenever its value OR its shown text changed — the
        # text carries the focused title while On (same rule as _sync_device).
        display_value = focus_name if (on and focus_name) else display
        if (current.get("onOffState") != on
                or current.get("powerState") != console.power_state
                or current.get("focusedTitleName") != focus_name):
            batch.append({"key": "onOffState", "value": on, "uiValue": display_value})

        if batch:
            dev.updateStatesOnServer(batch)
        dev.updateStateOnServer("lastPoll", value=now_iso)   # heartbeat, matches presence

    def _resolve_focused_title(self, console_id, aumid):
        """Resolve ``aumid`` -> ``(titleName, titleId)`` via a cached
        installedApps lookup, keyed per console id. A miss for a specific
        aumid is cached too so an unmatched aumid never re-fetches every
        poll; the app list itself is only refetched when a new, unseen aumid
        appears. Returns ``None`` (meaning: leave state unchanged) whenever
        the installedApps fetch itself fails — including a ``None`` result
        (not authorized), which is a failure and must NOT be cached as a
        miss, only a genuine app list is."""
        cache = self._console_apps.setdefault(console_id, {})
        if aumid in cache:
            return cache[aumid] or ("", "")
        try:
            apps = xb_consoles.fetch_installed_apps(self._api, self._auth, console_id,
                                                    logger=self.logger)
        except XboxError as exc:
            self._log_console_detail_failure_once(
                console_id, "installed apps",
                f"Xbox installed-apps fetch failed for console {console_id} (states left "
                f"as-is): {exc}")
            return None
        except Exception as exc:  # pylint: disable=broad-except
            self._log_console_detail_failure_once(
                console_id, "installed apps",
                f"Xbox installed-apps fetch failed for console {console_id} (unexpected "
                f"{type(exc).__name__}; states left as-is): {exc}", unexpected=True)
            return None
        if apps is None:
            self._log_console_detail_failure_once(
                console_id, "installed apps",
                f"Xbox installed-apps fetch failed for console {console_id} (not authorized; "
                f"states left as-is)")
            return None
        self._clear_console_detail_failure(console_id, "installed apps")
        for app in apps:
            if app.aumid:
                cache[app.aumid] = (app.name, app.title_id)
        if aumid not in cache:
            cache[aumid] = None            # cache the miss too — app list was real
        return cache[aumid] or ("", "")

    def _log_console_detail_failure_once(self, console_id, kind, message, unexpected=False):
        """Warn once per console per failure episode for a status/installed
        apps fetch failure (reset on the next success), debug thereafter —
        same shape as _log_console_failure_once for the console-list call.
        ``unexpected=True`` (a non-XboxError — a bug, not a normal API
        failure) logs with a traceback the first time."""
        key = (console_id, kind)
        with self._dev_lock:
            already = self._console_detail_error_logged.get(key, False)
            self._console_detail_error_logged[key] = True
        if already:
            self.logger.debug(message)
        elif unexpected:
            self.logger.error(message, exc_info=True)
        else:
            self.logger.warning(message)

    def _log_focus_unknown_once(self, dev, console_id):
        """Debug-log once per console per episode when a status response is
        missing ``focusAppAumid`` entirely (unknown focus — states left
        as-is), so this doesn't spam every poll for a console that never
        reports it."""
        with self._dev_lock:
            already = console_id in self._console_focus_unknown_logged
            self._console_focus_unknown_logged.add(console_id)
        if not already:
            self.logger.debug(
                "Xbox console '%s': focusAppAumid absent from status (leaving focused "
                "title as-is)", dev.name)

    def _clear_focus_unknown(self, console_id):
        with self._dev_lock:
            self._console_focus_unknown_logged.discard(console_id)

    def _log_console_sync_failure_once(self, dev, exc):
        """Error-log (with traceback) once per device per failure episode
        when writing a console device's states fails, debug thereafter —
        same shape as _log_console_detail_failure_once, keyed by device id."""
        with self._dev_lock:
            already = self._console_sync_error_logged.get(dev.id, False)
            self._console_sync_error_logged[dev.id] = True
        if already:
            self.logger.debug("Xbox console '%s': state update still failing: %s",
                              dev.name, exc, exc_info=True)
        else:
            self.logger.error("Xbox console '%s': state update failed: %s", dev.name, exc,
                              exc_info=True)

    def _clear_console_sync_failure(self, dev_id):
        with self._dev_lock:
            self._console_sync_error_logged.pop(dev_id, None)

    def _clear_console_detail_failure(self, console_id, kind):
        with self._dev_lock:
            self._console_detail_error_logged.pop((console_id, kind), None)

    # -- Client construction -------------------------------------------------
    def _prefs_dir(self):
        try:
            return os.path.join(indigo.server.getInstallFolderPath(), "Preferences", "Plugins")
        except Exception:  # pylint: disable=broad-except
            return os.path.join(os.path.expanduser("~"), ".indigo-xbox")

    def _token_path(self):
        return os.path.join(self._prefs_dir(), TOKEN_FILENAME)

    def _sessions_path(self):
        return os.path.join(self._prefs_dir(), SESSIONS_FILENAME)

    def _history_path(self):
        return os.path.join(self._prefs_dir(), HISTORY_FILENAME)

    def _title_image(self, caller_xuid, title_id):
        """Resolve a title's box-art URL, cached per titleId for the plugin's
        lifetime. A cached ``""`` (including after a failed fetch) means "don't
        try again". Never raises — a fetch failure logs at debug and yields
        ``""`` so presence sync always completes."""
        if not title_id or title_id == xc.DASHBOARD_TITLE_ID or not caller_xuid:
            return ""
        if title_id in self._title_images:
            return self._title_images[title_id]
        try:
            url = xb_presence.fetch_title_image(self._api, self._auth, caller_xuid,
                                                title_id, logger=self.logger) or ""
        except Exception as exc:  # pylint: disable=broad-except
            self.logger.debug("Xbox title image fetch failed for %s: %s", title_id, exc)
            url = ""
        self._title_images[title_id] = url
        return url

    def _build_client(self, client_id):
        api = XboxAPI(logger=self.logger)
        auth = XboxAuth(api, self._token_path(), client_id, logger=self.logger)
        return api, auth

    def _rebuild_client(self):
        client_id = self.pluginPrefs.get("clientId", "").strip()
        with self._client_lock:
            if self._auth is not None and self._auth.matches(client_id):
                return                        # unchanged client: no-op supersession
            if self._auth is not None:
                self._auth.mark_stale()       # a late worker must not persist a superseded result
            self._api, self._auth = self._build_client(client_id)

    # -- Config UI -----------------------------------------------------------
    def getPrefsUiValues(self, *args, **kwargs):  # pylint: disable=unused-argument
        values = super().getPrefsUiValues(*args, **kwargs) if hasattr(
            super(), "getPrefsUiValues") else self.pluginPrefs
        try:
            state = self._auth.state() if self._auth else xc.STATE_UNAUTHORIZED
            values["authStatus"] = _STATE_TEXT.get(state, "")
        except Exception as exc:  # pylint: disable=broad-except
            self.logger.debug("Xbox: auth status unavailable for config dialog (%s)",
                              type(exc).__name__)
        return values

    def authorizeButtonPressed(self, valuesDict, typeId="", devId=0):  # noqa: N803, ARG002
        client_id = valuesDict.get("clientId", "").strip()
        if not client_id:
            valuesDict["authStatus"] = "Enter your Azure Application (client) ID first."
            return valuesDict

        # Supersede any in-flight authorization before starting a new one.
        self._stop_auth.set()
        self._stop_auth = threading.Event()
        with self._client_lock:
            if self._auth is not None:
                self._auth.mark_stale()
            self._api, self._auth = self._build_client(client_id)
            auth = self._auth

        try:
            info = auth.start_device_flow()
            valuesDict["authInstructions"] = info.get("verification_uri") or ""
            valuesDict["authUserCode"] = info.get("user_code") or ""
            valuesDict["authStatus"] = "Waiting for you to authorize in the browser…"
            self._start_device_flow_worker(auth, client_id, self._stop_auth)
        except XboxError as exc:
            valuesDict["authStatus"] = (f"Authorization error: {exc}. Check the client ID is "
                                        "correct (consumers tenant, public client flows enabled), "
                                        "then press Authorize again.")
            self.logger.error("Xbox authorization error: %s — check the client ID and that the "
                              "Azure app allows public client flows", exc)
        return valuesDict

    def _start_device_flow_worker(self, auth, client_id, stop_event):
        def _worker():
            status, detail = auth.run_device_flow(should_stop=stop_event.is_set)
            if status == "success":
                self.logger.info("Xbox authorization successful (client %s)", redact(client_id))
            elif status == "denied":
                self.logger.error("Xbox authorization denied: %s", detail)
            elif status == "cancelled":
                self.logger.info("Xbox authorization cancelled")
            else:
                self.logger.error("Xbox authorization failed: %s", detail)

        self._auth_thread = threading.Thread(target=_worker, name="xbox-device-flow", daemon=True)
        self._auth_thread.start()

    def validatePrefsConfigUi(self, valuesDict):  # noqa: N803
        errors = indigo.Dict()
        if not valuesDict.get("clientId", "").strip():
            errors["clientId"] = "Enter your Azure Application (client) ID."
        interval = valuesDict.get("pollInterval", str(DEFAULT_POLL_INTERVAL))
        try:
            value = int(interval)
        except (TypeError, ValueError):
            errors["pollInterval"] = "Poll interval must be a whole number of seconds."
        else:
            if not MIN_POLL_INTERVAL <= value <= MAX_POLL_INTERVAL:
                errors["pollInterval"] = (f"Poll interval must be between {MIN_POLL_INTERVAL} "
                                          f"and {MAX_POLL_INTERVAL} seconds.")
        console_interval = valuesDict.get("consolePollInterval", str(DEFAULT_POLL_INTERVAL))
        try:
            console_value = int(console_interval)
        except (TypeError, ValueError):
            errors["consolePollInterval"] = "Poll interval must be a whole number of seconds."
        else:
            if not MIN_POLL_INTERVAL <= console_value <= MAX_POLL_INTERVAL:
                errors["consolePollInterval"] = (f"Poll interval must be between {MIN_POLL_INTERVAL} "
                                                 f"and {MAX_POLL_INTERVAL} seconds.")
        if len(errors) > 0:
            return (False, valuesDict, errors)
        return (True, valuesDict)

    def closedPrefsConfigUi(self, valuesDict, userCancelled):  # noqa: N803
        if userCancelled:
            return
        self.debug = valuesDict.get("showDebugInfo", False)
        self._poll_interval = _coerce_interval(valuesDict.get("pollInterval"))
        self._console_poll_interval = _coerce_interval(valuesDict.get("consolePollInterval"))
        self._rebuild_client()

    # -- Device lifecycle ----------------------------------------------------
    def deviceStartComm(self, dev):  # noqa: N803
        dev.stateListOrDisplayStateIdChanged()  # pick up state keys added in newer Devices.xml revisions
        if dev.deviceTypeId == "xboxConsole":
            console_id = (dev.pluginProps or {}).get("consoleId", "")
            with self._dev_lock:
                self._consoles[dev.id] = console_id
            self.logger.info("Xbox console device '%s' started (console=%s)",
                             dev.name, console_id)
            return
        xuid = (dev.pluginProps or {}).get("xuid", "")
        with self._dev_lock:
            self._tracked[dev.id] = xuid
        self.logger.info("Xbox device '%s' started (xuid=%s)", dev.name, redact(xuid))

    def deviceStopComm(self, dev):  # noqa: N803
        with self._dev_lock:
            self._tracked.pop(dev.id, None)
            console_id = self._consoles.pop(dev.id, None)
            self._errored.pop(dev.id, None)
        if console_id:
            self._console_apps.pop(console_id, None)

    def didDeviceCommPropertyChange(self, origDev, newDev):  # noqa: N803
        if newDev.deviceTypeId == "xboxConsole":
            return ((origDev.pluginProps or {}).get("consoleId")
                    != (newDev.pluginProps or {}).get("consoleId"))
        return (origDev.pluginProps or {}).get("xuid") != (newDev.pluginProps or {}).get("xuid")

    # -- Presence → device state sync ----------------------------------------
    def _sync_all(self, people):
        by_xuid = {p.xuid: p for p in people}
        self._last_people = people
        with self._dev_lock:
            tracked = dict(self._tracked)
        now_iso = _utc_now_iso()
        caller_xuid = self._caller_xuid()
        for dev_id, xuid in tracked.items():
            try:
                dev = indigo.devices[dev_id]
            except KeyError:
                continue                  # device deleted between poll and lookup
            except Exception as exc:  # pylint: disable=broad-except
                self.logger.exception(exc)      # lookup itself broke, not "gone"
                continue
            presence = by_xuid.get(xuid)
            if presence is None:
                self._mark_unknown(dev)
            else:
                self._clear_error(dev)
                self._sync_device(dev, presence, now_iso, caller_xuid)
        self._persist_sessions()

    def _caller_xuid(self):
        """The signed-in account's own xuid (for the user-scoped titlehub path),
        or ``""`` when unavailable. Never raises."""
        if not self._auth:
            return ""
        try:
            own = self._auth.own_identity()
        except Exception:  # pylint: disable=broad-except
            return ""
        return own[0] if own else ""

    def _mark_unknown(self, dev):
        self._set_device_error(dev, "gamertag not visible on this account")

    def _set_device_error(self, dev, message):
        """Set an error state on ``dev`` once per error episode, but also
        whenever the message itself changes — a device stuck on an earlier
        message (e.g. "console list unavailable") must still show a
        different one (e.g. "not authorized") the moment that's what's
        actually wrong (shared by the presence and console paths — dev.id is
        unique across both)."""
        with self._dev_lock:
            previous = self._errored.get(dev.id)
            changed = previous is None or previous != message
            if changed:
                self._errored[dev.id] = message
        if changed:
            dev.setErrorStateOnServer(message)
        return changed

    def _clear_error(self, dev):
        with self._dev_lock:
            was_errored = dev.id in self._errored
            self._errored.pop(dev.id, None)
        if was_errored:
            dev.setErrorStateOnServer("")

    def _sync_device(self, dev, presence, now_iso, caller_xuid):
        """Write only changed states so SQL Logger records real transitions, not
        every poll. The sensor's on/off display carries the title (when playing)
        or the presence state as its uiValue."""
        current = dev.states
        in_game = presence.in_game
        online = presence.online
        title_name = (presence.primary_title_text or presence.presence_text) if in_game else ""

        # Feed the session model this poll's in-game boolean; the returned stats
        # (start / live minutes / last / today) are written change-only below.
        stats = self._sessions.update(presence.xuid, in_game)
        # Feed the finer-grained segment tracker (one segment per title); it
        # persists a row on segment close only. A title switch closes+reopens.
        self._segments.update(presence.xuid, in_game, presence.primary_title_id,
                              presence.primary_title_text or "", presence.gamertag)
        # Box art only for a real, non-dashboard title — resolved inline into the
        # state tuple below (cache-guarded → one titlehub call per titleId for
        # the plugin's lifetime; a failure yields "" and never breaks the poll).

        batch = []
        # Transition to offline: stamp lastSeen once, on the online→offline edge.
        if current.get("online") and not online:
            batch.append({"key": "lastSeen", "value": now_iso})

        for key, value in (("online", online),
                           ("presenceState", presence.state),
                           ("titleName", title_name),
                           ("titleId", presence.primary_title_id),
                           ("device", presence.device),
                           # Rich presence detail (peoplehub decorations)
                           ("richPresenceText", presence.rich_presence_text if in_game else ""),
                           ("isBroadcasting", bool(presence.is_broadcasting) if online else False),
                           ("inMultiplayer", bool(presence.in_multiplayer) if online else False),
                           # Profile extras (persistent; not gated on game state)
                           ("gamerScore", presence.gamer_score),
                           ("accountTier", presence.account_tier),
                           ("gamerPicUrl", presence.gamer_pic_url),
                           ("displayName", presence.display_name),
                           # Title box art
                           ("titleImageUrl",
                            self._title_image(caller_xuid, presence.primary_title_id)
                            if in_game else ""),
                           # Session stats
                           ("sessionStartedAt", stats.session_started_at),
                           ("sessionMinutes", stats.session_minutes),
                           ("lastSessionMinutes", stats.last_session_minutes),
                           ("todayMinutes", stats.today_minutes)):
            if current.get(key) != value:
                batch.append({"key": key, "value": value})

        # Refresh the on/off sensor state (with its display text) whenever the
        # value OR the shown text changed — the text is derived from titleName /
        # presenceState, so a title change while still in-game still refreshes it.
        display_changed = (current.get("onOffState") != in_game
                           or current.get("titleName") != title_name
                           or current.get("presenceState") != presence.state)
        if display_changed:
            display = title_name if in_game else presence.state
            batch.append({"key": "onOffState", "value": in_game, "uiValue": display})

        if batch:
            dev.updateStatesOnServer(batch)
        dev.updateStateOnServer("lastPoll", value=now_iso)   # heartbeat, not session history

    # -- Device ConfigUI -----------------------------------------------------
    def listPeople(self, filter="", valuesDict=None, typeId="", targetId=0):  # noqa: A002, N803, ARG002
        """Dynamic menu of people visible on the account: ``gamertag (xuid)``.

        Uses the last successful poll's list; if that is empty (e.g. just after
        startup) and we are authorized, does one live fetch so the picker is not
        needlessly blank."""
        people = self._last_people
        if not people and self._auth and self._auth.is_authorized():
            try:
                fetched = xb_presence.fetch_presence(self._api, self._auth, logger=self.logger)
                people = fetched or []
                if people:
                    self._last_people = people
            except XboxError as exc:
                self.logger.warning("Xbox: could not load people list: %s", exc)
                return []
        options = [(p.xuid, f"{p.gamertag} ({p.xuid})") for p in people if p.xuid]
        options.sort(key=lambda item: item[1].lower())
        own = self._auth.own_identity() if self._auth else None
        if own:
            own_xuid, own_gamertag = own
            options = [opt for opt in options if opt[0] != own_xuid]
            options.insert(0, (own_xuid, f"Me — {own_gamertag}" if own_gamertag else "Me"))
        return options

    def listConsoles(self, filter="", valuesDict=None, typeId="", targetId=0):  # noqa: A002, N803, ARG002
        """Dynamic menu of consoles on the account: ``name (consoleType)``.

        Always does a live fetch (unlike :meth:`listPeople`, there is no cheap
        "last poll" to fall back on while no console device is configured
        yet — consoles are never polled until one exists)."""
        if not (self._auth and self._auth.is_authorized()):
            self.logger.warning("Xbox: not authorized — authorize the plugin first.")
            return []
        try:
            consoles = xb_consoles.fetch_consoles(self._api, self._auth, logger=self.logger)
        except XboxError as exc:
            self.logger.warning("Xbox: could not load console list: %s", exc)
            if exc.key == "RemoteManagementDisabled":
                self.logger.warning("Xbox: enable Settings -> Devices & connections -> Remote "
                                    "features on the console, then try again.")
            return []
        except Exception as exc:  # pylint: disable=broad-except
            self.logger.exception(exc)
            return []
        if consoles is None:
            # is_authorized() passed above but the header was gone by the
            # time fetch_consoles actually ran (auth lost mid-call).
            self.logger.warning("Xbox: console list unavailable — authorization lost; "
                                "authorize the plugin again.")
            return []
        if not consoles:
            self.logger.warning("Xbox: no consoles on this Xbox account — is Remote features "
                                "enabled?")
            return []
        self._last_consoles = consoles
        options = [(c.id, f"{c.name} ({c.console_type})") for c in consoles if c.id]
        options.sort(key=lambda item: item[1].lower())
        return options

    def validateDeviceConfigUi(self, valuesDict, typeId, devId):  # noqa: N803, ARG002
        if typeId == "xboxConsole":
            return self._validate_console_device_config(valuesDict)
        manual = valuesDict.get("manualXuid", "").strip()
        selected = valuesDict.get("person", "").strip()
        xuid = manual or selected
        if not xuid:
            errors = indigo.Dict()
            errors["person"] = "Choose a person or enter an xuid."
            return (False, valuesDict, errors)
        gamertag = ""
        if selected and not manual:
            for person in self._last_people:
                if person.xuid == selected:
                    gamertag = person.gamertag
                    break
        valuesDict["xuid"] = xuid
        valuesDict["gamertag"] = gamertag or valuesDict.get("gamertag") or xuid
        return (True, valuesDict)

    def _validate_console_device_config(self, valuesDict):
        console_id = valuesDict.get("console", "").strip()
        if not console_id:
            errors = indigo.Dict()
            errors["console"] = "Choose a console."
            return (False, valuesDict, errors)
        name = ""
        for console in self._last_consoles:
            if console.id == console_id:
                name = console.name
                break
        valuesDict["consoleId"] = console_id
        valuesDict["consoleName"] = name or valuesDict.get("consoleName") or console_id
        return (True, valuesDict)

    # -- Menu items ----------------------------------------------------------
    def logTrackedPeople(self):
        if not (self._auth and self._auth.is_authorized()):
            self.logger.info("Xbox: not authorized yet — authorize the plugin first.")
            return
        own = self._auth.own_identity()
        if own:
            own_xuid, own_gamertag = own
            try:
                own_presence = xb_presence.fetch_own_presence(
                    self._api, self._auth, own_xuid=own_xuid, own_gamertag=own_gamertag,
                    logger=self.logger)
            except XboxError as exc:
                self.logger.error("Xbox: could not fetch your own presence — %s", exc)
                own_presence = None
            if own_presence is not None:
                title = own_presence.primary_title_text or own_presence.presence_text or "-"
                self.logger.info("  %s (xuid=%s) %s — %s%s (me)",
                                 own_presence.gamertag, own_presence.xuid,
                                 own_presence.state, title, _score_suffix(own_presence))
        try:
            people = xb_presence.fetch_presence(self._api, self._auth, logger=self.logger)
        except XboxError as exc:
            self.logger.error("Xbox: could not fetch presence — %s", exc)
            return
        if not people:
            self.logger.info("Xbox: no people visible on this account.")
            return
        self._last_people = people
        self.logger.info("Xbox: %d person(s) visible:", len(people))
        for person in people:
            title = person.primary_title_text or person.presence_text or "-"
            self.logger.info("  %s (xuid=%s) %s — %s%s",
                             person.gamertag, person.xuid, person.state, title,
                             _score_suffix(person))

    # -- Device actions (Status Request / console power) ---------------------
    def actionControlUniversal(self, action, dev):  # noqa: N803
        if action.deviceAction == indigo.kUniversalAction.RequestStatus:
            if dev.deviceTypeId == "xboxConsole":
                self.logger.debug("Xbox console '%s': status request — polling now", dev.name)
                self._poll_consoles_now()
            else:
                self.logger.debug("Xbox: status request for '%s' (nothing to do — presence "
                                  "is polled on its own schedule)", dev.name)

    def powerOnConsole(self, action, dev=None):  # noqa: N802, N803, ARG002
        self._send_console_power_command(dev, xc.POWER_COMMAND_WAKE_UP, "power on")

    def powerOffConsole(self, action, dev=None):  # noqa: N802, N803, ARG002
        self._send_console_power_command(dev, xc.POWER_COMMAND_TURN_OFF, "power off")

    def _send_console_power_command(self, dev, command, label):
        if dev is None:
            self.logger.error("Xbox console %s failed — no device selected", label)
            return
        console_id = (dev.pluginProps or {}).get("consoleId", "")
        if not console_id:
            self.logger.error("Xbox console '%s': %s failed — device is not configured with "
                              "a console", dev.name, label)
            return
        auth, api = self._auth, self._api
        if not (auth and api and auth.is_authorized()):
            self.logger.error("Xbox console '%s': %s failed — not authorized", dev.name, label)
            return
        try:
            result = xb_consoles.send_power_command(api, auth, console_id, command,
                                                     self._session_id, logger=self.logger)
        except XboxError as exc:
            self.logger.error("Xbox console '%s': %s failed — %s", dev.name, label, exc)
            return
        if result is None:
            # No XBL header (auth lost between the is_authorized() check
            # above and the call) — not a success, must not read as one.
            self.logger.error("Xbox console '%s': %s failed — not authorized", dev.name, label)
            return
        self.logger.info("Xbox console '%s': %s sent", dev.name, label)
        self._pull_console_poll_forward(CONSOLE_POWER_ACTION_FOLLOWUP_DELAY)

    # -- Usage-charts HTTP endpoint (IWS hidden action) ----------------------
    def http_chart_data(self, action, dev=None, caller_waiting_for_result=None):  # noqa: N803, ARG002
        """Serve the charts page's data: live device states for each tracked
        person plus the last :data:`CHART_DATA_DAYS` days of play segments.

        Returns only presence/usage data (no tokens or other secrets)."""
        try:
            body = self._build_chart_data()
        except Exception as exc:  # pylint: disable=broad-except
            self.logger.exception(exc)
            return self._chart_reply(500, {"error": "internal_error", "message": str(exc)})
        return self._chart_reply(200, body)

    def _build_chart_data(self):
        with self._dev_lock:
            tracked = dict(self._tracked)
        people = []
        for dev_id, xuid in tracked.items():
            try:
                dev = indigo.devices[dev_id]
            except Exception:  # pylint: disable=broad-except
                continue                  # device deleted between capture and lookup
            people.append(_chart_person(dev, xuid))
        people.sort(key=lambda person: (person["gamertag"] or "").lower())
        segments = []
        if self._history is not None:
            cutoff = (datetime.now().date() - timedelta(days=CHART_DATA_DAYS)).isoformat()
            segments = self._history.segments_since(cutoff)
        return {"people": people, "segments": segments, "generated_at": _utc_now_iso()}

    @staticmethod
    def _chart_reply(status, body):
        reply = indigo.Dict()
        reply["status"] = status
        reply["headers"] = indigo.Dict({"Content-Type": "application/json"})
        reply["content"] = json.dumps(body)
        return reply


def _chart_person(dev, xuid):
    """Shape one tracked device's states into the charts payload's person dict."""
    states = dev.states
    gamertag = (dev.pluginProps or {}).get("gamertag") or states.get("displayName") or xuid
    return {
        "xuid": xuid,
        "gamertag": gamertag,
        "displayName": states.get("displayName") or "",
        "gamerPicUrl": states.get("gamerPicUrl") or "",
        "live": {
            "online": bool(states.get("online")),
            "inGame": bool(states.get("onOffState")),
            "titleName": states.get("titleName") or "",
            "titleImageUrl": states.get("titleImageUrl") or "",
            "sessionMinutes": _as_int(states.get("sessionMinutes")),
            "todayMinutes": _as_int(states.get("todayMinutes")),
        },
    }


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _coerce_interval(value):
    """Clamp a prefs poll-interval to the allowed range, defaulting on garbage."""
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return DEFAULT_POLL_INTERVAL
    return max(MIN_POLL_INTERVAL, min(MAX_POLL_INTERVAL, seconds))


def _utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def _score_suffix(presence):
    """A `` [Gold · 27210 GS]`` suffix for a log line, or ``""`` when neither the
    account tier nor gamerscore is known."""
    tier = presence.account_tier
    score = presence.gamer_score
    if not tier and not score:
        return ""
    parts = []
    if tier:
        parts.append(tier)
    if score:
        parts.append(f"{score} GS")
    return f" [{' · '.join(parts)}]"
