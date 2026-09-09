# Volume Presets

A local volume preset system for the speakers in one room, controlled from a
modded **Spotify Car Thing**.

The Car Thing runs a custom webapp that talks to a Python **Flask** server on a
**Raspberry Pi 2**. The server sends absolute volume commands straight to each
speaker over the LAN with
[`pychromecast`](https://github.com/home-assistant-libs/pychromecast), opening
one persistent connection per speaker **by static IP** — no zeroconf/mDNS
discovery, so the footprint stays small enough to run 24/7 on a Pi 2 (1 GB RAM,
ARMv7).

**Networking:** the Car Thing has no WiFi of its own — it reaches the Pi only
over USB. `adb reverse tcp:5005 tcp:5005` tunnels the device's `localhost:5005`
to the Pi. Because the device also has no internet, the Pi **proxies** every
Spotify call (now-playing, album art, playlists); no Spotify tokens ever touch
the device.

```
[Car Thing] ──USB (adb reverse :5005)──> [Raspberry Pi Flask server] ──Cast──> speakers
  webapp @ localhost:5005                           │                  (static IP, :8009)
                                                    └── proxies ──> [Spotify Web API]
```

> **Keep Spotify signed out of the TV.** If a TV among your Cast devices is
> signed in to your Spotify account it registers as a Spotify Connect device, and
> from then on *any* write to its Cast volume is reported to Spotify and applied
> to whichever session is currently active. Two things follow, both measured on
> this setup:
>
> - While the TV is a member of a **casting Cast group**, that closes a loop —
>   the write moves the session, the session rescales every group member
>   including the TV, and it reports again. A single volume write took the room
>   from 4% to 100% in about 15 seconds with no further input.
> - While Spotify is **playing on another device**, the write moves *that*
>   device's volume instead — turning the dial here changed a laptop's volume in
>   another room.
>
> Signing Spotify out of the TV removes it from `/me/player/devices` and cuts the
> reporting path, which is why `server.py` can write every speaker directly with
> no special-casing. If you ever sign back in, both behaviours return and will
> look like an unexplained regression. The cost of staying signed out is that the
> TV is no longer a Spotify Connect target, so playlists can't be started in the
> room from cold — see **Playlist targets** under Endpoints.

---

## Configuration & secrets

All config lives in **`presets.json`**, which holds secrets (Spotify client
secret + refresh token) and is therefore **git-ignored**. The repo ships
**`presets.example.json`** as a template — copy it to `presets.json` and fill in
your own values (see Setup). Never commit `presets.json`.

---

## Setup

### Step 1 — Give your speakers static IPs

Add a **DHCP reservation** on your router for each speaker's MAC so its IP never
changes. The server connects to these IPs directly, so a stable address per
speaker is **required** — there is no discovery to fall back on. Note each IP
(e.g. `10.0.0.50`, `10.0.0.51`, `10.0.0.52`).

### Step 2 — Install dependencies

```bash
sudo apt update
sudo apt install -y python3-venv adb
```

Then create the virtualenv and install the Python packages:

```bash
cd ~/SmartRoomThing                 # adjust if you cloned elsewhere
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install flask pychromecast
```

> On a Pi 2 the `pychromecast` install pulls in `zeroconf`/`protobuf` and can
> take a few minutes (some wheels build from source). It only uses them for the
> direct host connection — discovery is never run.

### Step 3 — Create `presets.json`

```bash
cp presets.example.json presets.json
```

Set each speaker's `ip_address` to the static IP from Step 1, then tune the
per-speaker preset volumes (floats `0.0`–`1.0`). Fill in the Spotify fields in
Step 4.

### Step 4 — Test the server manually

```bash
./venv/bin/python3 server.py
```

The startup banner lists each speaker with `[connected]` or `[OFFLINE]`. An
offline speaker is logged as a warning and skipped — the server never crashes or
blocks on an unreachable one. Verify from another machine on the LAN:

```bash
curl -X POST http://<pi-ip>:5005/preset/1     # speakers should jump to the DESK mix
```

Stop with Ctrl-C.

### Step 5 — Set up Spotify (one-time)

The Car Thing has no keyboard, so Spotify is **not** authorized on the device.
You run a one-time OAuth flow that produces a **refresh token**. All Spotify
secrets — client ID, client secret, refresh token — live server-side in
`presets.json`; the webapp never sees them.

Spotify rejects plain-`http://` redirects to a LAN IP but grants a loopback
exception to `http://127.0.0.1`, so the auth page must be reached at `127.0.0.1`.
On a headless Pi, forward the port from your laptop:

```bash
ssh -L 5005:localhost:5005 <user>@<pi-host>
```

1. **Register the app.** At https://developer.spotify.com/dashboard, create or
   open an app. Under **Edit Settings → Redirect URIs**, add
   `http://127.0.0.1:5005/` (exactly, with the trailing slash) and **Save**.

2. **Fill in `presets.json`** with `spotify_client_id` and
   `spotify_client_secret` from the dashboard.

3. **Start the server** (Step 4), then open `http://127.0.0.1:5005/auth` in a
   browser on the machine with the port-forward.

4. **Click "Authorize with Spotify"**, log in, and approve. You'll be redirected
   back and the page exchanges the code for tokens automatically.

5. **Copy the refresh token** shown on the page and paste it into `presets.json`
   as `spotify_refresh_token`. Restart the server.

This only needs doing **once** — the refresh token does not expire unless you
revoke it. Because the token lives only on the Pi, you never need to re-push the
webapp when you re-authorize.

> Until `spotify_refresh_token` is set, the Car Thing shows *"Open
> http://127.0.0.1:5005/auth on your computer to finish Spotify setup."*

The `/auth` flow requests the `playlist-read-private` and
`playlist-read-collaborative` scopes, which the Playlists screen needs.

### Step 6 — Deploy the webapp to the Car Thing

Plug the Car Thing into the **Pi's** USB, then:

```bash
adb devices                          # verify the Car Thing is connected
adb shell mount -o remount,rw /

# Push the file directly (pushing the folder nests a stale copy).
adb push car-thing-webapp/index.html /usr/share/qt-superbird-app/webapp/index.html

# Stop the kiosk Chromium serving a stale cached page after a relaunch.
# Disable its disk cache once:
adb shell 'grep -q "disk-cache-size=1" /etc/supervisord.conf || \
  sed -i "s#--user-data-dir=/var/cache/chrome_storage #&--disk-cache-size=1 --aggressive-cache-discard #" /etc/supervisord.conf'
adb shell 'rm -rf /var/cache/chrome_storage/Default/Cache /var/cache/chrome_storage/Default/"Code Cache" /var/cache/chrome_storage/Default/GPUCache 2>/dev/null'
adb shell 'supervisorctl reread; supervisorctl update'   # applies the chromium flag

# IMPORTANT: commit the writes to disk. An unclean reboot drops any rootfs writes
# still sitting in cache — without this, changes silently revert to the stock
# image. `sync` + remount-ro forces them to durable storage.
adb shell 'sync; sync; mount -o remount,ro /'

adb shell 'supervisorctl restart chromium'   # load the freshly-pushed webapp
```

> Re-run the `mount -o remount,rw /` … `sync; … remount,ro /` wrapper **any** time
> you push files to the device, or the change won't survive the next reboot.

Install the on-device backlight watcher the same way (see **How it works**):

```bash
adb push car-thing-webapp/backlight_watch.sh /usr/share/qt-superbird-app/backlight_watch.sh
adb shell 'chmod +x /usr/share/qt-superbird-app/backlight_watch.sh; supervisorctl restart dockbacklight'
```

To test the tunnel by hand before the watchdog is installed:

```bash
adb reverse tcp:5005 tcp:5005
adb shell 'curl -s http://localhost:5005/presets'   # should return JSON
```

### Step 7 — Install the systemd service

```bash
sudo cp volumepresets.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start volumepresets
journalctl -u volumepresets -f          # follow the logs
```

Edit `/etc/systemd/system/volumepresets.service` if your repo path or username
differ. The unit binds to `network-online.target` so the speakers are reachable
when the server opens its connections, and `Restart=on-failure` brings it back if
it crashes.

### Step 8 — Install the USB tunnel watchdog

The Car Thing loses its `adb reverse` mapping every time it re-enumerates on USB.
`adb-watch.sh` notices and re-establishes it:

```bash
sudo cp adb-watch.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now adb-watch
journalctl -u adb-watch -f              # watch for "tunnel up"
```

It polls rather than reacting to udev, because the Car Thing enumerates as an
RNDIS gadget (`1d6b:1014`) that no Android udev rule matches. Presence is checked
every 5s (host-side `adb devices`, essentially free); the end-to-end check — a
real HTTP fetch through the tunnel — runs when the device appears or disappears,
or once a minute as a backstop. That check has to be a real request: once the
transport dies, `adb reverse` still exits 0 and adbd still shows a listener on
the device, while nothing crosses.

> First-time adb authorization: if `adb devices` shows `unauthorized`, accept the
> prompt on the device, then replug.

### Step 9 — Enable auto-start at boot

```bash
sudo systemctl enable volumepresets
sudo systemctl enable adb-watch
```

With both enabled, powering on the Pi brings up the server and the tunnel with no
intervention — the Car Thing reaches Now Playing about a minute after boot.

To turn auto-start back off: `sudo systemctl disable volumepresets adb-watch`.

---

## How it works

- **`server.py`** — Flask, binds `0.0.0.0:5005`. Loads `presets.json` on startup
  and opens one persistent `pychromecast` connection per speaker by static IP.
  Volume writes do **not** block the HTTP response: each speaker has a writer
  thread holding only the newest target, so a fast dial spin collapses to one
  write per speaker instead of a queue that keeps landing after you stop. Holds
  all Spotify secrets server-side and **fully proxies Spotify** (the device has no
  internet) over kept-alive HTTPS connections, with an LRU cache for album art.
  Also serves the one-time auth page at `/auth`. `GET /presets` returns only
  preset/speaker names plus a `spotify_configured` flag — never secrets.
- **`car-thing-webapp/index.html`** — vanilla HTML/JS, no build step. Plain DOM
  text and CSS at 800×480, set in Circular (Spotify's own face, already on the
  Car Thing image) falling back to Noto Sans. Three views: **Now Playing** (home),
  **Mixer** (live vertical faders, one per speaker) and **Playlists** (a grid of
  covers). A strip along the top puts a tick under each physical bezel button at
  its true x position, labelled with what that button currently does. Holds **no
  credentials and makes no internet calls**.
- **`car-thing-webapp/backlight_watch.sh`** — runs **on the Car Thing** under its
  own supervisord, not on the Pi. Polls `/ping` every 3s and powers the panel off
  when the server is unreachable (Pi off, or the USB tunnel down), back on when it
  returns. It only touches `bl_power`, never `brightness`, because the stock
  firmware continuously rewrites brightness from the ambient-light sensor. Kept in
  the repo because it otherwise exists only on the device's flash and is lost on a
  reflash.
- **`adb-watch.sh` / `adb-watch.service`** — the USB tunnel watchdog (Step 8).
- **`volumepresets.service`** — systemd unit for the server.
- **`monitor_watch.sh`, `com.volumepresets.plist`** — **legacy**, from the
  original Mac + Home Assistant deployment. Kept for reference only; nothing on
  the Pi uses them. See **Legacy** at the bottom.

---

## Endpoints

| Method | Path                | Body                  | Returns |
|--------|---------------------|-----------------------|---------|
| GET    | `/presets`          | —                     | `{presets, speakers, spotify_configured}` (no secrets) |
| POST   | `/preset/<id>`      | —                     | `{ok, preset, volumes}` |
| POST   | `/preset/<id>/save` | —                     | `{ok, preset, levels}` — saves the live mix into the preset (writes `presets.json`) |
| POST   | `/volume/adjust`    | `{"delta": ±N}` or `{"delta": ±N, "speaker": "<key>"}` | `{ok, volumes, unavailable}` — ALL (proportional) or one speaker |
| GET    | `/status`           | —                     | `{active_preset, volumes, unavailable}` |
| GET    | `/ping`             | —                     | `204`, no body — liveness only, for `backlight_watch.sh` |
| GET    | `/spotify/now-playing` | —                  | Current track JSON, or `{item:null, is_playing:false}` |
| GET    | `/spotify/art?u=`   | —                     | Proxied album art (Spotify CDN hosts only), LRU-cached; `X-Art-Cache: hit\|miss` |
| GET    | `/spotify/token`    | —                     | `{ok, have_token}` (debug: confirms the server can mint a token) |
| GET    | `/spotify/playlists` | —                    | `[{id, name, image_url}]` — saved playlists (`id` is the playlist URI). Needs the `playlist-read-private` scope. |
| GET    | `/spotify/devices`  | —                     | Raw Spotify Connect devices array |
| POST   | `/spotify/play-playlist` | `{"playlist_uri": "...", "device_name"?: "..."}` | `{ok:true}` — starts the playlist. Targets are tried in order: `device_name`, then each entry of `spotify_connect_device`, then whatever Spotify session is currently active. `404 {"error":"no active session", tried, available}` if nothing is playing anywhere |
| POST   | `/spotify/play` `/pause` `/next` `/previous` | — | `{ok, status}` — transport controls |
| GET    | `/auth`             | —                     | One-time Spotify OAuth page (reach it at `127.0.0.1`) |
| GET    | `/`                 | —                     | Car Thing webapp (or auth completion if `?code=`) |

> **Playlist targets:** Spotify can only start playback on a device already
> registered to the account. A Cast group registers *only while something is
> casting to it*, and Google speakers never register at all — they run no Spotify
> client. So there is no way to start playback in the room from cold. Set
> `spotify_connect_device` to an ordered list (group first, then any real
> Spotify-app device such as a TV) and the server falls back to the active
> session when none of them is available.

> **Playlists scope:** reading playlists requires the **`playlist-read-private`**
> scope, which older tokens don't have. If `/spotify/playlists` returns
> `502 {"error":"HTTP 403"}`, re-run the `/auth` flow to mint a fresh refresh
> token and update `spotify_refresh_token`.

---

## Car Thing controls

| Input            | Action                                |
|------------------|---------------------------------------|
| Button 1         | Activate preset 1 (DESK)              |
| Button 2         | Activate preset 2 (BED)               |
| Button 3         | Activate preset 3 (AMBIENT)           |
| Button 4         | Toggle Mixer ↔ Now Playing            |
| Back button (5th) | Open **Playlists** (press again to return to Now Playing) |
| Dial scroll      | Volume up/down — or move the selection on the Playlists screen |
| Tap a fader      | Control just that speaker (~4s, then back to ALL) |
| Tap **SAVE**, then 1/2/3 | Save the current mix into that preset |

The strip along the top of the screen shows what each bezel button does in the
current view, with a tick under each one at its real position. On the Playlists
screen buttons 1 and 2 become **Play** and **Refresh**; arming SAVE turns the
three preset labels blue.

**Playlists screen** — a 4×2 grid of covers; the dial moves the selection and the
grid scrolls a row at a time. **Button 1** plays the highlighted playlist,
**button 2** re-fetches, and the back button returns to Now Playing. Cached for
the session. Desktop testing: `Esc`/`Backspace` acts as the back button, and
`1` plays, `2` refreshes, ↑/↓ move the selection.

> The back button is read off the hardware websocket as a 5th button id
> (`{type:'button', button:5}`) or a `{type:'back'}` message. If your Car Thing
> emits a different id, adjust the `b === '5'` check in `connectHardwareWS()`.

By default the dial scales **all speakers together by a fixed ratio** — the
loudest moves by the step and the rest scale to match, preserving the balance and
capping at 1.0. **Tap a fader** to focus one speaker; the dial then adjusts only
that one until it auto-returns to ALL. A fader the server can't drive (an offline
speaker) is dimmed and shows `--` rather than silently swallowing the dial.

