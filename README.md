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

## Development

- Python 3.10+, **stdlib only** (no `requirements.txt`). Only `plugin.py`
  imports `indigo`; the `xb_*` modules are pure and unit-tested.
- Run the tests: `python3 -m pytest -q`.

See the [Indigo workspace](../CLAUDE.md) for shared standards.
