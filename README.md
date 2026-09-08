# Volume Presets

A local volume preset system for three Google Home speakers, controlled via a
modded Spotify Car Thing.

The Car Thing runs a custom webapp talking to a Python Flask server on your Mac.
The server sends absolute volume commands to each speaker through a local
**Home Assistant** instance (running in Docker), which talks to the speakers over
the LAN via the Google Cast integration. Home Assistant handles all Google auth
internally with a long-lived token that never expires — no cloud round-trip and
no 24h token churn. The Mac server auto-starts when a specific external monitor
is connected, and shuts down when it disconnects.

**Networking:** the Car Thing has no WiFi of its own — it reaches the Mac only
over USB. `monitor_watch.sh` runs `adb reverse tcp:5005 tcp:5005`, which tunnels
the device's `localhost:5005` to the Mac server. Because the device also has no
internet, the Mac **proxies** all Spotify calls (now-playing + album art); no
Spotify tokens ever touch the device.

```
[Car Thing] ──USB (adb reverse :5005)──> [Mac Flask server] ──REST──> [Home Assistant] ──> speakers
   webapp @ localhost:5005                       │                     (Docker, :8123)
                                                 └── proxies ──> [Spotify Web API] (now-playing, art)
```

---

## Configuration & secrets

All config lives in **`presets.json`**, which holds secrets (Home Assistant
long-lived token, Spotify client secret + refresh token) and is therefore
**git-ignored**. The repo ships **`presets.example.json`** as a template — copy
it to `presets.json` and fill in your own values (see Setup). Never commit
`presets.json`.

---

## Setup

### Step 1 — Give your speakers stable IPs (recommended)

Home Assistant discovers the speakers over the LAN, so they should have **stable
IP addresses**, or Home Assistant may lose them when DHCP renews. Use DHCP
reservations on your router:

1. Find each speaker's MAC address in the Google Home app:
   - Open the Google Home app → tap the device → gear icon → **Device information**.
   - Note the **MAC address** (looks like `AA:BB:CC:11:22:33`).
2. Log into your router admin page (usually `http://192.168.1.1` or
   `http://10.0.0.1` — check the sticker on the router).
3. Find the section called **DHCP Reservations**, **Address Reservations**, or
   **Static Leases**.
4. Add a reservation for each speaker's MAC, assigning it a fixed IP on your
   LAN (e.g. `10.0.0.50`, `10.0.0.51`, `10.0.0.52`).
5. Reboot the speakers so they pick up the new lease.

### Step 2 — Create `presets.json`

Copy the template to create your local config:

```bash
cp presets.example.json presets.json
```

`presets.json` holds your secrets (Home Assistant token, Spotify client secret,
refresh token) and is **git-ignored** — never commit it. You'll fill in the
`ha_token` and per-speaker `entity_id` values during **Step 3**, and the Spotify
fields during **Step 8**. For now, just tune the volume levels.

Volumes are floats from **0.0 (muted) to 1.0 (max)**. Tune the per-speaker
levels to taste — the three presets `DESK`, `BED`, and `AMBIENT` are starting
points.

### Step 3 — Set up Home Assistant

Speaker commands are routed through a local Home Assistant instance running in
Docker. Home Assistant manages the Google Cast connection and auth for you.

1. **Install Docker Desktop for Mac** if you don't already have it:
   https://www.docker.com/products/docker-desktop/ — install and launch it.

2. **Run the Home Assistant container:**

   ```bash
   docker run -d \
     --name homeassistant \
     --restart unless-stopped \
     -p 8123:8123 \
     -v "$HOME/homeassistant:/config" \
     ghcr.io/home-assistant/home-assistant:stable
   ```

3. **Create an account:** open `http://localhost:8123` in a browser and follow
   the onboarding to create your Home Assistant account.

4. **Add the Google Cast integration:** go to **Settings → Devices & Services**,
   click **Add Integration**, search for **Google Cast**, and add it.

