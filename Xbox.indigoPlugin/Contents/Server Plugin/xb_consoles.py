"""Xbox console (SmartGlass xccs) management: console list, per-console status,
installed apps, and power commands.

Parsing is pure (:func:`parse_console_list`, :func:`parse_console_status`,
:func:`parse_installed_apps`) and tested against real, id-stripped fixtures;
the ``fetch_*``/``send_power_command`` functions are the thin HTTP
orchestration on top, mirroring :mod:`xb_presence`'s one-shot 401 retry
exactly (a peoplehub-derived XSTS token can expire between derivation and
use, and the same applies here — no new auth path). Never imports ``indigo``.
"""
import logging
from dataclasses import dataclass

import xb_constants as xc
from xb_api import XboxError


@dataclass(frozen=True)
class ConsoleInfo:
    """One console entry from the xccs device list."""

    id: str  # noqa: A003 - matches the wire field name
    name: str
    console_type: str
    power_state: str
    remote_management_enabled: bool = False


@dataclass(frozen=True)
class ConsoleStatus:
    """A single console's ``/consoles/{id}`` status — only the fields the
    plugin needs (focused-title resolution; power state is already known
    from the list call that decided this device was worth a status call).

    ``focus_app_aumid`` is ``None`` when the field is absent from the payload
    (meaning: unknown — the caller must leave its focused-title states as
    they were), and ``""`` when the field is present but blank (the
    dashboard, with nothing focused)."""

    power_state: str
    focus_app_aumid: str  # None = unknown/absent, "" = dashboard, else an aumid
    playback_state: str = ""
    login_state: str = ""


@dataclass(frozen=True)
class InstalledApp:
    """One entry from a console's installed-apps list."""

    aumid: str
    title_id: str
    name: str
    is_game: bool = False


# -- Power-state display -------------------------------------------------------
# Unknown/missing states are deliberately absent from this map: the caller
# treats a miss as "unavailable" rather than guessing a display string.
_DISPLAY_TEXT = {
    xc.POWER_STATE_ON: "On",
    xc.POWER_STATE_STANDBY: "Standby",
    xc.POWER_STATE_OFF: "Off",
    xc.POWER_STATE_UPDATING: "Updating",
}


def is_on(power_state):
    """True only for the exact ``"On"`` power state (``ConnectedStandby`` is
    not "on" — a controller power-off observed live goes On -> ConnectedStandby,
    never Off)."""
    return power_state == xc.POWER_STATE_ON


def display_text(power_state):
    """Display text for a console's powerState, or ``None`` when the state is
    Unknown, missing, or unrecognised — the caller treats that as
    "unavailable", not as a value to show."""
    return _DISPLAY_TEXT.get(power_state)


# -- Parsing --------------------------------------------------------------------

def _check_status(payload, context):
    """Raise :class:`XboxError` when ``payload["status"]["errorCode"]`` is
    present and not ``"OK"`` — the code rides in both the message and
    ``XboxError.key`` so callers can branch on it without re-parsing text."""
    status = (payload or {}).get("status")
    if not isinstance(status, dict):
        return
    code = status.get("errorCode")
    if code and code != "OK":
        message = status.get("errorMessage") or ""
        text = f"xccs {context} error [{code}]"
        if message:
            text += f": {message}"
        raise XboxError(text, key=code)


def parse_console_list(payload):
    """Parse a ``GET /lists/devices`` response into a list of
    :class:`ConsoleInfo`. Raises :class:`XboxError` when the response's own
    ``status.errorCode`` is not ``"OK"`` (e.g. ``RemoteManagementDisabled``),
    when the payload is not a dict, or when ``result`` is missing/not a list
    — those are failed calls, not "no consoles". Only an explicit empty
    ``result`` list yields ``[]``."""
    if not isinstance(payload, dict):
        raise XboxError("xccs console list error: invalid response")
    _check_status(payload, "console list")
    result = payload.get("result")
    if not isinstance(result, list):
        raise XboxError("xccs console list error: missing result")
    consoles = []
    for item in result:
        if not isinstance(item, dict):
            continue
        consoles.append(ConsoleInfo(
            id=str(item.get("id") or ""),
            name=item.get("name") or "",
            console_type=item.get("consoleType") or "",
            power_state=item.get("powerState") or "",
            remote_management_enabled=bool(item.get("remoteManagementEnabled")),
        ))
    return consoles


def parse_console_status(payload):
    """Parse a ``GET /consoles/{id}`` response into a :class:`ConsoleStatus`.
    Raises :class:`XboxError` when ``status.errorCode`` is not ``"OK"``, when
    the payload is not a dict, or when ``powerState`` is missing/blank — a
    status response the caller can't even tell On from Off from is a failed
    call, not "everything blank"."""
    if not isinstance(payload, dict):
        raise XboxError("xccs console status error: invalid response")
    _check_status(payload, "console status")
    power_state = payload.get("powerState")
    if not power_state:
        raise XboxError("xccs console status error: missing powerState")
    return ConsoleStatus(
        power_state=power_state,
        focus_app_aumid=payload.get("focusAppAumid"),
        playback_state=payload.get("playbackState") or "",
        login_state=payload.get("loginState") or "",
    )


