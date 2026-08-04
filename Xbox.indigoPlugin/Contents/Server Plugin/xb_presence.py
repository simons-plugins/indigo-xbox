"""Peoplehub presence: fetch the caller's social list and parse each person's
presence into a :class:`PersonPresence`.

Parsing is a pure function (:func:`parse_people`) tested against real peoplehub
fixtures; :func:`fetch_presence` is the thin HTTP orchestration on top. The
peoplehub ``presenceDetails`` list is PascalCase (``IsPrimary``, ``IsGame``,
``TitleId`` …) and may be null or missing — both are handled. Never imports
``indigo``.
"""
import logging
from dataclasses import dataclass, replace

import xb_constants as xc
from xb_api import XboxError


@dataclass
class PersonPresence:
    """One person's current Xbox Live presence, flattened for device sync.

    The trailing fields carry the richer decorations (rich presence,
    broadcasting, multiplayer) and profile extras (gamerscore/tier/pic). They
    all default so existing call sites and tests that build a bare presence keep
    working; peoplehub fills them from the ``detail``/``multiplayersummary``
    decorations, and the profile service fills them for the self device."""

    xuid: str
    gamertag: str
    display_name: str
    state: str                    # peoplehub presenceState: Online / Offline / Away
    primary_title_text: str       # PresenceText of the primary game detail
    primary_title_id: str
    is_game: bool                 # currently in a game (primary detail IsGame)
    device: str                   # console/device of the primary detail
    presence_text: str            # raw person-level presenceText
    rich_presence_text: str = ""  # primary detail RichPresenceText (falls back to PresenceText)
    is_broadcasting: bool = False
    in_multiplayer: bool = False  # multiplayerSummary.InMultiplayerSession > 0
    gamer_score: int = 0
    account_tier: str = ""        # detail.accountTier (e.g. Gold / Silver)
    gamer_pic_url: str = ""       # displayPicRaw / profile GameDisplaypicRaw

    @property
    def online(self):
        return self.state == xc.PRESENCE_ONLINE

    @property
    def in_game(self):
        """True when the person is online AND in a game."""
        return self.online and self.is_game


def _select_primary_detail(details):
    """Choose the most relevant presence detail.

    Prefer a detail that is both primary and a game; then any game detail; then
    the primary detail; then the first. ``details`` may be ``None``/empty.
    """
    if not details:
        return None
    games = [d for d in details if isinstance(d, dict) and d.get("IsGame")]
    for detail in games:
        if detail.get("IsPrimary"):
            return detail
    if games:
        return games[0]
    for detail in details:
        if isinstance(detail, dict) and detail.get("IsPrimary"):
            return detail
    first = details[0]
    return first if isinstance(first, dict) else None


def parse_person(person):
    """Parse one peoplehub ``person`` dict into a :class:`PersonPresence`.

    The person object is camelCase (``xuid``, ``gamertag``, ``gamerScore``,
    ``displayPicRaw``, ``isBroadcasting``) but its nested decoration objects are
    PascalCase: ``presenceDetails[]`` (``IsGame``/``PresenceText``/
    ``RichPresenceText``/``IsBroadcasting`` …) and ``multiplayerSummary``
    (``InMultiplayerSession``/``InParty``). The ``detail`` decoration is itself
    camelCase (``accountTier``)."""
    detail = _select_primary_detail(person.get("presenceDetails"))
    detail = detail or {}
    is_game = bool(detail.get("IsGame"))
    multiplayer = person.get("multiplayerSummary") or {}
    profile_detail = person.get("detail") or {}
    return PersonPresence(
        xuid=str(person.get("xuid", "")),
        gamertag=person.get("gamertag") or "",
        display_name=person.get("displayName") or "",
        state=person.get("presenceState") or xc.PRESENCE_OFFLINE,
        primary_title_text=detail.get("PresenceText") or "",
        primary_title_id=str(detail.get("TitleId") or ""),
        is_game=is_game,
        device=detail.get("Device") or "",
        presence_text=person.get("presenceText") or "",
        rich_presence_text=(detail.get("RichPresenceText")
                            or detail.get("PresenceText") or ""),
        is_broadcasting=bool(detail.get("IsBroadcasting")
                             or person.get("isBroadcasting")),
        in_multiplayer=_coerce_int(multiplayer.get("InMultiplayerSession")) > 0,
        gamer_score=_coerce_int(person.get("gamerScore")),
        account_tier=profile_detail.get("accountTier") or "",
        gamer_pic_url=person.get("displayPicRaw") or "",
    )


