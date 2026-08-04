"""OAuth for Xbox Live: MSA Device Flow + token persistence + the Xbox Live
token chain (user token → XSTS), cached against each token's ``NotAfter``.

Design mirrors Home Connect's ``hc_auth`` (a workspace pattern):

* MSA tokens live in a standalone JSON file (0600), keyed by ``client_id`` so
  switching clients keeps old tokens; each entry stores an absolute expiry.
* This module owns **no thread**. The caller drives MSA refresh via
  :meth:`refresh_if_needed` and the interactive device-flow poll loop via
  :meth:`run_device_flow` (blocking) / :meth:`start_device_flow` +
  :meth:`poll_device_flow`.
* The Xbox Live chain (user token, then XSTS) is derived on demand from the MSA
  access token and cached in memory until its ``NotAfter`` approaches or a 401
  invalidates it. The presence layer asks for :meth:`xbl_header` each call.

All :class:`XboxAuth` instances on the same token file share one process-wide
:class:`TokenStore` (the sole writer: ``mkstemp`` → ``chmod 0600`` →
``os.replace``, merge-on-write). Never imports ``indigo``; all HTTP goes through
an injected :class:`~xb_api.XboxAPI`.
"""
import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timezone

import xb_constants as xc
from xb_api import XboxError, redact

# ---------------------------------------------------------------------------
# Shared, process-wide token store (one per absolute path)
# ---------------------------------------------------------------------------
_STORES = {}
_STORES_GUARD = threading.Lock()


def get_token_store(path, logger=None):
    """Return the shared :class:`TokenStore` for ``path`` (created on first use)."""
    abspath = os.path.abspath(path)
    with _STORES_GUARD:
        store = _STORES.get(abspath)
        if store is None:
            store = TokenStore(abspath, logger=logger)
            _STORES[abspath] = store
        return store


class TokenStore:
    """The single writer for one token file. Atomic writes; merge-on-write."""

    def __init__(self, path, logger=None):
        self._path = os.path.abspath(path)
        self._logger = logger or logging.getLogger("xb_auth")
        self._lock = threading.RLock()
        self._refresh_locks = {}
        self._refresh_guard = threading.Lock()
        self._last_refresh_ts = {}
        self._entries = self._read_disk()

    @property
    def path(self):
        return self._path

    def _read_disk(self):
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if isinstance(v, dict)}

    def _atomic_write(self, data):
        directory = os.path.dirname(self._path) or "."
        try:
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(data, handle)
                os.chmod(tmp, 0o600)          # restrict BEFORE it becomes the real file
                os.replace(tmp, self._path)   # atomic: readers see old or new, never partial
            except OSError:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError as exc:
            self._logger.warning("Xbox token save failed (store left intact): %s", exc)

    def get(self, client_id):
        with self._lock:
            entry = self._entries.get(client_id)
            return dict(entry) if entry is not None else None

    def keys(self):
        with self._lock:
            return list(self._entries.keys())

    def set(self, client_id, entry):
        with self._lock:
            merged = self._read_disk()        # pick up other clients' keys first
            merged.update(self._entries)      # our in-process view wins for our keys
            merged[client_id] = dict(entry)
            self._entries = merged
            self._atomic_write(merged)

    def refresh_lock(self, client_id):
        with self._refresh_guard:
            lock = self._refresh_locks.get(client_id)
            if lock is None:
                lock = threading.Lock()
                self._refresh_locks[client_id] = lock
            return lock

    def note_refresh(self, client_id, ts):
        with self._lock:
            self._last_refresh_ts[client_id] = ts

    def last_refresh(self, client_id):
        with self._lock:
            return self._last_refresh_ts.get(client_id, float("-inf"))


def _expires_at(entry):
    """The entry's numeric expiry, or 0 (= due immediately) when corrupted."""
    value = entry.get("expires_at")
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def parse_not_after(value):
    """Parse an Xbox ``NotAfter`` timestamp to epoch seconds (0 on failure).

    Xbox stamps carry a trailing ``Z`` and up to 7 fractional digits
    (``2999-10-10T19:06:35.5251155Z``) — neither of which ``fromisoformat``
    accepts on Python 3.10, so parse defensively. A 0 return means "treat as
    expired" so the chain simply re-derives rather than trusting a bad stamp.
    """
    if not value:
        return 0.0
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1]
    if "." in text:
        base, frac = text.split(".", 1)
        frac = "".join(ch for ch in frac if ch.isdigit())[:6]
        text = f"{base}.{frac}" if frac else base
    fmt = "%Y-%m-%dT%H:%M:%S.%f" if "." in text else "%Y-%m-%dT%H:%M:%S"
    try:
        parsed = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
    except ValueError:
        return 0.0
    return parsed.timestamp()