5. **Discover the speakers:** your three speakers should be discovered
   automatically. Add all three when prompted.

6. **Find each entity ID:** go to **Settings → Devices & Services → Google Cast**
   and click each device. Each speaker has an entity ID in the form
   `media_player.device_name` (e.g. `media_player.nest_audio`). Note all three.

7. **Create a long-lived access token:** click your **profile** (bottom-left
   corner) → scroll to the bottom → **Long-Lived Access Tokens** →
   **Create Token** → name it `volumepresets` → **copy it immediately** (it is
   shown only once).

8. **Fill in `presets.json`:** replace the placeholders with the values you just
   collected:

   - `REPLACE_WITH_HA_LONG_LIVED_TOKEN` → your long-lived token
   - `REPLACE_WITH_NEST_AUDIO_ENTITY_ID` → the Nest Audio entity ID
   - `REPLACE_WITH_GOOGLE_HOME_ENTITY_ID` → the Google Home entity ID
   - `REPLACE_WITH_CHROMECAST_ENTITY_ID` → the Chromecast entity ID

> **Note:** Home Assistant is intentionally **not** exposed to the internet. It
> only listens on `localhost` (`-p 8123:8123` binds the local port) and
> communicates with the speakers over your local LAN. The long-lived token never
> leaves your Mac.

### Step 4 — Create a venv and install Flask

macOS's system Python is externally-managed, so install Flask into a local
virtualenv inside the project folder. `monitor_watch.sh` looks for the venv
at `./venv/bin/python3`.

```bash
cd /path/to/SmartRoomThing
python3 -m venv venv
./venv/bin/pip install --upgrade pip flask
```

**CA certificates (important):** the server makes outbound HTTPS calls to Spotify,
so its Python needs a working CA bundle. The python.org macOS installer ships
without one. If you used it, run its certificate installer once (adjust the
version), or the server's Spotify proxy will fail with
`CERTIFICATE_VERIFY_FAILED`:

```bash
/Applications/Python\ 3.11/Install\ Certificates.command
```

(Homebrew Python already has certs and needs nothing here.)

### Step 5 — Test the server manually

```bash
./venv/bin/python3 server.py
```

You should see a startup banner with the URL, the Home Assistant URL, the Spotify
status, and each speaker's entity ID. Open `http://127.0.0.1:5005` in a browser on
the Mac — you should see the Car Thing webapp. Try
`curl -X POST http://127.0.0.1:5005/preset/1` and verify the speakers respond.

Stop the server with Ctrl-C.

### Step 6 — Install ADB (needed once for Car Thing deployment)

```bash
brew install android-platform-tools
```

### Step 7 — Deploy the webapp to the Car Thing

Plug the Car Thing into your Mac over USB, then:

```bash
adb devices                          # verify Car Thing is connected
adb shell mount -o remount,rw /

# Push the webapp file directly (pushing the folder nests a stale copy).
adb push car-thing-webapp/index.html /usr/share/qt-superbird-app/webapp/index.html

# Stop the kiosk Chromium from serving a stale cached page after a relaunch
# (a dock power-glitch can reboot the device). Disable its disk cache once:
adb shell 'grep -q "disk-cache-size=1" /etc/supervisord.conf || \
  sed -i "s#--user-data-dir=/var/cache/chrome_storage #&--disk-cache-size=1 --aggressive-cache-discard #" /etc/supervisord.conf'
adb shell 'rm -rf /var/cache/chrome_storage/Default/Cache /var/cache/chrome_storage/Default/"Code Cache" /var/cache/chrome_storage/Default/GPUCache 2>/dev/null'
adb shell 'supervisorctl reread; supervisorctl update'   # applies the chromium flag

# IMPORTANT: commit the writes to disk. The dock power-cycles the Car Thing on
# dock/undock (an *unclean* reboot), which drops any rootfs writes still sitting
# in cache — so without this, your changes silently revert to the stock image on
# the next dock. `sync` + remount-ro forces them to durable storage.
adb shell 'sync; sync; mount -o remount,ro /'

adb shell 'supervisorctl restart chromium'   # load the freshly-pushed webapp
```