def _coerce_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def parse_people(payload):
    """Parse a peoplehub presencedetail response into a list of presences.

    A missing/malformed ``people`` array yields an empty list rather than raising
    — a supervisor tick must never crash on an unexpected shape."""
    people = (payload or {}).get("people")
    if not isinstance(people, list):
        return []
    return [parse_person(p) for p in people if isinstance(p, dict)]


def _select_active_title(devices):
    """Pick the "in a game" title across all of a userpresence response's
    ``devices``: an ``Active`` title that is not the Xbox dashboard, preferring
    one with ``placement`` ``"Full"``. Returns ``(title, device_type)``, or
    ``(None, None)`` when nothing qualifies. ``devices`` may be ``None``/empty."""
    candidates = []
    for device in devices or []:
        if not isinstance(device, dict):
            continue
        device_type = device.get("type") or ""
        for title in device.get("titles") or []:
            if not isinstance(title, dict) or title.get("state") != "Active":
                continue
            if str(title.get("id") or "") == xc.DASHBOARD_TITLE_ID:
                continue
            candidates.append((title, device_type))
    if not candidates:
        return None, None
    for title, device_type in candidates:
        if title.get("placement") == "Full":
            return title, device_type
    return candidates[0]


def _online_device_type(devices):
    """Device type to report when online but not in a game (dashboard only, or
    no active title at all): whichever device has an Active title, else the
    first device present, else ``""``."""
    devices = devices or []
    for device in devices:
        if not isinstance(device, dict):
            continue
        for title in device.get("titles") or []:
            if isinstance(title, dict) and title.get("state") == "Active":
                return device.get("type") or ""
    first = devices[0] if devices else None
    return (first.get("type") or "") if isinstance(first, dict) else ""


def parse_own_presence(payload, own_xuid="", own_gamertag=""):
    """Parse a userpresence ``/users/me`` response into a :class:`PersonPresence`.

    Unlike peoplehub this is a single item, camelCase, and shapes ``devices``
    differently: ``devices[].titles[]`` (fields ``id``/``name``/``placement``/
    ``state``) rather than a flat ``presenceDetails`` list. The Xbox dashboard
    appears as its own Active title (:data:`xb_constants.DASHBOARD_TITLE_ID`) —
    that alone means "online", not "in a game"; only a non-dashboard Active
    title (preferring ``placement`` ``"Full"``) counts. When offline, the
    optional ``lastSeen`` (``deviceType``/``titleId``/``titleName``) is used for
    display only — it never makes ``is_game`` true. ``own_xuid``/``own_gamertag``
    are used only as a fallback when the payload omits them (it always echoes
    ``xuid``; it never includes a gamertag).
    """
    payload = payload or {}
    state = payload.get("state") or xc.PRESENCE_OFFLINE
    online = state == xc.PRESENCE_ONLINE
    title, device_type = _select_active_title(payload.get("devices")) if online else (None, None)
    rich = ""
    if title is not None:
        title_text = title.get("name") or ""
        title_id = str(title.get("id") or "")
        device = device_type or ""
        rich = _active_rich_presence(title) or title_text
    elif online:
        title_text = ""
        title_id = ""
        device = _online_device_type(payload.get("devices"))
    else:
        last_seen = payload.get("lastSeen") or {}
        title_text = last_seen.get("titleName") or ""
        title_id = str(last_seen.get("titleId") or "")
        device = last_seen.get("deviceType") or ""
    return PersonPresence(
        xuid=str(payload.get("xuid") or own_xuid or ""),
        gamertag=own_gamertag or "",
        display_name=own_gamertag or "",
        state=state,
        primary_title_text=title_text,
        primary_title_id=title_id,
        is_game=title is not None,
        device=device,
        presence_text="",
        rich_presence_text=rich,
    )


