#!/usr/bin/env python3
"""Volume presets server for Google Home speakers, controlled by a Car Thing.

Speaker volume commands are sent natively over the LAN with pychromecast. On
startup the server opens one persistent Cast connection per speaker (by static
IP — no zeroconf/mDNS discovery) and caches it; volume changes are pushed
directly to the device. This replaces the previous Home Assistant + Docker
setup, so the whole thing runs comfortably on a Raspberry Pi 2.

Spotify is still fully proxied server-side (the Car Thing has no internet of its
own); that logic is unchanged.
"""

import base64
import collections
import json
import os
import time
import sys
import threading
import uuid
import http.client
import urllib.request
import urllib.error
import urllib.parse
import pychromecast
from flask import Flask, jsonify, request, send_from_directory, make_response

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PRESETS_PATH = os.path.join(SCRIPT_DIR, "presets.json")
WEBAPP_DIR = os.path.join(SCRIPT_DIR, "car-thing-webapp")

with open(PRESETS_PATH, "r") as f:
    CONFIG = json.load(f)

SPEAKERS = CONFIG["speakers"]
PRESETS = CONFIG["presets"]
SERVER_HOST = CONFIG.get("server_host", "0.0.0.0")
SERVER_PORT = int(CONFIG.get("server_port", 5005))

# The standard Google Cast control port. Speakers have static DHCP reservations,
# so we connect straight to this host:port and never touch zeroconf/mDNS.
CAST_PORT = 8009


def _config_value(key):
    """Return a config string, treating REPLACE_WITH... placeholders as unset."""
    value = CONFIG.get(key, "")
    if isinstance(value, str) and value.startswith("REPLACE_WITH"):
        return ""
    return value


# All Spotify secrets live here on the server, never in the Car Thing webapp.
SPOTIFY_CLIENT_ID = _config_value("spotify_client_id")
SPOTIFY_CLIENT_SECRET = _config_value("spotify_client_secret")
SPOTIFY_REFRESH_TOKEN = _config_value("spotify_refresh_token")
SPOTIFY_CONFIGURED = bool(
    SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET and SPOTIFY_REFRESH_TOKEN
)
# Name of the Spotify Connect target (the Google speaker group) to start
# playlists on, exactly as Spotify reports it. Read from config so the device
# name is never hardcoded in the webapp; matched case-insensitively by substring.
SPOTIFY_CONNECT_DEVICE = _config_value("spotify_connect_device")
# Spotify only grants the http (non-https) loopback exception to 127.0.0.1, and
# the redirect URI must match the dashboard exactly. Keep it at the root path.
SPOTIFY_REDIRECT_URI = f"http://127.0.0.1:{SERVER_PORT}/"

STATE = {
    "active_preset": None,
    "volumes": {key: 0.0 for key in SPEAKERS},
}

# Persistent Cast connections, one per speaker, opened once at startup and
# reused for every volume command. A speaker that is unreachable at startup is
# stored as None; we never block a request retrying it.
#   CASTS[key]      -> pychromecast.Chromecast | None
#   CAST_LOCKS[key] -> threading.Lock guarding mutations on that connection
CASTS = {}
CAST_LOCKS = {key: threading.Lock() for key in SPEAKERS}


def _connect_speaker(key):
    """Open a persistent Cast connection to one speaker by static IP.

    Returns the connected Chromecast, or None if it can't be reached. Connecting
    by host (not mDNS discovery) keeps the memory/CPU footprint tiny on the Pi
    and works as long as the speaker has a static DHCP reservation.
    """
    ip = SPEAKERS[key]["ip_address"]
    try:
        # get_chromecast_from_host builds a host-only connection (no zeroconf):
        # (host, port, uuid, model_name, friendly_name). The uuid is just a local
        # identifier for a direct host connection, so a random one is fine.
        cast = pychromecast.get_chromecast_from_host(
            (ip, CAST_PORT, uuid.uuid4(), None, None),
            tries=1, retry_wait=2, timeout=5,
        )
        cast.wait(timeout=10)  # block until the device is ready for commands
        print(f"[{key}] connected to {ip} ({cast.cast_info.friendly_name or 'cast'})")
        return cast
    except Exception as e:
        print(f"[{key}] WARNING: could not connect to {ip}: {e}")
        return None


def _init_casts():
    """Connect to every speaker at startup. Never crashes on an unreachable one."""
    for key in SPEAKERS:
        CASTS[key] = _connect_speaker(key)


app = Flask(__name__, static_folder=None)


@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