> Re-run the `mount -o remount,rw /` … `sync; … remount,ro /` wrapper any time you
> push new files to the device, or the change won't survive the next dock.

The webapp reaches the server at `http://localhost:5005` **on the device**,
tunneled to your Mac over USB. In normal operation `monitor_watch.sh` keeps
`adb reverse tcp:5005 tcp:5005` running for you (Step 9). To test by hand before
that's installed, set it yourself once the device is up:

```bash
adb reverse tcp:5005 tcp:5005
adb shell 'curl -s http://localhost:5005/presets'   # should return JSON
```

### Step 8 — Set up Spotify (one-time, on your Mac)

The Car Thing has no keyboard, so Spotify is **not** authorized on the device.
You run a one-time OAuth flow on your Mac at `http://127.0.0.1:5005/auth`, which
produces a **refresh token**. All Spotify secrets — client ID, client secret,
and refresh token — live server-side in `presets.json`; the webapp never sees
them. It asks the server for a short-lived access token at `/spotify/token`
whenever it needs one. Nothing secret is ever pushed to the Car Thing.

1. **Register the app.** Go to https://developer.spotify.com/dashboard, log in,
   and either create an app or open the existing one. Under **Edit Settings →
   Redirect URIs**, make sure `http://127.0.0.1:5005/` is listed (exactly — with
   the trailing slash), then **Save**. Spotify rejects plain-`http://` redirects
   to a LAN IP like `10.0.0.144` but grants a loopback exception to
   `http://127.0.0.1`, which is why auth must be done from the Mac.

2. **Fill in `presets.json`.** Copy the **Client ID** and **Client Secret** from
   the dashboard into:

   - `spotify_client_id`
   - `spotify_client_secret`

3. **Start the server** (Step 5) and, in a browser on the Mac, open
   `http://127.0.0.1:5005/auth`.

4. **Click "Authorize with Spotify"** and log in / approve the prompt. You'll be
   redirected back and the page will exchange the code for tokens automatically.

5. **Copy the refresh token** shown on the page (use the **Copy** button).

6. **Paste it into `presets.json`** as `spotify_refresh_token`, then restart the
   server so it picks up the new value:

   ```bash
   launchctl kickstart -k gui/$UID/com.volumepresets
   ```

   (Or just unplug/replug the monitor.)