class XboxAuth:
    """Holds one client id's MSA device flow + the derived Xbox Live token chain."""

    def __init__(self, api, token_path, client_id, logger=None,
                 now=time.time, sleep=time.sleep):
        self._api = api
        self._token_path = os.path.abspath(token_path)
        self._store = get_token_store(self._token_path, logger=logger)
        self._client_id = client_id or ""
        self._logger = logger or logging.getLogger("xb_auth")
        self._now = now
        self._sleep = sleep

        self._lock = threading.RLock()
        self._device = None               # active device-flow context
        self._device_interval = xc.DEFAULT_DEVICE_INTERVAL
        self._last_refresh_attempt = 0.0
        self._refresh_backoff_until = 0.0
        self._refresh_failures = 0
        self._stale = False
        self._xbl = None                  # cached {token, uhs, xid, gtg, not_after}
        self._state = (xc.STATE_AUTHORIZED if self._store.get(self._client_id)
                       else xc.STATE_UNAUTHORIZED)

    # -- Lifecycle -----------------------------------------------------------
    def mark_stale(self):
        """Supersede this instance: a late device-flow result won't be persisted."""
        with self._lock:
            self._stale = True

    def matches(self, client_id):
        """True when this instance already represents this client id.

        An unchanged-value prefs save is a no-op supersession and must NOT mark
        the instance stale — a device-flow authorization may be pending on it."""
        return self._client_id == (client_id or "")

    # -- Public state accessors ---------------------------------------------
    @property
    def client_id(self):
        return self._client_id

    def state(self):
        with self._lock:
            return self._state

    def is_authorized(self):
        return self._store.get(self._client_id) is not None

    def access_token(self):
        entry = self._store.get(self._client_id)
        return entry.get("access_token") if entry else None

    def access_expires_at(self):
        entry = self._store.get(self._client_id)
        return entry.get("expires_at") if entry else None

    # -- MSA token storage ---------------------------------------------------
    def _store_token(self, data):
        access = data["access_token"]
        refresh = data.get("refresh_token")
        expires_in = int(data.get("expires_in", 3600))
        scope = data.get("scope") or xc.SCOPE
        entry = {
            "access_token": access,
            "refresh_token": refresh,
            "expires_at": self._now() + expires_in,
            "scopes": scope.split() if isinstance(scope, str) else list(scope),
            "obtained_at": self._now(),
        }
        self._store.set(self._client_id, entry)   # atomic + merges other clients
        with self._lock:
            self._state = xc.STATE_AUTHORIZED
            self._xbl = None                       # new MSA token → re-derive the chain
        self._logger.debug("Stored Xbox MSA token: access=%s refresh=%s expires_in=%ds",
                           redact(access), redact(refresh), expires_in)

    # -- Device Flow ---------------------------------------------------------
    def start_device_flow(self):
        """Request a verification URI + user code. Stashes the device code."""
        form = {"client_id": self._client_id, "scope": xc.SCOPE}
        data = self._api.post_form(xc.DEVICE_CODE_URL, form)
        interval = int(data.get("interval", xc.DEFAULT_DEVICE_INTERVAL))
        expires_in = int(data.get("expires_in", xc.DEFAULT_DEVICE_EXPIRES))
        uri = data.get("verification_uri")
        user_code = data.get("user_code")
        with self._lock:
            self._device = {"device_code": data["device_code"],
                            "expires_at": self._now() + expires_in}
            self._device_interval = interval
            self._state = xc.STATE_PENDING
        self._logger.info("Xbox authorization: visit %s and enter code %s", uri, user_code)
        return {"verification_uri": uri, "user_code": user_code,
                "interval": interval, "expires_in": expires_in}

    def poll_device_flow(self):
        """One token poll. Returns ``(status, detail)`` where status is one of
        ``pending`` / ``slow_down`` / ``success`` / ``denied`` / ``expired`` /
        ``cancelled`` / ``error``."""
        with self._lock:
            device = self._device
        if not device:
            return ("error", "no active device flow")
        form = {
            "client_id": self._client_id,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device["device_code"],
        }
        try:
            data = self._api.post_form(xc.TOKEN_URL, form)
        except XboxError as exc:
            key = exc.key
            if key == "authorization_pending":
                return ("pending", None)
            if key == "slow_down":
                with self._lock:
                    self._device_interval += xc.SLOW_DOWN_STEP
                return ("slow_down", None)
            if key == "authorization_declined":
                self._clear_device(xc.STATE_UNAUTHORIZED)
                return ("denied", exc.description or "authorization declined")
            if key == "expired_token":
                self._clear_device(xc.STATE_UNAUTHORIZED)
                return ("expired", exc.description or "device code expired")
            self._logger.error("Xbox device-flow poll failed: %s", exc)
            return ("error", str(exc))
        with self._lock:
            stale = self._stale
        if stale:
            self._clear_device(xc.STATE_UNAUTHORIZED)
            return ("cancelled", "instance superseded")
        self._store_token(data)
        self._clear_device(None)
        return ("success", None)

    def _clear_device(self, new_state):
        with self._lock:
            self._device = None
            if new_state is not None:
                self._state = new_state

    def run_device_flow(self, on_prompt=None, should_stop=None, max_restarts=1):
        """Blocking driver: poll until resolved, auto-restart once on expiry.

        Reuses a device code already stashed by :meth:`start_device_flow` (the
        one whose user code the caller is displaying) — starting a fresh flow
        here would orphan that code. Runs on the caller's thread.
        """
        restarts = 0
        with self._lock:
            device = self._device
            active = device is not None and self._now() < device["expires_at"]
        if not active:
            info = self.start_device_flow()
            if on_prompt:
                on_prompt(info)
        while True:
            if should_stop and should_stop():
                self._clear_device(xc.STATE_UNAUTHORIZED)
                return ("cancelled", None)
            self._sleep(self._device_interval)
            status, detail = self.poll_device_flow()
            if status in ("pending", "slow_down"):
                continue
            if status == "expired" and restarts < max_restarts:
                restarts += 1
                self._logger.info("Xbox device code expired; restarting authorization")
                info = self.start_device_flow()
                if on_prompt:
                    on_prompt(info)
                continue
            return (status, detail)

    # -- MSA refresh ---------------------------------------------------------
    def next_refresh_due(self):
        """Wall-clock time the MSA token should be refreshed, or ``None``."""
        entry = self._store.get(self._client_id)
        if not entry:
            return None
        return _expires_at(entry) - xc.REFRESH_WINDOW

    def refresh_if_needed(self, force=False):
        """Refresh the MSA access token when within :data:`REFRESH_WINDOW` of
        expiry (or ``force``). Honors the min interval and any active backoff.
        Returns True when a refresh happened (here or by a concurrent caller)."""
        entry = self._store.get(self._client_id)
        if not entry:
            return False
        with self._lock:
            if self._state == xc.STATE_AUTH_REQUIRED:
                return False              # dead grant: re-auth required, don't resubmit
            now = self._now()
            due_at = _expires_at(entry) - xc.REFRESH_WINDOW
            if not force and now < due_at:
                return False
            if now < self._refresh_backoff_until:
                return False
            if not force and (now - self._last_refresh_attempt) < xc.MIN_REFRESH_INTERVAL:
                return False
            self._last_refresh_attempt = now
        seen_access = entry.get("access_token")
        refresh_token = entry.get("refresh_token")
        if not refresh_token:
            return False
        return self._do_refresh(refresh_token, seen_access)

    def _do_refresh(self, refresh_token, seen_access):
        with self._store.refresh_lock(self._client_id):
            if (self._now() - self._store.last_refresh(self._client_id)) < xc.MIN_REFRESH_INTERVAL:
                return True
            current = self._store.get(self._client_id)
            if current and current.get("access_token") != seen_access:
                return True               # another caller already refreshed
            use_token = current.get("refresh_token") if current else refresh_token
            form = {
                "client_id": self._client_id,
                "grant_type": "refresh_token",
                "scope": xc.SCOPE,
                "refresh_token": use_token,
            }
            try:
                data = self._api.post_form(xc.TOKEN_URL, form)
            except XboxError as exc:
                if exc.status == 429 and exc.retry_after:
                    with self._lock:
                        self._refresh_backoff_until = self._now() + exc.retry_after
                    self._logger.warning("Xbox token refresh rate-limited; retrying in %ss",
                                         exc.retry_after)
                    return False
                if exc.key == "invalid_grant":
                    with self._lock:
                        self._state = xc.STATE_AUTH_REQUIRED
                    self._logger.error("Xbox authorization lost (revoked or expired). "
                                       "Re-authorize the plugin in its configuration.")
                    return False
                self._refresh_failures += 1
                backoff = min(xc.REFRESH_BACKOFF_BASE * (2 ** (self._refresh_failures - 1)),
                              xc.REFRESH_BACKOFF_MAX)
                with self._lock:
                    self._refresh_backoff_until = self._now() + backoff
                self._logger.error("Xbox token refresh failed: %s — next attempt in %ds",
                                   exc, backoff)
                return False
            self._store_token(data)
            self._store.note_refresh(self._client_id, self._now())
            self._refresh_failures = 0
            self._logger.info("Xbox access token refreshed")
            return True

    # -- Xbox Live token chain (user token → XSTS) ---------------------------
    def invalidate_xbl(self):
        """Drop the cached XSTS/user token so the next :meth:`xbl_header` re-derives.
        Called by the presence layer on a peoplehub 401."""
        with self._lock:
            self._xbl = None

    def xbl_header(self):
        """Return the ``XBL3.0 x=<uhs>;<token>`` header, deriving the chain if
        needed, or ``None`` when not authorized. Raises :class:`XboxError` on a
        chain failure so the caller logs one actionable line."""
        xbl = self._ensure_xbl()
        if not xbl:
            return None
        return f"XBL3.0 x={xbl['uhs']};{xbl['token']}"

    def own_identity(self):
        """Return ``(xuid, gamertag)`` for the signed-in account, or ``None``.

        Peoplehub's social graph never includes the caller's own account, so
        this is how the config UI and poll loop find the signed-in gamertag to
        offer/track it. Nothing new is persisted — ``xid``/``gtg`` already ride
        along in the XSTS ``DisplayClaims`` cached by :meth:`_ensure_xbl`;
        derives the chain on demand (e.g. before the first poll) and swallows a
        chain failure as "unavailable" rather than raising."""
        if not self.is_authorized():
            return None
        try:
            xbl = self._ensure_xbl()
        except XboxError:
            return None
        if not xbl or not xbl.get("xid"):
            return None
        return (xbl["xid"], xbl.get("gtg") or "")

    def _ensure_xbl(self):
        self.refresh_if_needed()          # make sure the MSA access token is fresh
        with self._lock:
            cached = self._xbl
            if cached and self._now() < (cached["not_after"] - xc.XSTS_MARGIN):
                return cached
        access = self.access_token()
        if not access:
            return None
        user_token = self._request_user_token(access)
        xbl = self._request_xsts_token(user_token)
        with self._lock:
            self._xbl = xbl
        return xbl

    def _request_user_token(self, access_token):
        payload = {
            "RelyingParty": xc.USER_RELYING_PARTY,
            "TokenType": "JWT",
            "Properties": {
                "AuthMethod": "RPS",
                "SiteName": "user.auth.xboxlive.com",
                "RpsTicket": f"d={access_token}",
            },
        }
        data = self._api.post_json(xc.USER_AUTH_URL, payload,
                                   headers={"x-xbl-contract-version": xc.XBL_CONTRACT_VERSION})
        return data["Token"]

    def _request_xsts_token(self, user_token):
        payload = {
            "RelyingParty": xc.XSTS_RELYING_PARTY,
            "TokenType": "JWT",
            "Properties": {"UserTokens": [user_token], "SandboxId": xc.SANDBOX_ID},
        }
        try:
            data = self._api.post_json(
                xc.XSTS_URL, payload,
                headers={"x-xbl-contract-version": xc.XBL_CONTRACT_VERSION})
        except XboxError as exc:
            if exc.status == 401:
                self._handle_xsts_denied(exc)
            raise
        claims = (data.get("DisplayClaims", {}).get("xui") or [{}])[0]
        return {
            "token": data["Token"],
            "uhs": claims.get("uhs", ""),
            "xid": claims.get("xid", ""),
            "gtg": claims.get("gtg", ""),
            "not_after": parse_not_after(data.get("NotAfter")),
        }

    def _handle_xsts_denied(self, exc):
        """Map an XSTS 401 XErr to an actionable log line; latch terminal ones."""
        message = xc.XERR_MESSAGES.get(exc.xerr)
        if message:
            self._logger.error("Xbox sign-in refused: %s", message)
        else:
            self._logger.error("Xbox sign-in refused (XErr %s): %s", exc.xerr, exc)
        if exc.xerr in xc.XERR_TERMINAL:
            with self._lock:
                self._state = xc.STATE_AUTH_REQUIRED