The dial is a rotary encoder the webview delivers as DOM `wheel` events — one
detent per event. Desktop fallback: keys `1` `2` `3` `4`, ↑/↓, mouse wheel, and
clicking a fader.

---

## Troubleshooting

- **Server logs**: `journalctl -u volumepresets -f`
- **Watchdog logs**: `journalctl -u adb-watch -f`
- **Speaker shows `[OFFLINE]` at startup**: its IP moved. Confirm the DHCP
  reservation, `ping` the address, then restart the service — connections are
  opened once at startup and not retried.
- **Car Thing shows "Connecting to server…"**: almost always the USB tunnel. The
  device has no WiFi — it reaches the Pi only via `adb reverse`:
  ```bash
  adb devices                                        # listed as "device"?
  adb reverse tcp:5005 tcp:5005                      # (re)establish it
  adb shell 'curl -s http://localhost:5005/presets'  # device -> server works?
  ```
  Note `adb reverse --list` is unreliable on this device (it often returns a
  protocol fault even when the tunnel is fine) — trust the `curl` instead.
- **Car Thing screen is black**: `backlight_watch.sh` powers the panel off when
  the server is unreachable. Check the server and tunnel first; the log of
  transitions is at `/var/cache/blwatch.log` on the device (its timestamps are
  meaningless — the Car Thing has no RTC).