This only needs to be done **once**. The refresh token does not expire unless you
revoke it in your Spotify dashboard. Because the token lives only on the Mac, you
do **not** need to re-push the webapp to the Car Thing when you (re)authorize —
`adb push` is only for changes to `index.html` itself. The `127.0.0.1` address is
used both for this one-time auth (in your Mac browser) and by the Car Thing at
runtime (its USB tunnel maps the device's `localhost:5005` to the Mac).

> Until `spotify_refresh_token` is set in `presets.json`, the Car Thing shows
> *"Open http://127.0.0.1:5005/auth on your Mac to complete setup."*

### Step 9 — Install launchd for auto-start

This makes the server start whenever the LG HDR WFHD monitor is connected, and
shut down when it disconnects.

```bash
# Replace the username placeholder in the plist with your actual username
sed -i '' 's/REPLACE_WITH_YOUR_USERNAME/'"$USER"'/g' com.volumepresets.plist

# Install
cp com.volumepresets.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.volumepresets.plist
```

Verify it's running:

```bash
launchctl list | grep volumepresets
tail -f ~/Library/Logs/volumepresets.log
```

### Step 10 — Tune your presets

Edit `presets.json` whenever you want to adjust the levels. After saving,
restart the server (`launchctl kickstart -k gui/$UID/com.volumepresets`, or
just unplug/replug the monitor). The Car Thing reflects the new values on the
next preset press.

---

## Raspberry Pi Setup

This is the **lightweight native deployment** designed to run 24/7 on a
**Raspberry Pi 2 (1GB RAM, ARM)**. It drops the Mac + Docker + Home Assistant
stack entirely: the server talks to the speakers **directly over the LAN with
[`pychromecast`](https://github.com/home-assistant-libs/pychromecast)**, opening
one persistent connection per speaker by **static IP** (no zeroconf/mDNS
discovery, so the footprint stays tiny). Everything else — the Car Thing webapp,
the USB `adb reverse` tunnel, and the server-side Spotify proxy — works exactly
as on the Mac.

```
[Car Thing] ──USB (adb reverse :5005)──> [Raspberry Pi Flask server] ──Cast──> speakers
   webapp @ localhost:5005                          │                  (by static IP, :8009)
                                                     └── proxies ──> [Spotify Web API] (now-playing, art)
```

> **Config format note:** on this branch, `presets.example.json` uses the Pi
> format — each speaker has an `ip_address` (its static DHCP IP) instead of a
> Home Assistant `entity_id`, and the `ha_url` / `ha_token` fields are gone.
> Spotify and preset fields are unchanged. (If you're following the **Mac + Home
> Assistant** steps above instead, keep your existing HA-format `presets.json`.)

### Pi Step 1 — Give your speakers static IPs

Same as the Mac setup's Step 1: add a **DHCP reservation** on your router for
each speaker's MAC so its IP never changes. The server connects to these IPs
directly, so a stable address per speaker is **required** here (there's no
discovery to fall back on). Note each speaker's IP (e.g. `10.0.0.50`,
`10.0.0.51`, `10.0.0.52`).

### Pi Step 2 — Install dependencies

```bash
sudo apt update
sudo apt install -y python3-venv adb
```

Then create the virtualenv and install the Python packages (Flask + pychromecast):

```bash
cd /home/pi/SmartRoomThing          # adjust if you cloned elsewhere
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install flask pychromecast
```

> On a Pi 2 the `pychromecast` install pulls in `zeroconf`/`protobuf` and can
> take a few minutes (some wheels build from source). It only uses them for the
> direct host connection — discovery is never run.

### Pi Step 3 — Copy and edit `presets.json`

```bash
cp presets.example.json presets.json
```

Edit `presets.json` and set each speaker's `ip_address` to the static IP from
Pi Step 1, then tune the per-speaker preset volumes (floats `0.0`–`1.0`). Fill
in the Spotify fields too if you use Now Playing (see the Mac **Step 8** for the
one-time OAuth flow — it's identical, just open `http://127.0.0.1:5005/auth` on
the Pi or via an SSH port-forward). `presets.json` is git-ignored — never commit
it.

### Pi Step 4 — Test the server manually

```bash
./venv/bin/python3 server.py
```

The startup banner lists each speaker with `[connected]` or `[OFFLINE]`. An
offline speaker is logged as a warning and skipped — the server never crashes or
blocks on an unreachable speaker. Verify from another machine on the LAN:

```bash
curl -X POST http://<pi-ip>:5005/preset/1     # speakers should jump to the DESK mix
```

Stop with Ctrl-C.

### Pi Step 5 — Install the systemd service

```bash
sudo cp volumepresets.service /etc/systemd/system/
sudo systemctl daemon-reload
```

Edit `/etc/systemd/system/volumepresets.service` if your repo path or username
differ from `/home/pi/SmartRoomThing` / `pi`. Then start it **manually** (we are
in the testing phase — it is intentionally **not** enabled at boot yet):

```bash
sudo systemctl start volumepresets
journalctl -u volumepresets -f          # follow the logs
```

The service binds to `network-online.target`, so the speakers are reachable when
the server starts its connections. `Restart=on-failure` brings it back if it
crashes.

### Pi Step 6 — Deploy the webapp to the Car Thing

This is identical to the Mac setup's **Step 7** (`adb push` the webapp,
disable the kiosk Chromium cache, commit the rootfs writes). `adb` works the
same on the Pi — just run those commands on the Pi with the Car Thing plugged
into the **Pi's** USB.