def _active_rich_presence(title):
    """Pull rich-presence text from a userpresence title's ``activity`` list
    (``activity[0].richPresence``); ``""`` when absent. ``title`` is a dict."""
    for activity in title.get("activity") or []:
        if isinstance(activity, dict) and activity.get("richPresence"):
            return activity["richPresence"]
    return ""


def apply_profile(presence, profile):
    """Overlay a :class:`ProfileInfo` onto a presence (the self device's extras
    come from the profile service, not userpresence). Returns a new presence;
    empty profile fields never clobber existing values."""
    if profile is None:
        return presence
    return replace(
        presence,
        gamer_score=profile.gamer_score or presence.gamer_score,
        account_tier=profile.account_tier or presence.account_tier,
        gamer_pic_url=profile.gamer_pic_url or presence.gamer_pic_url,
        display_name=profile.display_name or presence.display_name,
    )


def fetch_own_presence(api, auth, own_xuid="", own_gamertag="", logger=None):
    """Fetch + parse the signed-in account's own presence.

    Peoplehub never lists the caller, so tracking your own gamertag needs this
    separate userpresence call. Returns ``None`` when not authorized (no XBL
    header available); retries once through a fresh token chain on a 401, same
    as :func:`fetch_presence`. Raises :class:`XboxError` on any other failure.
    """
    logger = logger or logging.getLogger("xb_presence")
    header = auth.xbl_header()
    if not header:
        return None
    try:
        payload = _get_own_presence(api, header)
    except XboxError as exc:
        if exc.status != 401:
            raise
        logger.debug("Xbox own presence 401 — re-deriving token chain and retrying once")
        auth.invalidate_xbl()
        header = auth.xbl_header()
        if not header:
            return None
        payload = _get_own_presence(api, header)
    return parse_own_presence(payload, own_xuid=own_xuid, own_gamertag=own_gamertag)


def _get_own_presence(api, header):
    headers = {
        "Authorization": header,
        "x-xbl-contract-version": xc.OWN_PRESENCE_CONTRACT_VERSION,
    }
    return api.get_json(xc.OWN_PRESENCE_URL, headers=headers)


def fetch_presence(api, auth, logger=None):
    """Fetch + parse the caller's social presence list.

    Returns a list of :class:`PersonPresence`, or ``None`` when not authorized
    (no XBL header available). Retries once through a fresh token chain on a 401
    (the cached XSTS token can expire between derivation and use). Raises
    :class:`XboxError` on any other API failure so the supervisor logs it.
    """
    logger = logger or logging.getLogger("xb_presence")
    header = auth.xbl_header()
    if not header:
        return None
    try:
        payload = _get_presence(api, header)
    except XboxError as exc:
        if exc.status != 401:
            raise
        logger.debug("Xbox presence 401 — re-deriving token chain and retrying once")
        auth.invalidate_xbl()
        header = auth.xbl_header()
        if not header:
            return None
        payload = _get_presence(api, header)
    return parse_people(payload)


def _get_presence(api, header):
    headers = {
        "Authorization": header,
        "x-xbl-contract-version": xc.PEOPLE_CONTRACT_VERSION,
        "Accept-Language": xc.ACCEPT_LANGUAGE,
    }
    return api.get_json(xc.PRESENCE_URL, headers=headers)


# -- Profile extras (self device) ---------------------------------------------

@dataclass
class ProfileInfo:
    """Gamerscore / tier / gamerpic / display name from the profile service."""

    gamer_score: int = 0
    account_tier: str = ""
    gamer_pic_url: str = ""
    display_name: str = ""


