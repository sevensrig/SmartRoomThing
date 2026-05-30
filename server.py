#!/usr/bin/env python3
"""Volume presets server for Google Home speakers, controlled by a Car Thing.

Speaker volume commands are routed through the Home Assistant REST API rather
than the Google Home Local API. Home Assistant maintains its own persistent
connection to the speakers and accepts a long-lived token that never expires,
which avoids the 403 / 24h token churn of calling the speakers directly.
"""

import base64
import json
import os
import time
import sys
import urllib.request
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
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
HA_URL = CONFIG["ha_url"].rstrip("/")
HA_TOKEN = CONFIG["ha_token"]


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
# Spotify only grants the http (non-https) loopback exception to 127.0.0.1, and
# the redirect URI must match the dashboard exactly. Keep it at the root path.
SPOTIFY_REDIRECT_URI = f"http://127.0.0.1:{SERVER_PORT}/"

STATE = {
    "active_preset": None,
    "volumes": {key: 0.0 for key in SPEAKERS},
}

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
var SCOPE = 'user-read-playback-state user-read-currently-playing user-modify-playback-state';

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
    req = urllib.request.Request(
        "https://accounts.spotify.com/api/token",
        data=data,
        headers={
            "Authorization": "Basic " + basic,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            j = json.loads(resp.read().decode("utf-8"))
        _TOKEN_CACHE["access_token"] = j["access_token"]
        _TOKEN_CACHE["expires_at"] = now + j.get("expires_in", 3600)
        return _TOKEN_CACHE["access_token"]
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:200]
        print(f"[spotify] token refresh failed: HTTP {e.code} {detail}")
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
    req = urllib.request.Request(
        "https://api.spotify.com/v1/me/player/currently-playing",
        headers={"Authorization": "Bearer " + token},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            if resp.status in (202, 204):
                return jsonify({"item": None, "is_playing": False})
            body = resp.read().decode("utf-8")
        return app.response_class(body, mimetype="application/json")
    except urllib.error.HTTPError as e:
        print(f"[spotify] now-playing failed: HTTP {e.code}")
        return jsonify({"item": None, "is_playing": False, "error": f"HTTP {e.code}"}), 502
    except Exception as e:
        print(f"[spotify] now-playing error: {e}")
        return jsonify({"item": None, "is_playing": False, "error": str(e)}), 502


@app.route("/spotify/art", methods=["GET"])
def spotify_art():
    """Proxy album art (Spotify CDN), since the Car Thing has no internet to
    load https://i.scdn.co images directly. Only Spotify CDN hosts are allowed."""
    url = request.args.get("u", "")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc.endswith(".scdn.co"):
        return ("forbidden", 403)
    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            data = resp.read()
            ctype = resp.headers.get("Content-Type", "image/jpeg")
        out = make_response(data)
        out.headers["Content-Type"] = ctype
        out.headers["Cache-Control"] = "public, max-age=86400"
        return out
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


def _set_speaker_volume(speaker_key, volume):
    speaker = SPEAKERS[speaker_key]
    entity_id = speaker["entity_id"]
    url = f"{HA_URL}/api/services/media_player/volume_set"
    payload = json.dumps(
        {"entity_id": entity_id, "volume_level": float(volume)}
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {HA_TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            status = resp.status
            body = resp.read().decode("utf-8", errors="replace")
            print(f"[{speaker_key}] {entity_id} -> {volume:.2f} | HTTP {status} {body[:80]}")
            STATE["volumes"][speaker_key] = float(volume)
            return {"speaker": speaker_key, "ok": True, "status": status}
    except urllib.error.HTTPError as e:
        print(f"[{speaker_key}] {entity_id} HTTPError {e.code}: {e.reason}")
        return {"speaker": speaker_key, "ok": False, "error": f"HTTP {e.code} {e.reason}"}
    except urllib.error.URLError as e:
        print(f"[{speaker_key}] {entity_id} URLError: {e.reason}")
        return {"speaker": speaker_key, "ok": False, "error": str(e.reason)}
    except Exception as e:
        print(f"[{speaker_key}] {entity_id} Error: {e}")
        return {"speaker": speaker_key, "ok": False, "error": str(e)}


def _dispatch_volumes(volume_map):
    with ThreadPoolExecutor(max_workers=len(volume_map)) as ex:
        futures = {
            ex.submit(_set_speaker_volume, key, vol): key
            for key, vol in volume_map.items()
        }
        return [f.result() for f in futures]


_volumes_synced_at = 0.0


def _refresh_volumes_from_ha(force=False):
    """Pull each speaker's current volume_level from Home Assistant into STATE.

    The server doesn't otherwise know the real speaker volumes (it only tracks
    what it has set), so after a restart it would start at 0 and the dial would
    yank everything down. This keeps STATE in sync with reality. Throttled so the
    webapp's frequent /status polls don't hammer HA."""
    global _volumes_synced_at
    now = time.time()
    if not force and (now - _volumes_synced_at) < 2.0:
        return
    _volumes_synced_at = now
    for key, info in SPEAKERS.items():
        entity_id = info["entity_id"]
        req = urllib.request.Request(
            f"{HA_URL}/api/states/{entity_id}",
            headers={"Authorization": f"Bearer {HA_TOKEN}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            vol = data.get("attributes", {}).get("volume_level")
            if isinstance(vol, (int, float)):
                STATE["volumes"][key] = round(float(vol), 4)
        except Exception:
            pass  # HA not ready yet / speaker unavailable — keep last known


@app.route("/preset/<preset_id>", methods=["POST"])
def activate_preset(preset_id):
    if preset_id not in PRESETS:
        return jsonify({"ok": False, "error": f"unknown preset {preset_id}"}), 404
    preset = PRESETS[preset_id]
    volume_map = {key: preset[key] for key in SPEAKERS if key in preset}
    print(f"\n=== Activating preset {preset_id} ({preset['name']}) ===")
    results = _dispatch_volumes(volume_map)
    failed = [r for r in results if not r["ok"]]
    if failed:
        return jsonify({"ok": False, "error": failed[0]["error"], "results": results}), 500
    STATE["active_preset"] = preset_id
    return jsonify({"ok": True, "preset": preset["name"], "volumes": STATE["volumes"]})


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
    vols = STATE["volumes"]

    # If the relevant level looks uninitialized (e.g. right after a restart),
    # pull the real value from HA first so we scale from it instead of from 0.
    target_zero = (vols.get(speaker, 0.0) <= 0.0) if speaker in SPEAKERS \
        else (max(vols.values()) if vols else 0.0) <= 0.0
    if target_zero:
        _refresh_volumes_from_ha(force=True)
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

    results = _dispatch_volumes(new_volumes)
    failed = [r for r in results if not r["ok"]]
    if failed:
        return jsonify({"ok": False, "error": failed[0]["error"], "results": results}), 500
    return jsonify({"ok": True, "volumes": STATE["volumes"]})


@app.route("/status", methods=["GET"])
def status():
    _refresh_volumes_from_ha()  # keep the UI showing the speakers' real levels
    return jsonify({
        "active_preset": STATE["active_preset"],
        "volumes": STATE["volumes"],
    })


def main():
    print("=" * 60)
    print(" VOLUME PRESETS SERVER")
    print("=" * 60)
    print(f" URL:  http://{SERVER_HOST}:{SERVER_PORT}")
    print(f" Bind: 0.0.0.0:{SERVER_PORT}")
    print(f" Home Assistant: {HA_URL}")
    if SPOTIFY_CONFIGURED:
        print(" Spotify:        configured (refresh token loaded)")
    else:
        print(f" Spotify:        NOT configured — visit http://127.0.0.1:{SERVER_PORT}/auth on this Mac")
    print(" Speakers:")
    for key, info in SPEAKERS.items():
        print(f"   - {key:12s} {info['name']:14s} {info['entity_id']}")
    print(" Presets:")
    for pid, p in PRESETS.items():
        print(f"   [{pid}] {p['name']}")
    print("=" * 60)
    sys.stdout.flush()
    app.run(host="0.0.0.0", port=SERVER_PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