### Pi Step 7 — Install the USB tunnel watchdog

The Car Thing loses its `adb reverse` mapping every time it re-enumerates on USB.
`adb-watch.sh` notices and re-establishes it:

```bash
sudo cp adb-watch.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now adb-watch
journalctl -u adb-watch -f              # watch for "tunnel up"
```

It polls every 5s rather than reacting to udev, because the Car Thing enumerates
as an RNDIS gadget (`1d6b:1014`) that no Android udev rule matches. The health
check is a real HTTP fetch through the tunnel: once the transport dies,
`adb reverse` still exits 0 and adbd still shows a listener on the device, but
nothing crosses.

> First-time adb authorization: if `adb devices` shows `unauthorized`, accept the
> prompt on the device, then replug.

### Pi Step 8 — Enable auto-start at boot

```bash
sudo systemctl enable volumepresets
sudo systemctl enable adb-watch
```

With both enabled, powering on the Pi brings up the server and the tunnel with no
intervention — the Car Thing reaches Now Playing about a minute after boot.

To turn auto-start back off: `sudo systemctl disable volumepresets adb-watch`.

---

## How it works

- **`server.py`** — Flask, binds `0.0.0.0:5005`. Loads `presets.json` on
  startup. `POST /preset/<id>` fans out to all three speakers concurrently via
  `ThreadPoolExecutor`, calling the Home Assistant REST endpoint
  `POST {ha_url}/api/services/media_player/volume_set` (with a
  `Bearer {ha_token}` header) and a body of
  `{"entity_id": "<entity_id>", "volume_level": <0.0-1.0>}`. Holds all Spotify
  secrets server-side and **fully proxies Spotify** (the device has no internet):
  `GET /spotify/now-playing` returns the current track and `GET /spotify/art`
  streams album art from the Spotify CDN. Access tokens are minted and cached on
  the server from the stored refresh token + client secret and never leave the
  Mac. Also serves the one-time auth page at `/auth` (completing the OAuth
  exchange when the redirect lands on `/` with `?code=`). `GET /presets` returns
  only preset/speaker names plus a `spotify_configured` flag — never the HA
  token or Spotify secrets.
- **`car-thing-webapp/index.html`** — vanilla HTML/JS, no build step. Plain DOM
  text and CSS at 800×480, set in Circular (Spotify's own face, already on the
  Car Thing image) falling back to Noto Sans. Reaches the server at
  `http://localhost:5005` (USB tunnel). Two views: **Mixer** (live
  vertical faders, one per speaker — the home screen) and **Now Playing**
  (auto-switches when Spotify is playing). Turning the dial updates the faders
  live; tapping a fader focuses that speaker for individual control (auto-returns
  to ALL after a few seconds). Holds **no credentials and makes no internet
  calls** — it polls `/spotify/now-playing` and loads art via `/spotify/art`.
  `init()` retries until the server is reachable, then either starts or (if
  `/presets` reports Spotify unconfigured) shows a message pointing to the Mac
  auth page.
- **`monitor_watch.sh`** — polls `system_profiler SPDisplaysDataType` every
  5 seconds for the string `LG HDR WFHD`. On connect it starts the
  `homeassistant` Docker container (waiting up to 30s for its API to come up),
  starts the Flask server, and establishes `adb reverse tcp:5005 tcp:5005` so the
  Car Thing can reach the server over USB (re-asserted each poll, so it survives
  a Car Thing reboot/replug). On disconnect it stops the Flask server, removes
  the reverse, and stops the container.
- **`com.volumepresets.plist`** — launchd agent that keeps `monitor_watch.sh`
  alive across logins and crashes.

---

## Endpoints