def parse_profile(payload):
    """Parse a profile-settings response into a :class:`ProfileInfo`, or ``None``
    when the shape is unusable. The response echoes each requested setting id
    back as ``{"id": ..., "value": ...}``; read case-insensitively so a
    ``GameDisplayPicRaw``/``GameDisplaypicRaw`` casing difference is tolerated."""
    users = (payload or {}).get("profileUsers")
    if not isinstance(users, list) or not users:
        return None
    first = users[0] if isinstance(users[0], dict) else {}
    settings = first.get("settings")
    if not isinstance(settings, list):
        return None
    values = {}
    for setting in settings:
        if isinstance(setting, dict) and setting.get("id"):
            values[str(setting["id"]).lower()] = setting.get("value")
    return ProfileInfo(
        gamer_score=_coerce_int(values.get("gamerscore")),
        account_tier=values.get("accounttier") or "",
        gamer_pic_url=values.get("gamedisplaypicraw") or "",
        display_name=values.get("gamedisplayname") or "",
    )


def fetch_profile(api, auth, xuid, logger=None):
    """Fetch + parse profile extras for ``xuid`` (used for the self device).

    Returns ``None`` when not authorized. Same one-shot 401 retry as the other
    fetchers; raises :class:`XboxError` on any other failure so the caller can
    decide (the plugin logs it at debug and leaves the states empty)."""
    logger = logger or logging.getLogger("xb_presence")
    header = auth.xbl_header()
    if not header:
        return None
    try:
        payload = _get_profile(api, header, xuid)
    except XboxError as exc:
        if exc.status != 401:
            raise
        logger.debug("Xbox profile 401 — re-deriving token chain and retrying once")
        auth.invalidate_xbl()
        header = auth.xbl_header()
        if not header:
            return None
        payload = _get_profile(api, header, xuid)
    return parse_profile(payload)


def _get_profile(api, header, xuid):
    headers = {
        "Authorization": header,
        "x-xbl-contract-version": xc.PROFILE_CONTRACT_VERSION,
    }
    return api.get_json(xc.PROFILE_URL.format(xuid=xuid), headers=headers)


# -- Title box art (titlehub) --------------------------------------------------

def parse_title_image(payload):
    """Return a box-art URL from a titlehub titleinfo response, or ``""``.

    Prefers the title's ``displayImage``; falls back to the best entry in the
    ``images`` list by :data:`xb_constants.TITLE_IMAGE_TYPES` priority."""
    titles = (payload or {}).get("titles")
    if not isinstance(titles, list) or not titles:
        return ""
    title = titles[0] if isinstance(titles[0], dict) else {}
    display = title.get("displayImage")
    if display:
        return display
    images = title.get("images") or []
    by_type = {}
    for image in images:
        if isinstance(image, dict) and image.get("url"):
            by_type.setdefault(image.get("type"), image["url"])
    for wanted in xc.TITLE_IMAGE_TYPES:
        if wanted in by_type:
            return by_type[wanted]
    return next(iter(by_type.values()), "")


def fetch_title_image(api, auth, caller_xuid, title_id, logger=None):
    """Fetch + parse a title's box-art URL. The titlehub path is scoped by the
    caller's own xuid (the metadata itself is global). Returns ``None`` when not
    authorized; same one-shot 401 retry; raises :class:`XboxError` otherwise."""
    logger = logger or logging.getLogger("xb_presence")
    header = auth.xbl_header()
    if not header:
        return None
    try:
        payload = _get_title_info(api, header, caller_xuid, title_id)
    except XboxError as exc:
        if exc.status != 401:
            raise
        logger.debug("Xbox titlehub 401 — re-deriving token chain and retrying once")
        auth.invalidate_xbl()
        header = auth.xbl_header()
        if not header:
            return None
        payload = _get_title_info(api, header, caller_xuid, title_id)
    return parse_title_image(payload)


def _get_title_info(api, header, caller_xuid, title_id):
    headers = {
        "Authorization": header,
        "x-xbl-contract-version": xc.TITLEHUB_CONTRACT_VERSION,
        "x-xbl-client-name": xc.TITLEHUB_CLIENT_NAME,
        "x-xbl-client-type": xc.TITLEHUB_CLIENT_TYPE,
        "x-xbl-client-version": xc.TITLEHUB_CLIENT_VERSION,
        "Accept-Language": xc.ACCEPT_LANGUAGE,
    }
    url = xc.TITLE_INFO_URL.format(xuid=caller_xuid, title_id=title_id)
    return api.get_json(url, headers=headers)
