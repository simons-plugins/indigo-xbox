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
from datetime import datetime, timedelta, timezone

try:
    import indigo
except ImportError:  # pragma: no cover - only importable inside the Indigo server
    indigo = None

import xb_constants as xc
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
        self._auth_thread = None
        self._stop_auth = threading.Event()
        # Guards the api/auth pair against races between the config UI thread
        # (rebuild) and runConcurrentThread (poll every pollInterval).
        self._client_lock = threading.RLock()
        # dev.id -> xuid for every enabled tracked device; and the ids currently
        # showing an error, so we clear it exactly once on recovery.
        self._tracked = {}
        self._errored = set()
        self._dev_lock = threading.RLock()
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
        try:
            while True:
                self._supervise()
                self.sleep(self._poll_interval)
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
        if len(errors) > 0:
            return (False, valuesDict, errors)
        return (True, valuesDict)

    def closedPrefsConfigUi(self, valuesDict, userCancelled):  # noqa: N803
        if userCancelled:
            return
        self.debug = valuesDict.get("showDebugInfo", False)
        self._poll_interval = _coerce_interval(valuesDict.get("pollInterval"))
        self._rebuild_client()

    # -- Device lifecycle ----------------------------------------------------
    def deviceStartComm(self, dev):  # noqa: N803
        dev.stateListOrDisplayStateIdChanged()  # pick up state keys added in newer Devices.xml revisions
        xuid = (dev.pluginProps or {}).get("xuid", "")
        with self._dev_lock:
            self._tracked[dev.id] = xuid
        self.logger.info("Xbox device '%s' started (xuid=%s)", dev.name, redact(xuid))

    def deviceStopComm(self, dev):  # noqa: N803
        with self._dev_lock:
            self._tracked.pop(dev.id, None)
            self._errored.discard(dev.id)

    def didDeviceCommPropertyChange(self, origDev, newDev):  # noqa: N803
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
            except Exception:  # pylint: disable=broad-except
                continue                  # device deleted between poll and lookup
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
        with self._dev_lock:
            already = dev.id in self._errored
            self._errored.add(dev.id)
        if not already:
            dev.setErrorStateOnServer("gamertag not visible on this account")

    def _clear_error(self, dev):
        with self._dev_lock:
            was_errored = dev.id in self._errored
            self._errored.discard(dev.id)
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

    def validateDeviceConfigUi(self, valuesDict, typeId, devId):  # noqa: N803, ARG002
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