| Method | Path                | Body                  | Returns |
|--------|---------------------|-----------------------|---------|
| GET    | `/presets`          | —                     | `{presets, speakers, spotify_configured}` (no secrets) |
| POST   | `/preset/<id>`      | —                     | `{ok, preset, volumes}` |
| POST   | `/preset/<id>/save` | —                     | `{ok, preset, levels}` — saves the live mix into the preset (writes `presets.json`) |
| POST   | `/volume/adjust`    | `{"delta": ±N}` or `{"delta": ±N, "speaker": "<key>"}` | `{ok, volumes}` — ALL (proportional) or one speaker |
| GET    | `/status`           | —                     | `{active_preset, volumes}` |
| GET    | `/spotify/now-playing` | —                  | Current track JSON, or `{item:null, is_playing:false}` |
| GET    | `/spotify/art?u=`   | —                     | Proxied album art image (Spotify CDN hosts only) |
| GET    | `/spotify/token`    | —                     | `{ok, have_token}` (debug: confirms server can mint a token) |
| GET    | `/spotify/playlists` | —                    | `[{id, name, image_url}]` — the user's saved playlists (live; `id` is the playlist URI). Needs the `playlist-read-private` scope. |
| GET    | `/spotify/devices`  | —                     | Raw Spotify Connect devices array |
| POST   | `/spotify/play-playlist` | `{"playlist_uri": "...", "device_name"?: "..."}` | `{ok:true}` — starts the playlist on the named Connect device (defaults to `spotify_connect_device` from config); `404 {"error":"device not found"}` if no match |
| POST   | `/spotify/play` `/pause` `/next` `/previous` | — | `{ok, status}` — transport controls (resume/pause/skip; `/spotify/play` is unchanged) |
| GET    | `/auth`             | —                     | One-time Spotify OAuth page (run on the Mac) |
| GET    | `/`                 | —                     | Car Thing webapp (or auth completion if `?code=`) |

> **Playlists screen & Spotify scope:** the Car Thing's **back button** (5th
> button) opens a **Playlists** screen — dial scrolls, **button 1** plays the
> selected playlist on the `spotify_connect_device` (set this in `presets.json`),
> **button 2** refreshes, back button exits. Reading playlists requires the
> **`playlist-read-private`** scope, which older tokens don't have — if
> `/spotify/playlists` returns `502 {"error":"HTTP 403"}`, **re-run the `/auth`
> flow** (it now requests the playlist scopes) to mint a fresh refresh token and
> update `spotify_refresh_token` in `presets.json`.

---

## Car Thing controls

| Input            | Action                                |
|------------------|---------------------------------------|
| Button 1         | Activate preset 1 (DESK)              |
| Button 2         | Activate preset 2 (BED)               |
| Button 3         | Activate preset 3 (AMBIENT)           |
| Button 4         | Toggle Mixer ↔ Now Playing view       |
| Back button (5th) | Open the **Playlists** screen (press again to exit to Mixer) |
| Dial scroll      | Volume up/down (live faders) — or scroll the playlist on the Playlists screen |
| Tap a fader      | Control just that speaker (~4s, then back to ALL) |
| Tap **SAVE**, then 1/2/3 | Save the current mix into that preset |

**Playlists screen** (open with the back button): the dial scrolls the list,
**button 1** plays the highlighted playlist on the Spotify Connect group
(`spotify_connect_device` in `presets.json`), **button 2** re-fetches the list,
and the back button returns to the Mixer. Text-only list, cached for
the session. Desktop testing: `Esc`/`Backspace` acts as the back button, and on
the Playlists screen `1` plays, `2` refreshes, and ↑/↓ move the selection.

> The back button is read off the hardware websocket as a 5th button id
> (`{type:'button', button:5}`) or a `{type:'back'}` message. If your Car Thing
> emits a different id for it, adjust the `b === '5'` check in
> `connectHardwareWS()` in `car-thing-webapp/index.html`.

By default the dial scales **all speakers together by a fixed ratio** — the
loudest moves by the step and the rest scale to match, preserving the per-speaker
balance and capping at 1.0 so nothing clips. **Tap a fader** to focus one
speaker; the dial then adjusts only that one until it auto-returns to ALL a few
seconds later. The Mixer view shows all three live levels and updates as you turn
the dial.