- **Car Thing shows an old version of the webapp**: the kiosk Chromium cached it,
  or the rootfs write wasn't committed. Confirm the cache flag
  (`adb shell 'grep disk-cache-size /etc/supervisord.conf'`), and make sure you
  re-ran the `sync; mount -o remount,ro /` wrapper after pushing.
- **Volume ramps to maximum on its own**: a TV in the room is signed in to
  Spotify. See the note at the top of this README.
- **Playlists show "PLAY SOMETHING ON SPOTIFY FIRST"**: nothing is playing
  anywhere, so there is no device for Spotify to start playback on. See
  **Playlist targets**.
- **Now Playing blank, or TLS errors in the log**: the Pi has no RTC. If it boots
  without network its clock is wrong and certificate validation fails. Confirm
  with `date`, and check `curl http://127.0.0.1:5005/spotify/token`.
- **Re-authorize Spotify**: redo Step 5. The token lives only on the Pi, so there
  is no need to re-push anything to the Car Thing.

---

## Legacy: the Mac + Home Assistant version

This project began as a Mac deployment: a Flask server on a laptop, talking to
the speakers through a **Home Assistant** container over its REST API, with
`monitor_watch.sh` starting everything when a particular external monitor was
connected and a launchd agent keeping it alive.

That version is preserved on the **`legacy/mac`** branch, including its own
README with the Docker, Home Assistant and launchd setup steps:

```bash
git checkout legacy/mac
```

The Pi version on `main` drops Docker and Home Assistant entirely — the server
talks to the speakers directly with `pychromecast` — and replaces the
monitor-triggered launchd agent with systemd units plus a USB tunnel watchdog.
`monitor_watch.sh` and `com.volumepresets.plist` are still present on `main` for
reference but are unused.