# ---------------------------------------------------------------------------
# One-time Spotify OAuth helper, served on the Mac at /auth.
#
# The Car Thing has no keyboard, so we don't authorize on the device. Instead,
# the user runs this page once on the Mac at http://127.0.0.1:5005/auth, clicks
# "Authorize with Spotify", and the resulting refresh token is shown so it can be
# pasted into car-thing-webapp/index.html as STORED_REFRESH_TOKEN.
#
# The Spotify redirect URI is the root (http://127.0.0.1:5005/), so the OAuth
# code lands back on "/" with ?code=...; serve_webapp() detects that and serves
# this same page to complete the token exchange.
# ---------------------------------------------------------------------------
AUTH_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Spotify Auth — Volume Presets</title>
<style>
  body { background:#111; color:#ddd; font-family: ui-monospace, Menlo, Consolas, monospace;
         padding:40px; line-height:1.6; max-width:760px; margin:0 auto; }
  h1 { color:#1db954; }
  button { background:#1db954; color:#fff; border:none; padding:14px 28px;
           font-size:15px; border-radius:24px; cursor:pointer; font-family:inherit; }
  button:hover { background:#1ed760; }
  code, pre { background:#000; color:#0f0; padding:12px; border-radius:4px;
              display:block; white-space:pre-wrap; word-break:break-all; }
  code.inline { display:inline; padding:2px 6px; }
  .hidden { display:none; }
</style>
</head>
<body>
<h1>Spotify Authorization</h1>
<div id="start">
  <p>Client ID: <code class="inline">__CLIENT_ID__</code></p>
  <p>This authorizes Volume Presets to read your currently-playing track. You
     only need to do this once.</p>
  <button id="go">Authorize with Spotify</button>
</div>
<div id="result" class="hidden"></div>
<div id="error" class="hidden"></div>
<script>
var CLIENT_ID = '__CLIENT_ID__';
var CLIENT_SECRET = '__CLIENT_SECRET__';
var REDIRECT_URI = '__REDIRECT_URI__';
var SCOPE = 'user-read-playback-state user-read-currently-playing user-modify-playback-state playlist-read-private playlist-read-collaborative';

function qs(name){ return new URLSearchParams(location.search).get(name); }

document.getElementById('go').onclick = function(){
  // localStorage survives the full-page navigation to Spotify and back
  // (sessionStorage does not, reliably).
  localStorage.setItem('vp_client_id', CLIENT_ID);
  localStorage.setItem('vp_client_secret', CLIENT_SECRET);
  var url = 'https://accounts.spotify.com/authorize'
    + '?response_type=code'
    + '&client_id=' + encodeURIComponent(CLIENT_ID)
    + '&scope=' + encodeURIComponent(SCOPE)
    + '&redirect_uri=' + encodeURIComponent(REDIRECT_URI);
  location.href = url;
};

async function exchange(code){
  var cid = localStorage.getItem('vp_client_id') || CLIENT_ID;
  var csec = localStorage.getItem('vp_client_secret') || CLIENT_SECRET;
  var body = new URLSearchParams();
  body.set('grant_type', 'authorization_code');
  body.set('code', code);
  body.set('redirect_uri', REDIRECT_URI);
  var basic = btoa(cid + ':' + csec);
  try {
    var r = await fetch('https://accounts.spotify.com/api/token', {
      method: 'POST',
      headers: { 'Authorization': 'Basic ' + basic,
                 'Content-Type': 'application/x-www-form-urlencoded' },
      body: body.toString()
    });
    var j = await r.json();
    if (j.refresh_token) { showResult(j.refresh_token); }
    else { showError(JSON.stringify(j, null, 2)); }
  } catch (e) { showError(String(e)); }
}

function showResult(rtok){
  document.getElementById('start').classList.add('hidden');
  var el = document.getElementById('result');
  el.classList.remove('hidden');
  el.innerHTML =
      '<h2>Success! Your refresh token:</h2>'
    + '<pre id="rtok"></pre>'
    + '<button id="copy">Copy</button>'
    + '<h3>Next steps</h3>'
    + '<p>Paste this refresh token into <code class="inline">presets.json</code> '
    + 'as <code class="inline">spotify_refresh_token</code>, then restart the server '
    + '(reconnect the monitor, or '
    + '<code class="inline">launchctl kickstart -k gui/$UID/com.volumepresets</code>).</p>'
    + '<p>The token stays on your Mac — nothing needs to be pushed to the Car Thing.</p>';
  document.getElementById('rtok').textContent = rtok;
  document.getElementById('copy').onclick = function(){
    navigator.clipboard.writeText(rtok);
    this.textContent = 'Copied!';
  };
  // Clear the ?code from the URL so a refresh doesn't retry a used code.
  history.replaceState(null, '', location.pathname);
}

function showError(msg){
  document.getElementById('start').classList.add('hidden');
  var el = document.getElementById('error');
  el.classList.remove('hidden');
  el.innerHTML = '<h2>Error</h2><pre></pre><p>Try again from '
    + '<a href="/auth" style="color:#1db954;">/auth</a>.</p>';
  el.querySelector('pre').textContent = msg;
}

var code = qs('code');
if (code){
  document.getElementById('start').classList.add('hidden');
  exchange(code);
}
</script>
</body>
</html>
"""


def _spotify_auth_page():
    html = (
        AUTH_PAGE_TEMPLATE
        .replace("__CLIENT_ID__", SPOTIFY_CLIENT_ID)
        .replace("__CLIENT_SECRET__", SPOTIFY_CLIENT_SECRET)
        .replace("__REDIRECT_URI__", SPOTIFY_REDIRECT_URI)
    )
    resp = make_response(html)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    return resp


@app.route("/auth", methods=["GET"])
def spotify_auth():
    return _spotify_auth_page()


@app.route("/", defaults={"path": "index.html"})
@app.route("/<path:path>")
def serve_webapp(path):
    # OAuth redirect lands on the root with ?code=...; complete the exchange on
    # the standalone auth page rather than the Car Thing webapp.
    if request.args.get("code"):
        return _spotify_auth_page()
    full_path = os.path.join(WEBAPP_DIR, path)
    if os.path.isfile(full_path):
        return send_from_directory(WEBAPP_DIR, path)
    return send_from_directory(WEBAPP_DIR, "index.html")


@app.route("/presets", methods=["GET"])
def get_presets():
    # Only expose what the webapp needs — never the HA token or Spotify secrets.
    return jsonify({
        "presets": PRESETS,
        "speakers": {key: {"name": info.get("name", key)} for key, info in SPEAKERS.items()},
        "spotify_configured": SPOTIFY_CONFIGURED,
    })


# Reused HTTPS connections, one per host.
#
# A fresh TLS handshake costs roughly 243ms of wall time and 23ms of CPU here —
# ARMv7 on a Pi 2 has no crypto acceleration — and the now-playing poll runs
# every few seconds. Measured against api.spotify.com: 391ms wall / 31.7ms CPU
# per call opening a new connection, versus 148ms / 8.3ms reusing one.
_HTTP_CONNS = {}
_HTTP_LOCKS = {}
_HTTP_POOL_LOCK = threading.Lock()


def _host_lock(host):
    with _HTTP_POOL_LOCK:
        lock = _HTTP_LOCKS.get(host)
        if lock is None:
            lock = _HTTP_LOCKS[host] = threading.Lock()
        return lock


def _https(host, method, path, headers=None, body=None, timeout=8):
    """Request over a kept-alive connection to `host` -> (status, headers, body).

    Retries once when a *reused* connection turns out to have been closed by the
    far end, which is ordinary for keep-alive. A brand-new connection that fails
    is a real error and is raised.
    """
    last = None
    with _host_lock(host):
        for _ in (0, 1):
            conn = _HTTP_CONNS.get(host)
            reused = conn is not None
            if conn is None:
                conn = http.client.HTTPSConnection(host, timeout=timeout)
                _HTTP_CONNS[host] = conn
            try:
                conn.request(method, path, body=body, headers=headers or {})
                resp = conn.getresponse()
                data = resp.read()
                return resp.status, dict(resp.getheaders()), data
            except Exception as e:
                last = e
                try:
                    conn.close()
                except Exception:
                    pass
                _HTTP_CONNS.pop(host, None)
                if not reused:
                    break
    raise last


# The Car Thing has no internet of its own (it reaches the Mac only over USB via
# adb reverse), so the Mac proxies every Spotify call. The access token is cached
# here and never leaves the Mac.
_TOKEN_CACHE = {"access_token": None, "expires_at": 0.0}


def _spotify_access_token():
    """Return a cached Spotify access token, refreshing it when near expiry."""
    now = time.time()
    if _TOKEN_CACHE["access_token"] and now < _TOKEN_CACHE["expires_at"] - 30:
        return _TOKEN_CACHE["access_token"]
    if not SPOTIFY_CONFIGURED:
        return None
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": SPOTIFY_REFRESH_TOKEN,
    }).encode("utf-8")
    basic = base64.b64encode(
        f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode("utf-8")
    ).decode("ascii")
    try:
        status, _, raw = _https(
            "accounts.spotify.com", "POST", "/api/token",
            headers={
                "Authorization": "Basic " + basic,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            body=data,
        )
        if status != 200:
            print(f"[spotify] token refresh failed: HTTP {status} "
                  f"{raw.decode('utf-8', errors='replace')[:200]}")
            return None
        j = json.loads(raw.decode("utf-8"))
        _TOKEN_CACHE["access_token"] = j["access_token"]
        _TOKEN_CACHE["expires_at"] = now + j.get("expires_in", 3600)
        return _TOKEN_CACHE["access_token"]
    except Exception as e:
        print(f"[spotify] token refresh error: {e}")
    return None


@app.route("/spotify/token", methods=["GET"])
def spotify_token():
    """Debug endpoint: confirm the server can mint an access token."""
    if not SPOTIFY_CONFIGURED:
        return jsonify({"ok": False, "error": "spotify not configured"}), 503
    token = _spotify_access_token()
    if not token:
        return jsonify({"ok": False, "error": "token refresh failed"}), 502
    return jsonify({"ok": True, "have_token": True})


@app.route("/spotify/now-playing", methods=["GET"])
def spotify_now_playing():
    """Proxy the user's currently-playing track. Returns the raw Spotify JSON
    (or {item: null, is_playing: false} when nothing is playing / unconfigured)."""
    if not SPOTIFY_CONFIGURED:
        return jsonify({"item": None, "is_playing": False})
    token = _spotify_access_token()
    if not token:
        return jsonify({"item": None, "is_playing": False, "error": "no token"}), 502
    try:
        status, _, raw = _https(
            "api.spotify.com", "GET", "/v1/me/player/currently-playing",
            headers={"Authorization": "Bearer " + token},
        )
        if status in (202, 204):
            return jsonify({"item": None, "is_playing": False})
        if status != 200:
            print(f"[spotify] now-playing failed: HTTP {status}")
            return jsonify({"item": None, "is_playing": False, "error": f"HTTP {status}"}), 502
        return app.response_class(raw.decode("utf-8"), mimetype="application/json")
    except Exception as e:
        print(f"[spotify] now-playing error: {e}")
        return jsonify({"item": None, "is_playing": False, "error": str(e)}), 502


# Album art cache. Each image is ~90 KB and costs ~109ms of server CPU to fetch
# and proxy; the Car Thing re-requests it whenever the track changes and albums
# repeat, so keeping the last few makes those free.
ART_HOST_SUFFIXES = (".scdn.co", ".spotifycdn.com")
_ART_CACHE = collections.OrderedDict()   # url -> (content_type, bytes)
_ART_CACHE_MAX = 48                      # album art plus a list of playlist covers
_ART_LOCK = threading.Lock()


@app.route("/spotify/art", methods=["GET"])
def spotify_art():
    """Proxy album art and playlist covers, since the Car Thing has no internet
    of its own.

    Restricted to Spotify's own CDNs so this can't be used as an open relay.
    Album art is on i.scdn.co; playlist covers are spread across mosaic.scdn.co
    (auto-generated four-square covers) and image-cdn-*.spotifycdn.com
    (user-uploaded ones), so both suffixes are needed."""
    url = request.args.get("u", "")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc.endswith(ART_HOST_SUFFIXES):
        return ("forbidden", 403)
    def _art_response(ctype, data, cached):
        out = make_response(data)
        out.headers["Content-Type"] = ctype
        out.headers["Cache-Control"] = "public, max-age=86400"
        out.headers["X-Art-Cache"] = "hit" if cached else "miss"
        return out

    with _ART_LOCK:
        hit = _ART_CACHE.get(url)
        if hit is not None:
            _ART_CACHE.move_to_end(url)
    if hit is not None:
        return _art_response(hit[0], hit[1], True)

    try:
        path = parsed.path + (("?" + parsed.query) if parsed.query else "")
        status, headers, data = _https(parsed.netloc, "GET", path)
        if status != 200:
            print(f"[spotify] art proxy: HTTP {status}")
            return ("", 502)
        ctype = headers.get("Content-Type", "image/jpeg")
        with _ART_LOCK:
            _ART_CACHE[url] = (ctype, data)
            _ART_CACHE.move_to_end(url)
            while len(_ART_CACHE) > _ART_CACHE_MAX:
                _ART_CACHE.popitem(last=False)
        return _art_response(ctype, data, False)
    except Exception as e:
        print(f"[spotify] art proxy error: {e}")
        return ("", 502)


def _spotify_command(http_method, endpoint):
    """Send a playback command to the Spotify Web API (Premium + the
    user-modify-playback-state scope are required)."""
    token = _spotify_access_token()
    if not token:
        return jsonify({"ok": False, "error": "spotify not configured"}), 503
    req = urllib.request.Request(
        "https://api.spotify.com/v1/me/player/" + endpoint,
        data=b"" if http_method in ("PUT", "POST") else None,
        method=http_method,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return jsonify({"ok": True, "status": resp.status})
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:200]
        if e.code == 403:
            msg = "needs Spotify Premium and re-auth (user-modify-playback-state)"
        elif e.code == 404:
            msg = "no active Spotify device"
        else:
            msg = f"HTTP {e.code}"
        print(f"[spotify] {http_method} {endpoint} -> {e.code} {detail}")
        return jsonify({"ok": False, "error": msg, "code": e.code}), 502
    except Exception as e:
        print(f"[spotify] {http_method} {endpoint} error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 502


@app.route("/spotify/play", methods=["POST"])
def spotify_play():
    return _spotify_command("PUT", "play")


@app.route("/spotify/pause", methods=["POST"])
def spotify_pause():
    return _spotify_command("PUT", "pause")


@app.route("/spotify/next", methods=["POST"])
def spotify_next():
    return _spotify_command("POST", "next")


@app.route("/spotify/previous", methods=["POST"])
def spotify_previous():
    return _spotify_command("POST", "previous")


def _spotify_fetch_devices(token):
    """Return the raw Spotify Connect devices array. Raises on transport error."""
    req = urllib.request.Request(
        "https://api.spotify.com/v1/me/player/devices",
        headers={"Authorization": "Bearer " + token},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("devices", [])


@app.route("/spotify/playlists", methods=["GET"])
def spotify_playlists():
    """Return the user's saved playlists as [{id, name, image_url}].

    `id` is the playlist URI (spotify:playlist:...) so it can be handed straight
    back to /spotify/play-playlist as a context_uri. Always live (not cached);
    the webapp does its own in-memory caching."""
    if not SPOTIFY_CONFIGURED:
        return jsonify({"error": "spotify not configured"}), 503
    token = _spotify_access_token()
    if not token:
        return jsonify({"error": "token refresh failed"}), 502
    req = urllib.request.Request(
        "https://api.spotify.com/v1/me/playlists?limit=50",
        headers={"Authorization": "Bearer " + token},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"[spotify] playlists failed: HTTP {e.code}")
        return jsonify({"error": f"HTTP {e.code}"}), 502
    except Exception as e:
        print(f"[spotify] playlists error: {e}")
        return jsonify({"error": str(e)}), 502
    out = []
    for it in data.get("items", []):
        if not it:
            continue  # Spotify occasionally returns null items for dead playlists
        # Prefer the smallest cover Spotify offers. Rows render it at 40px, and
        # most playlists expose 640/300/60 — proxying the 60px variant costs a
        # couple of KB instead of ~90. Mosaic covers report no dimensions at all,
        # in which case there is only one to take.
        images = [i for i in (it.get("images") or []) if i.get("url")]
        sized = [i for i in images if isinstance(i.get("width"), int)]
        pick = min(sized, key=lambda i: i["width"]) if sized else (images[-1] if images else None)
        out.append({
            "id": it.get("uri"),
            "name": it.get("name", ""),
            "image_url": pick.get("url") if pick else None,
        })
    return jsonify(out)


@app.route("/spotify/devices", methods=["GET"])
def spotify_devices():
    """Return the raw Spotify Connect devices array (for display / future use)."""
    if not SPOTIFY_CONFIGURED:
        return jsonify({"error": "spotify not configured"}), 503
    token = _spotify_access_token()
    if not token:
        return jsonify({"error": "token refresh failed"}), 502
    try:
        return jsonify(_spotify_fetch_devices(token))
    except urllib.error.HTTPError as e:
        print(f"[spotify] devices failed: HTTP {e.code}")
        return jsonify({"error": f"HTTP {e.code}"}), 502
    except Exception as e:
        print(f"[spotify] devices error: {e}")
        return jsonify({"error": str(e)}), 502


@app.route("/spotify/play-playlist", methods=["POST"])
def spotify_play_playlist():
    """Start a playlist on a Spotify Connect device (the Google speaker group).

    Body: {"playlist_uri": "spotify:playlist:...", "device_name": "..."}.
    `device_name` is optional; without it the targets come from
    `spotify_connect_device` in presets.json, which may be a single name or an
    ordered list of fallbacks. Names match case-insensitively on a substring of
    the Spotify-reported name; `spotify_connect_device_id` is tried first as an
    exact match in case a device was renamed.

    This is a *separate* endpoint from /spotify/play (which stays a plain
    resume-playback call) so no existing caller's behaviour changes."""
    if not SPOTIFY_CONFIGURED:
        return jsonify({"ok": False, "error": "spotify not configured"}), 503
    data = request.get_json(silent=True) or {}
    playlist_uri = data.get("playlist_uri")
    if not playlist_uri:
        return jsonify({"ok": False, "error": "missing playlist_uri"}), 400

    # Targets are tried in order. spotify_connect_device may be a single name or
    # a list, so a Cast group can be preferred with a real Spotify-app device
    # (a TV, a phone) behind it — a group only appears in Spotify's device list
    # while something is already casting to it, so on a cold start it is absent.
    targets = []
    if data.get("device_name"):
        targets.append(data["device_name"])
    cfg = SPOTIFY_CONNECT_DEVICE
    if isinstance(cfg, list):
        targets.extend([c for c in cfg if c])
    elif cfg:
        targets.append(cfg)
    if not targets:
        return jsonify({
            "ok": False,
            "error": "no target configured (set spotify_connect_device)",
        }), 400
    token = _spotify_access_token()
    if not token:
        return jsonify({"ok": False, "error": "token refresh failed"}), 502

    # 1) Find the target Connect device by (case-insensitive substring) name.
    try:
        devices = _spotify_fetch_devices(token)
    except urllib.error.HTTPError as e:
        print(f"[spotify] play-playlist devices lookup failed: HTTP {e.code}")
        return jsonify({"ok": False, "error": f"HTTP {e.code}"}), 502
    except Exception as e:
        print(f"[spotify] play-playlist devices lookup error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 502
    match = None
    # An exact id match first, in case a device was renamed.
    want_id = _config_value("spotify_connect_device_id")
    if want_id:
        match = next((d for d in devices if d.get("id") == want_id), None)
    for t in targets:
        if match:
            break
        needle = t.lower()
        match = next((d for d in devices if needle in (d.get("name") or "").lower()), None)
    # No configured target is available. Spotify can only start playback on a
    # device it already knows about: Cast groups register only while they are
    # casting, and Google speakers never register at all (they run no Spotify
    # client). So from cold there is nothing in the room to aim at, and the
    # honest fallback is whatever session is already active — which is what
    # "start or change what's playing" means in practice.
    if not match:
        match = next((d for d in devices if d.get("is_active")), None)
        if match:
            print(f"[spotify] play-playlist: {targets} unavailable — "
                  f"using the active session on {match.get('name')!r}")

    if not match:
        names = [d.get("name") for d in devices]
        print(f"[spotify] play-playlist: none of {targets} found, and no active "
              f"session among {names}")
        return jsonify({
            "ok": False,
            "error": "no active session",
            "tried": targets,
            "available": names,
        }), 404

    # 2) Start the playlist context on that device.
    device_id = match.get("id", "")
    url = ("https://api.spotify.com/v1/me/player/play?device_id="
           + urllib.parse.quote(device_id))
    body = json.dumps({"context_uri": playlist_uri}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="PUT",
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            print(f"[spotify] play-playlist {playlist_uri} on '{match.get('name')}' -> {resp.status}")
            return jsonify({"ok": True})
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:200]
        print(f"[spotify] play-playlist failed: HTTP {e.code} {detail}")
        return jsonify({"ok": False, "error": f"HTTP {e.code}", "detail": detail}), 502
    except Exception as e:
        print(f"[spotify] play-playlist error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 502


# One writer thread per speaker, each holding only the latest target.
#
# A Cast set_volume takes 100-1000ms (measured: nest med 105ms, home med 212ms,
# TV med 99ms but up to 1014ms). The old code fanned out and joined before
# replying, so /volume/adjust took ~1.2s — and with the webapp keeping one
# request in flight, that capped the dial at under one step per second.
#
# Now a request records the intended level and hands the target to the writer,
# which applies the most recent value it has seen. Turning the dial fast collapses
# to a single write per speaker instead of a queue that keeps landing after you
# stop, and the response no longer waits for the network at all.
VOL_TARGETS = {}                     # speaker key -> latest target, or None
VOL_WAKE = {key: threading.Event() for key in SPEAKERS}
VOL_TARGET_LOCK = threading.Lock()


def _volume_writer(speaker_key):
    """Apply the newest queued target for one speaker, forever."""
    while True:
        VOL_WAKE[speaker_key].wait()
        VOL_WAKE[speaker_key].clear()
        with VOL_TARGET_LOCK:
            volume = VOL_TARGETS.get(speaker_key)
            VOL_TARGETS[speaker_key] = None
        if volume is None:
            continue
        cast = CASTS.get(speaker_key)
        if cast is None:
            print(f"[{speaker_key}] not connected — skipping (target {volume:.2f})")
            continue
        try:
            with CAST_LOCKS[speaker_key]:
                cast.set_volume(volume)
            print(f"[{speaker_key}] -> {volume:.2f}")
        except Exception as e:
            print(f"[{speaker_key}] set_volume failed: {e}")


def _start_volume_writers():
    for key in SPEAKERS:
        threading.Thread(target=_volume_writer, args=(key,),
                         name=f"vol-{key}", daemon=True).start()


def _set_speaker_volume(speaker_key, volume):
    """Record an intended volume and hand it to that speaker's writer.

    Returns as soon as the target is queued. STATE holds the intended level so
    the mixer's ratio maths and the HTTP response stay consistent; the periodic
    refresh in _refresh_volumes_from_casts reconciles it with what the speakers
    actually did.
    """
    volume = float(volume)
    STATE["volumes"][speaker_key] = volume
    with VOL_TARGET_LOCK:
        VOL_TARGETS[speaker_key] = volume
    VOL_WAKE[speaker_key].set()
    return {"speaker": speaker_key, "ok": True, "queued": True}


def _dispatch_volumes(volume_map):
    """Queue a volume for every speaker in the map. Does not block on the
    network — see _volume_writer."""
    return [_set_speaker_volume(key, vol) for key, vol in volume_map.items()]


_volumes_synced_at = 0.0


def _refresh_volumes_from_casts(force=False):
    """Sync STATE["volumes"] from each speaker's real, current volume level.

    The server doesn't otherwise know the real speaker volumes (it only tracks
    what it has set), so after a restart it would start at 0 and the dial would
    yank everything down. pychromecast keeps each cast's status live in a
    background worker thread, so reading cast.status.volume_level is a cheap
    local lookup (no network call). Throttled to match the old behaviour so the
    webapp's frequent /status polls stay light."""
    global _volumes_synced_at
    now = time.time()
    if not force and (now - _volumes_synced_at) < 2.0:
        return
    _volumes_synced_at = now
    for key in SPEAKERS:
        cast = CASTS.get(key)
        if cast is None:
            continue  # unreachable — keep last known
        try:
            with CAST_LOCKS[key]:
                status = cast.status
            vol = status.volume_level if status else None
            if isinstance(vol, (int, float)):
                STATE["volumes"][key] = round(float(vol), 4)
        except Exception:
            pass  # status not ready yet / speaker unavailable — keep last known


@app.route("/preset/<preset_id>", methods=["POST"])
def activate_preset(preset_id):
    if preset_id not in PRESETS:
        return jsonify({"ok": False, "error": f"unknown preset {preset_id}"}), 404
    preset = PRESETS[preset_id]
    volume_map = {key: preset[key] for key in SPEAKERS if key in preset}
    print(f"\n=== Activating preset {preset_id} ({preset['name']}) ===")
    # Fan out to every speaker. A speaker that's offline fails silently inside
    # _set_speaker_volume (logged, not raised) — we still apply the preset and
    # report success so a single dead speaker never blacks out the mixer UI.
    _dispatch_volumes(volume_map)
    STATE["active_preset"] = preset_id
    return jsonify({"ok": True, "preset": preset["name"], "volumes": STATE["volumes"]})


@app.route("/preset/<preset_id>/save", methods=["POST"])
def save_preset(preset_id):
    """Overwrite a preset with the current live mix and persist it to disk."""
    if preset_id not in PRESETS:
        return jsonify({"ok": False, "error": f"unknown preset {preset_id}"}), 404
    for key in SPEAKERS:
        PRESETS[preset_id][key] = round(float(STATE["volumes"].get(key, 0.0)), 4)
    try:
        with open(PRESETS_PATH, "w") as f:
            json.dump(CONFIG, f, indent=2)  # CONFIG["presets"] is PRESETS
            f.write("\n")
    except Exception as e:
        print(f"[save_preset] write failed: {e}")
        return jsonify({"ok": False, "error": f"write failed: {e}"}), 500
    STATE["active_preset"] = preset_id
    levels = {key: PRESETS[preset_id][key] for key in SPEAKERS}
    print(f"\n=== Saved preset {preset_id} ({PRESETS[preset_id]['name']}) = {levels} ===")
    return jsonify({"ok": True, "preset": PRESETS[preset_id]["name"], "levels": levels})


# Serialises the read-modify-write in adjust_volume. Flask runs threaded, so
# without this two dial events arriving together both read the same starting
# volumes and compute conflicting targets.
_ADJUST_LOCK = threading.Lock()


def _speaker_unavailable(key):
    """Why a speaker can't be driven right now, or None if it can.

    Mirrors the checks in _set_speaker_volume so the webapp can grey a fader out
    instead of silently swallowing the dial.
    """
    cast = CASTS.get(key)
    if cast is None:
        return "offline"
    return None


def _unavailable_map():
    out = {}
    for key in SPEAKERS:
        reason = _speaker_unavailable(key)
        if reason:
            out[key] = reason
    return out


def _clamp01(x):
    return max(0.0, min(1.0, x))


@app.route("/volume/adjust", methods=["POST"])
def adjust_volume():
    """Adjust volume by `delta`.

    - With `speaker`: nudge that one speaker only (individual control).
    - Without `speaker`: scale ALL speakers proportionally (fixed ratio). The
      loudest speaker moves by `delta` and the rest scale to match, so the
      per-speaker ratio is preserved and nothing clips past 1.0.
    """
    data = request.get_json(silent=True) or {}
    delta = float(data.get("delta", 0.0))
    speaker = data.get("speaker")

    # A fader the server can't drive (offline, or a TV that isn't casting) would
    # silently swallow every turn: the write is skipped and STATE never moves, so
    # the target never advances and the dial appears dead. Say so instead.
    if speaker in SPEAKERS:
        reason = _speaker_unavailable(speaker)
        if reason:
            print(f"\n=== Volume adjust [{speaker}] delta={delta:+.3f} -> {reason} ===")
            return jsonify({
                "ok": False, "error": reason,
                "volumes": STATE["volumes"], "unavailable": _unavailable_map(),
            })

    with _ADJUST_LOCK:
        return _apply_adjust(delta, speaker)


def _apply_adjust(delta, speaker):
    """Compute and push the new mix. Caller must hold _ADJUST_LOCK."""
    vols = STATE["volumes"]

    # If the relevant level looks uninitialized (e.g. right after a restart),
    # pull the real value from the speakers first so we scale from it, not 0.
    target_zero = (vols.get(speaker, 0.0) <= 0.0) if speaker in SPEAKERS \
        else (max(vols.values()) if vols else 0.0) <= 0.0
    if target_zero:
        _refresh_volumes_from_casts(force=True)
        vols = STATE["volumes"]

    if speaker in SPEAKERS:
        new_volumes = {speaker: round(_clamp01(vols.get(speaker, 0.0) + delta), 4)}
        STATE["active_preset"] = None  # mix no longer matches a preset
        print(f"\n=== Volume adjust [{speaker}] delta={delta:+.3f} ===")
    else:
        cur_max = max(vols.values()) if vols else 0.0
        if cur_max <= 0.0:
            # From silence there's no ratio to keep — nudge everything uniformly.
            new_volumes = {key: round(_clamp01(vols.get(key, 0.0) + delta), 4) for key in SPEAKERS}
        else:
            new_max = _clamp01(cur_max + delta)
            factor = new_max / cur_max
            new_volumes = {key: round(vols.get(key, 0.0) * factor, 4) for key in SPEAKERS}
        print(f"\n=== Volume adjust (ratio) delta={delta:+.3f} ===")

    # Offline speakers fail silently in _set_speaker_volume; the intended levels
    # are still recorded in STATE, so the UI stays consistent and responsive.
    _dispatch_volumes(new_volumes)
    return jsonify({
        "ok": True,
        "volumes": STATE["volumes"],
        "unavailable": _unavailable_map(),
    })


@app.route("/ping", methods=["GET"])
def ping():
    """Liveness only — no body, no Cast reads.

    backlight_watch.sh on the Car Thing polls this every few seconds to decide
    whether to power the panel. It only cares whether the request succeeds, so
    serving it from /status meant a volume refresh and a JSON build for a caller
    that discards the body.
    """
    return ("", 204)


@app.route("/status", methods=["GET"])
def status():
    _refresh_volumes_from_casts()  # keep the UI showing the speakers' real levels
    return jsonify({
        "active_preset": STATE["active_preset"],
        "volumes": STATE["volumes"],
        "unavailable": _unavailable_map(),
    })


def main():
    print("=" * 60)
    print(" VOLUME PRESETS SERVER")
    print("=" * 60)
    print(f" URL:  http://{SERVER_HOST}:{SERVER_PORT}")
    print(f" Bind: 0.0.0.0:{SERVER_PORT}")
    if SPOTIFY_CONFIGURED:
        print(" Spotify:        configured (refresh token loaded)")
    else:
        print(f" Spotify:        NOT configured — visit http://127.0.0.1:{SERVER_PORT}/auth")
    print(" Connecting to speakers (by static IP, no discovery):")
    _init_casts()
    _start_volume_writers()
    print(" Speakers:")
    for key, info in SPEAKERS.items():
        state = "connected" if CASTS.get(key) is not None else "OFFLINE"
        print(f"   - {key:12s} {info['name']:14s} {info['ip_address']:18s} [{state}]")
    print(" Presets:")
    for pid, p in PRESETS.items():
        print(f"   [{pid}] {p['name']}")
    print("=" * 60)
    sys.stdout.flush()
    # threaded=True: the webapp polls /status while volume/preset requests run, so
    # requests can overlap. Per-speaker CAST_LOCKS guard each cast connection.
    app.run(host="0.0.0.0", port=SERVER_PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