**Saving presets from the device:** dial in the mix you want, tap the **SAVE**
button (top-right of the Mixer), then press preset button **1, 2, or 3** — the
current levels are written into that preset in `presets.json`. (Tapping SAVE
again, or waiting a few seconds, cancels.)

The dial is a rotary encoder that the webview delivers as DOM `wheel` events
(it has no keyboard handler). Desktop/browser fallback for testing: keys
`1` `2` `3` `4`, arrow up / arrow down, mouse wheel, and clicking a fader.

---

## Troubleshooting

- **Server logs**: `tail -f ~/Library/Logs/volumepresets.log`
- **Check Home Assistant is running**: `docker ps | grep homeassistant`
- **Restart Home Assistant**: `docker restart homeassistant`
- **Home Assistant logs**: `docker logs -f homeassistant`
- **Test the HA API directly**:
  ```bash
  curl -H "Authorization: Bearer YOUR_TOKEN" http://localhost:8123/api/
  ```
  Should return `{"message": "API running."}`.
- **Test a volume set directly**:
  ```bash
  curl -X POST http://localhost:8123/api/services/media_player/volume_set \
    -H "Authorization: Bearer YOUR_TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"entity_id": "media_player.YOUR_ENTITY", "volume_level": 0.5}'
  ```
- **Speaker not responding**: confirm the speaker shows up in Home Assistant
  (Settings → Devices & Services → Google Cast), confirm its DHCP reservation is
  still active, and verify the speaker is on the same Wi-Fi network as the Mac.
- **Car Thing shows "Connecting to Mac server…" or the auth message**: this is
  almost always the USB tunnel. The device has no WiFi — it reaches the Mac only
  via `adb reverse`. Check:
  ```bash
  adb devices                                  # Car Thing listed as "device"?
  adb reverse --list                           # should show 5005
  adb reverse tcp:5005 tcp:5005                # (re)establish it
  adb shell 'curl -s http://localhost:5005/presets'   # device -> server works?
  ```
  `monitor_watch.sh` re-asserts the reverse every 5s, so it self-heals after a
  reboot/replug once adb reconnects. If `adb` isn't found by the agent, install
  it (`brew install android-platform-tools`) — the log will warn about this.
- **Car Thing shows an old version of the webapp**: the kiosk Chromium cached it.
  Confirm the cache-disable flag is applied (`adb shell 'grep disk-cache-size
  /etc/supervisord.conf'`); if missing, re-run the Chromium steps in Step 7. Force
  a clean reload: `adb shell 'rm -rf /var/cache/chrome_storage/Default/Cache;
  supervisorctl restart chromium'`. Also make sure you pushed
  `car-thing-webapp/index.html` (the file), not the folder.
- **Now Playing blank / no album art, or `CERTIFICATE_VERIFY_FAILED` in the log**:
  the server's Python can't verify Spotify's TLS cert. Run the cert installer for
  your Python (see Step 4): `/Applications/Python\ 3.11/Install\ Certificates.command`.
  Verify with `curl http://127.0.0.1:5005/spotify/token` → `{"ok":true,...}`.
- **Reload launchd after editing the plist**:
  ```bash
  launchctl unload ~/Library/LaunchAgents/com.volumepresets.plist
  launchctl load ~/Library/LaunchAgents/com.volumepresets.plist
  ```
- **Re-authorize Spotify**: redo Step 8 — run the `/auth` flow on the Mac again
  to mint a fresh refresh token, paste it into `index.html` as
  `STORED_REFRESH_TOKEN`, and re-push to the Car Thing. (Revoke the old token at
  developer.spotify.com if needed.)
- **Monitor not detected**: the script greps for the exact string
  `LG HDR WFHD` — if your monitor reports a different name, run
  `system_profiler SPDisplaysDataType | grep -i <hint>` to find the right
  substring and update `MONITOR_NAME` in `monitor_watch.sh`.