def parse_installed_apps(payload):
    """Parse a ``GET /lists/installedApps`` response into a list of
    :class:`InstalledApp`. Raises :class:`XboxError` when the response's own
    ``status.errorCode`` is not ``"OK"``, when the payload is not a dict, or
    when ``result`` is missing/not a list — those are failed calls, not "no
    apps installed". Only an explicit empty ``result`` list yields ``[]``."""
    if not isinstance(payload, dict):
        raise XboxError("xccs installed apps error: invalid response")
    _check_status(payload, "installed apps")
    result = payload.get("result")
    if not isinstance(result, list):
        raise XboxError("xccs installed apps error: missing result")
    apps = []
    for item in result:
        if not isinstance(item, dict):
            continue
        title_id = item.get("titleId")
        apps.append(InstalledApp(
            aumid=item.get("aumid") or "",
            title_id=str(title_id) if title_id is not None else "",
            name=item.get("name") or "",
            is_game=bool(item.get("isGame")),
        ))
    return apps


# -- HTTP orchestration -----------------------------------------------------

def _console_headers(header):
    return {
        "Authorization": header,
        "x-xbl-contract-version": xc.XCCS_CONTRACT_VERSION,
        "skillplatform": xc.XCCS_SKILL_PLATFORM,
        "Accept-Language": xc.ACCEPT_LANGUAGE,
    }


def fetch_consoles(api, auth, logger=None):
    """Fetch + parse the account's console list. Returns ``None`` when not
    authorized (no XBL header available). Same one-shot 401 retry as
    :func:`xb_presence.fetch_presence`; raises :class:`XboxError` otherwise."""
    logger = logger or logging.getLogger("xb_consoles")
    header = auth.xbl_header()
    if not header:
        return None
    try:
        payload = _get_console_list(api, header)
    except XboxError as exc:
        if exc.status != 401:
            raise
        logger.debug("Xbox console list 401 — re-deriving token chain and retrying once")
        auth.invalidate_xbl()
        header = auth.xbl_header()
        if not header:
            return None
        payload = _get_console_list(api, header)
    return parse_console_list(payload)


def _get_console_list(api, header):
    return api.get_json(xc.XCCS_CONSOLE_LIST_URL, headers=_console_headers(header))


def fetch_console_status(api, auth, console_id, logger=None):
    """Fetch + parse one console's status. Returns ``None`` when not
    authorized. Same one-shot 401 retry; raises :class:`XboxError` otherwise."""
    logger = logger or logging.getLogger("xb_consoles")
    header = auth.xbl_header()
    if not header:
        return None
    try:
        payload = _get_console_status(api, header, console_id)
    except XboxError as exc:
        if exc.status != 401:
            raise
        logger.debug("Xbox console status 401 — re-deriving token chain and retrying once")
        auth.invalidate_xbl()
        header = auth.xbl_header()
        if not header:
            return None
        payload = _get_console_status(api, header, console_id)
    return parse_console_status(payload)


def _get_console_status(api, header, console_id):
    url = xc.XCCS_CONSOLE_STATUS_URL.format(console_id=console_id)
    return api.get_json(url, headers=_console_headers(header))


def fetch_installed_apps(api, auth, console_id, logger=None):
    """Fetch + parse a console's installed-apps list. Returns ``None`` when
    not authorized. Same one-shot 401 retry; raises :class:`XboxError`
    otherwise."""
    logger = logger or logging.getLogger("xb_consoles")
    header = auth.xbl_header()
    if not header:
        return None
    try:
        payload = _get_installed_apps(api, header, console_id)
    except XboxError as exc:
        if exc.status != 401:
            raise
        logger.debug("Xbox installed-apps 401 — re-deriving token chain and retrying once")
        auth.invalidate_xbl()
        header = auth.xbl_header()
        if not header:
            return None
        payload = _get_installed_apps(api, header, console_id)
    return parse_installed_apps(payload)


def _get_installed_apps(api, header, console_id):
    url = xc.XCCS_INSTALLED_APPS_URL.format(console_id=console_id)
    return api.get_json(url, headers=_console_headers(header))


def send_power_command(api, auth, console_id, command, session_id, logger=None):
    """Send a ``Power`` command (``WakeUp``/``TurnOff``) to ``console_id``.
    Returns the parsed response dict, or ``None`` when not authorized. Same
    one-shot 401 retry; raises :class:`XboxError` on an HTTP failure, a
    non-``"OK"`` ``status.errorCode`` in the response body, or a response
    that isn't a dict at all (e.g. JSON ``null``) — ``None`` must keep
    meaning only "no auth header", never "unexpected body"."""
    logger = logger or logging.getLogger("xb_consoles")
    header = auth.xbl_header()
    if not header:
        return None
    payload = {
        "destination": "Xbox",
        "type": "Power",
        "command": command,
        "sessionId": session_id,
        "sourceId": xc.POWER_COMMAND_SOURCE_ID,
        "parameters": [{}],
        "linkedXboxId": console_id,
    }
    try:
        data = _post_power_command(api, header, payload)
    except XboxError as exc:
        if exc.status != 401:
            raise
        logger.debug("Xbox power command 401 — re-deriving token chain and retrying once")
        auth.invalidate_xbl()
        header = auth.xbl_header()
        if not header:
            return None
        data = _post_power_command(api, header, payload)
    if not isinstance(data, dict):
        raise XboxError("unexpected power command response")
    _check_status(data, "power command")
    return data


def _post_power_command(api, header, payload):
    return api.post_json(xc.XCCS_COMMANDS_URL, payload, headers=_console_headers(header))
