"""Endpoints, scopes, and state constants for the Xbox plugin.

Pure data — never imports ``indigo`` and has no side effects. Shared by
``xb_auth``, ``xb_api`` and ``xb_presence`` so the exact strings the Xbox / MSA
REST contract requires live in exactly one place.
"""

# -- OAuth (Microsoft account, consumers tenant) ------------------------------
# The scope must be EXACTLY this (nothing else) — extra scopes make the MSA
# device-code endpoint 400. offline_access buys the refresh token.
SCOPE = "XboxLive.signin offline_access"
DEVICE_CODE_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/devicecode"
TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"

# -- Xbox Live token chain ----------------------------------------------------
USER_AUTH_URL = "https://user.auth.xboxlive.com/user/authenticate"
XSTS_URL = "https://xsts.auth.xboxlive.com/xsts/authorize"
USER_RELYING_PARTY = "http://auth.xboxlive.com"
XSTS_RELYING_PARTY = "http://xboxlive.com"
SANDBOX_ID = "RETAIL"
XBL_CONTRACT_VERSION = "1"

# -- Peoplehub (social presence) ----------------------------------------------
# Decorations requested in one call (order is irrelevant to the response):
#   detail            → accountTier, gamerpic etc. (camelCase ``detail`` object)
#   multiplayersummary→ InMultiplayerSession / InParty (PascalCase object)
#   presencedetail    → the PascalCase ``presenceDetails`` list already parsed
# Valid decoration names verified against the reference PeopleProvider
# (get_friends_own default set: preferredcolor,detail,multiplayersummary,
# presencedetail) — we drop preferredcolor as unused.
PRESENCE_URL = ("https://peoplehub.xboxlive.com/users/me/people/social/"
                "decoration/detail,multiplayersummary,presencedetail")
PEOPLE_CONTRACT_VERSION = "3"
ACCEPT_LANGUAGE = "en-GB"

# -- Userpresence (own account) ------------------------------------------------
# Peoplehub's social graph never includes the caller's own account, so the
# signed-in account's presence has to come from this separate endpoint.
OWN_PRESENCE_URL = "https://userpresence.xboxlive.com/users/me?level=all"
OWN_PRESENCE_CONTRACT_VERSION = "3"

# -- Profile (own account extras: gamerscore / tier / gamerpic / name) --------
# Peoplehub carries these for social people, but never for the caller's own
# account, so the self device gets them from the profile settings service.
# NOTE: the reference set did not include the xbox-webapi profile provider, so
# these mirror that library's canonical ProfileProvider: contract version "3"
# (same as peoplehub) and the profile settings service. The response echoes
# each requested setting id back verbatim; :func:`xb_presence.parse_profile`
# reads them case-insensitively so a "GameDisplayPicRaw"/"GameDisplaypicRaw"
# casing difference on the wire is tolerated.
PROFILE_SETTINGS = "Gamerscore,AccountTier,GameDisplaypicRaw,GameDisplayName"
PROFILE_URL = ("https://profile.xboxlive.com/users/xuid({xuid})/profile/"
               "settings?settings=" + PROFILE_SETTINGS)
PROFILE_CONTRACT_VERSION = "3"

# -- Titlehub (box art for a title id) ----------------------------------------
# Headers + path verified against the reference TitlehubProvider
# (get_title_info → /users/xuid({xuid})/titles/titleid({id})/decoration/{fields}
# with x-xbl-contract-version 2 and the XboxApp/UWA client headers). The title
# is user-scoped by the caller's own xuid but the metadata (displayImage) is
# global. ``image,detail`` is the minimal decoration for box art.
TITLE_INFO_URL = ("https://titlehub.xboxlive.com/users/xuid({xuid})/titles/"
                  "titleid({title_id})/decoration/image,detail")
TITLEHUB_CONTRACT_VERSION = "2"
TITLEHUB_CLIENT_NAME = "XboxApp"
TITLEHUB_CLIENT_TYPE = "UWA"
TITLEHUB_CLIENT_VERSION = "39.39.22001.0"
# Preferred image ``type`` order when picking from a title's ``images`` list
# (fallback when ``displayImage`` is absent).
TITLE_IMAGE_TYPES = ("BoxArt", "Poster", "Tile", "Logo")

# The Xbox dashboard shows up as its own Active "title" on a console the moment
# it's powered on, even when no game is running — it must never be read as
# "in a game".
DASHBOARD_TITLE_ID = "750323071"

# -- Device-flow defaults (overridden by the server response when present) -----
DEFAULT_DEVICE_INTERVAL = 5
DEFAULT_DEVICE_EXPIRES = 900
SLOW_DOWN_STEP = 5

# -- MSA access-token refresh policy ------------------------------------------
# The access token lives ~1 h, so a 1 h window (Home Connect's value) would
# refresh on every tick — use a small window and refresh only when <10 min
# remain. Same min-interval + exponential-backoff guard rails as hc_auth.
REFRESH_WINDOW = 10 * 60
MIN_REFRESH_INTERVAL = 6
REFRESH_BACKOFF_BASE = 60
REFRESH_BACKOFF_MAX = 60 * 60

# XSTS tokens carry their own NotAfter; re-derive this many seconds before it.
XSTS_MARGIN = 5 * 60

# -- Auth states surfaced to the plugin / status line -------------------------
STATE_UNAUTHORIZED = "unauthorized"
STATE_PENDING = "pending"
STATE_AUTHORIZED = "authorized"
STATE_AUTH_REQUIRED = "authorization_required"   # revoked / child / banned

# -- Presence states (peoplehub ``presenceState``) ----------------------------
PRESENCE_ONLINE = "Online"
PRESENCE_OFFLINE = "Offline"
PRESENCE_AWAY = "Away"

# -- XSTS XErr codes (HTTP 401 body) → actionable messages --------------------
# Parsed from the XSTS response body so the log tells the user what is wrong
# instead of a bare "HTTP 401". Codes 2148916238 (child) and 2148916227
# (banned) are terminal — no amount of re-authorizing fixes them.
XERR_MESSAGES = {
    2148916233: ("this Microsoft account has no Xbox profile — sign in at xbox.com once "
                 "to create a gamertag, then authorize again"),
    2148916238: ("this is a child account — Xbox Live sign-in must use an ADULT account "
                 "(the child must be a friend/follower of it instead)"),
    2148916227: "this account is banned from Xbox Live",
    2148916235: "Xbox Live is not available in this account's region",
}
XERR_TERMINAL = frozenset({2148916238, 2148916227})
