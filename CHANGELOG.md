# Changelog

All notable changes to the Xbox plugin are documented here.

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
