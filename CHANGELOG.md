# Changelog

All notable changes to the Xbox plugin are documented here.

## 2026.1.0 — Usage charts

### Added
- Play-history charts page for parents, viewable in a browser and served by the
  Indigo Web Server at `/com.simons-plugins.indigo-xbox/static/charts/index.html`
  (behind IWS auth; reachable on the LAN or through the Reflector). It is a
  single self-contained page — inline CSS/JS and hand-rolled SVG, zero external
  requests — with a per-child "today" card (gamerpic, live status, minutes,
  current box art), a 14-day grouped daily-minutes bar chart, a 30-day
  per-game breakdown, and a 28-day hour-of-day × day-of-week heatmap. It
  auto-refreshes every 60s (paused while the tab is hidden) and renders in both
  light and dark themes.
- Play-segment history (`xb_history.py`): a stdlib `sqlite3` store recording one
  segment per title (a title switch closes the current segment and opens a new
  one — finer-grained than the session accountant, which is unchanged).
  Midnight-spanning segments are split into per-day rows. Open (in-progress)
  segments live in memory only, so a plugin restart loses at most the current
  segment's tail; `todayMinutes` continuity is unaffected (still handled by the
  session sidecar). Old rows are pruned (default 365 days) once per startup.
- JSON endpoint `chart_data` (IWS hidden action) returning each tracked
  person's live device states plus the last 30 days of play segments. No
  secrets are included in the response.

## 2026.0.4 — State-list refresh on device start

### Fixed
- Devices created under an older Devices.xml revision ignored newly added
  state keys ("state key X not defined" in the Event Log). `deviceStartComm`
  now calls `stateListOrDisplayStateIdChanged()` so existing devices pick up
  new states after a plugin upgrade without an open-and-save.

## 2026.0.3 — Rich states

### Added
- Session stats computed per device: `sessionStartedAt`, `sessionMinutes`
  (live), `lastSessionMinutes`, `todayMinutes` — with midnight splitting and
  restart persistence via an atomic sidecar file (`xb_sessions.py`).
- Rich presence: `richPresenceText`, `isBroadcasting`, `inMultiplayer`
  (peoplehub decorations expanded — still a single call).
- Profile extras: `gamerScore`, `accountTier`, `gamerPicUrl`, `displayName`
  (peoplehub detail decoration; profile service for the self device, cached
  and refreshed every 10th poll).
- Title box art: `titleImageUrl` via titlehub, cached per titleId; failures
  degrade to an empty state without breaking presence sync.

## 2026.0.2 — Self-tracking

Peoplehub's social graph never includes the signed-in account itself, so
there was no way to create a presence device for your own gamertag.

### Added
- `xb_auth`: the XSTS `DisplayClaims.xui[0]` already carries the signed-in
  account's own `xid`/`gtg` — exposed via `XboxAuth.own_identity()` (derives
  the token chain on demand, `None` when unauthorized/unavailable). Nothing
  new is persisted.
- `xb_presence`: `fetch_own_presence()` + a pure `parse_own_presence()` against
  `GET https://userpresence.xboxlive.com/users/me?level=all`, producing the
  same `PersonPresence` dataclass as peoplehub parsing. Handles in-game
  (an Active, non-dashboard title, preferring `placement: "Full"`),
  dashboard-only-online (title id `750323071`, "online" but not "in a game"),
  and offline with an optional `lastSeen`. Same one-shot 401 retry as
  `fetch_presence`.
- `plugin.py`: the device person-picker now prepends "Me — &lt;gamertag&gt;"
  when authorized; each poll fetches the account's own presence exactly once
  when a tracked device's xuid is the account's own (peoplehub still covers
  everyone else); "Log Tracked People" logs the signed-in account first,
  marked "(me)".

## 2026.0.1 — Milestone 1

Initial release. Presence tracking foundation only (no HTML charts yet).

### Added
- MSA OAuth **Device Flow** authorization (`xb_auth`): request a device code,
  display the verification URL + user code, poll to completion, persist tokens
  atomically (0600, keyed by client id).
- Xbox Live token chain: MSA access token → user token → XSTS token, cached
  against each token's `NotAfter` and re-derived on expiry or a 401. XSTS
  `XErr` codes (no profile / child account / banned / region) mapped to clear
  log messages.
- Proactive MSA access-token refresh with min-interval + exponential backoff.
- Peoplehub presence polling (`xb_presence`) parsed into a `PersonPresence`
  dataclass (handles PascalCase `presenceDetails`, null/empty details).
- `xboxPresence` sensor device (one per gamertag): on/off = currently in a game,
  plus `online`, `presenceState`, `titleName`, `titleId`, `device`, `lastSeen`,
  `lastPoll` states. States are written only on change so SQL Logger records
  real play-session transitions rather than every poll.
- Config UI: Application (client) ID, poll interval (15–600 s), Authorize
  button. Device config: people picker (dynamic list) + manual xuid fallback.
- "Log Tracked People" menu action for discovering xuids / verifying visibility.
- pytest suite covering auth, presence parsing, device sync and XML validity.
