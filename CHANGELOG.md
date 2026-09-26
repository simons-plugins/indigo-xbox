# Changelog

All notable changes to the Xbox plugin are documented here.

## 2026.2.0 — Xbox Console device

### Added
- `xboxConsole` sensor device: reports whether an Xbox **console** (not a
  gamertag) is powered on, via the SmartGlass console-management API (xccs).
  Uses the existing XBL3.0 auth chain — no separate sign-in. States:
  `powerState`, `consoleName`, `consoleType`, `focusedTitleName`,
  `focusedTitleId`, `lastPoll`, `lastPowerChange`; `onOffState` is true only
  when `powerState` is exactly `"On"` — a controller power-off (Sleep/Standby
  mode) reports `ConnectedStandby`, which reads as **off**, not just "not on".
- **Power On** / **Power Off** actions (`deviceFilter="self.xboxConsole"`)
  send a SmartGlass `WakeUp`/`TurnOff` command to the device's console. Power
  On requires the console's power mode to be Sleep/Standby (Full shutdown
  cannot be woken remotely). A successful command pulls the next console poll
  ~10 s forward so the state catches up without polling on every action.
- Status Request triggers an immediate console poll for `xboxConsole`
  devices (`actionControlUniversal` / `kUniversalAction.RequestStatus`).
- New `consolePollInterval` preference (default 60, 15–600 s, same
  validation as `pollInterval`) — console devices are polled on their own
  schedule, independent of presence, and are skipped entirely (no API call)
  while no console device is configured.
- Focused-title resolution: while a console is On, one `/consoles/{id}` call
  reads `focusAppAumid`, resolved to a title name/id via a per-console,
  per-aumid cached `installedApps` lookup (a miss is cached too, so an
  unmatched aumid never re-fetches every poll).
- Degradation paths: a console-list failure (transport, 401-after-retry,
  429, 5xx, bad JSON, or a list-level `errorCode`) sets one error per
  console device and backs off (429 honours `retry_after`; otherwise
  exponential from 60 s to a 900 s cap, reset on recovery) — last known
  states are always left untouched, never reset to Off. A console missing
  from a successful list, or reporting `Unknown`/a missing `powerState`, is
  reported as "unavailable"/"console not found", not Off. A `/consoles/{id}`
  or `installedApps` failure never breaks the power-state update — only the
  focused-title states are left as they were.

## 2026.1.0 — Usage charts

### Added
- Play-history charts page for parents, viewable in a browser and served by the
  Indigo Web Server at `/com.simons-plugins.indigo-xbox/static/pages/xbox-charts.html`
  (behind IWS auth; reachable on the LAN or through the Reflector). It is a
  single self-contained page — inline CSS/JS and hand-rolled SVG, zero external
  requests — with a per-child "today" card (gamerpic, live status, minutes,
  current box art), a 14-day grouped daily-minutes bar chart, a 30-day
  per-game breakdown, and a 28-day hour-of-day × day-of-week heatmap. It
  auto-refreshes every 60s (paused while the tab is hidden) and renders in both
  light and dark themes.
- The page now lives at `static/pages/` (not `static/charts/`) and carries
  `indigo-page-*` meta tags, so it is Domio-compatible: copy it to
  `{Indigo install}/Web Assets/static/pages/` and it appears in Domio's HTML
  pages list immediately (no restart) — see the README's "Charts in Domio"
  section. It resolves its data endpoint via an absolute same-origin path so
  it works unchanged from either location, and authenticates with
  `window.INDIGO_CONFIG` (Bearer apiKey) when Domio injects it.
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
