# Xbox (Indigo plugin)

Tracks the **Xbox Live presence** of family members as Indigo devices — one
device per gamertag — so Indigo's SQL Logger accumulates play-session history
(who is online, what game they are in, and when). This is Milestone 1: auth,
presence polling, and devices. HTML charts come later.

Each gamertag becomes a **sensor** device whose on/off state means "currently in
a game", with extra states for the presence state, current title, device, and
timestamps. Because states are written only when they change, SQL Logger records
real transitions (session start/stop, title changes) instead of one row per poll.

## How it works

- **Auth** — Microsoft account (MSA) OAuth **Device Flow**: the plugin shows a
  verification URL and a short code; you sign in once in a browser with an
  **adult** Microsoft account. Tokens are stored locally (never in prefs).
- **Xbox Live token chain** — the MSA access token is exchanged for an Xbox user
  token and then an XSTS token; these are cached and re-derived automatically.
- **Presence** — every *poll interval* the plugin reads your social presence
  list from peoplehub and updates each tracked device.

## Azure app registration (one time)

You need your own Azure "Application (client) ID". It is free and takes a minute.

1. Go to <https://portal.azure.com> → **App registrations** → **New registration**.
2. Name it anything (e.g. "Indigo Xbox").
3. Supported account types: **Personal Microsoft accounts only**.
4. **Leave the Redirect URI blank.** Click **Register**.
5. Open the app's **Authentication** blade → under **Advanced settings** set
   **Allow public client flows** = **Yes** → **Save**.
6. Copy the **Application (client) ID** from the app's Overview page.

> If the Xbox user-token step returns HTTP 400 from `user.auth.xboxlive.com`,
> the app registration is almost always the cause — see the community issue
> [OpenXbox/xbox-webapi-python#6](https://github.com/OpenXbox/xbox-webapi-python/issues/6).
> Checklist: the app must be **personal-accounts / consumers tenant**, the scope
> must be exactly `XboxLive.signin offline_access`, and **public client flows**
> must be enabled.

## Plugin configuration

1. Paste the **Application (client) ID** into the plugin config.
2. Set a **poll interval** (15–600 seconds; 60 is a good default).
3. Click **Authorize**. Open the verification URL shown (also written to the
   Event Log), enter the user code, and sign in with the **adult** account.
4. Add a device per family member: pick them from the **Person** menu, or paste
   an **xuid** manually if they are not listed. Use **Plugins → Xbox → Log
   Tracked People** to print everyone currently visible (with xuids).

## Child-account requirements (important)

Xbox Live sign-in here **must** use an **adult** account — child accounts cannot
authenticate (they return XSTS `XErr 2148916238`). To track a child's presence:

- Sign in with the **adult** account.
- The child (or any tracked person) must be a **friend/follower** of that adult
  account, **and** their privacy setting **"Others can see if you're online"**
  must allow it. Being in the same **family group is not enough** on its own —
  presence visibility is governed by the friend/follow relationship and that
  privacy toggle.

If a person is not visible you will see "gamertag not visible on this account" on
their device; fix the follow relationship / privacy setting and it clears on the
next poll.

## Xbox Console device

Alongside the gamertag ("Xbox Gamertag") device, the plugin offers an
**"Xbox Console"** device that reports whether the console **itself** is
powered on — independent of who (if anyone) is signed in and playing.

- **On the console**, go to **Settings → Devices & connections → Remote
  features** and turn on **"Enable remote features"**. Without this, the
  console won't appear in the device's **Console** picker.
- States: `powerState` (raw value: `On` / `ConnectedStandby` / `Off` /
  `SystemUpdate`), `consoleName`, `consoleType`, `focusedTitleName` /
  `focusedTitleId` (the game/app in focus, while On), `lastPoll`,
  `lastPowerChange`.
- **`onOffState` is true only when `powerState` is exactly `On`.** Turning
  the console off with the controller (its normal Sleep/Standby power mode)
  reports `ConnectedStandby`, not `Off` — this still reads as **off**
  (`onOffState` false), so triggers built on "console turned off" work as
  expected either way.
- **Poll interval** — a separate **Console poll interval** preference
  (15–600 s, default 60) controls how often console devices are checked.
  Presence keeps its own interval and cadence; no console devices configured
  means no extra API calls at all.
- **Power On / Power Off actions** — send a remote power command to the
  console. **Power On only works when the console's power mode is set to
  Sleep (sometimes labelled "Standby")** — a console that was fully shut
  down (Energy saving) cannot be woken remotely. After either action, the
  device's state catches up on the next console poll (pulled a few seconds
  sooner automatically).
- **Example trigger**: *Device State Changed* → the Xbox Console device →
  `onOffState` → *becomes false* — fires whenever the console goes to sleep
  or is turned off, whichever way it happened.

## Usage charts

The plugin ships a self-contained charts page (today's play per gamertag, daily
minutes over 14 days, per-game breakdown, weekly heatmap). It is served by the
Indigo Web Server straight from the plugin bundle at:

```
http(s)://<your-indigo-server>:8176/com.simons-plugins.indigo-xbox/static/pages/xbox-charts.html
```

(also reachable through your Reflector URL, behind your normal Indigo login).
The plugin logs this URL on startup.

### Showing the charts in Domio

The [Domio](https://domio-smart-home.app) iOS app lists pages it finds in
`{Indigo install}/Web Assets/static/pages/` — it does not scan inside plugin
bundles. Copy the page there once (run **on the Indigo server Mac**):

```bash
cp "/Library/Application Support/Perceptive Automation/Indigo 2025.2/Plugins/Xbox.indigoPlugin/Contents/Resources/static/pages/xbox-charts.html" \
   "/Library/Application Support/Perceptive Automation/Indigo 2025.2/Web Assets/static/pages/"
```

The page then appears in Domio's pages list as **"Xbox Usage"** (game-controller
icon) immediately — no plugin or server restart needed. Re-copy the file after
each plugin update to pick up page improvements. The exact source and
destination paths for your installation are printed in the Event Log each time
the plugin starts.

## Development

- Python 3.10+, **stdlib only** (no `requirements.txt`). Only `plugin.py`
  imports `indigo`; the `xb_*` modules are pure and unit-tested.
- Run the tests: `python3 -m pytest -q`.

See the [Indigo workspace](../CLAUDE.md) for shared standards.
