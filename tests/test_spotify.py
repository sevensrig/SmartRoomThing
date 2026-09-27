import io
import urllib.error

import pytest


class FakeResponse:
    status = 204

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def spotify(server, monkeypatch):
    """Capture Spotify playback commands instead of sending them."""
    sent = []
    monkeypatch.setattr(server, "_spotify_access_token", lambda: "test-token")

    def fake_urlopen(req, timeout=None):
        sent.append(req)
        return FakeResponse()

    monkeypatch.setattr(server.urllib.request, "urlopen", fake_urlopen)
    return sent


@pytest.mark.parametrize("route,method,endpoint", [
    ("/spotify/play", "PUT", "play"),
    ("/spotify/pause", "PUT", "pause"),
    ("/spotify/next", "POST", "next"),
    ("/spotify/previous", "POST", "previous"),
    # Back button, past the first few seconds: seek to the start, don't skip.
    ("/spotify/restart", "PUT", "seek?position_ms=0"),
])
def test_playback_routes_send_the_right_command(client, spotify, route, method, endpoint):
    r = client.post(route)
    assert r.get_json() == {"ok": True, "status": 204}
    (req,) = spotify
    assert req.get_method() == method
    assert req.full_url == "https://api.spotify.com/v1/me/player/" + endpoint
    assert req.get_header("Authorization") == "Bearer test-token"


def test_no_active_device_gets_a_readable_error(server, client, monkeypatch):
    monkeypatch.setattr(server, "_spotify_access_token", lambda: "test-token")

    def not_found(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(b"{}"))

    monkeypatch.setattr(server.urllib.request, "urlopen", not_found)
    r = client.post("/spotify/restart")
    assert r.status_code == 502
    assert r.get_json()["error"] == "no active Spotify device"


def test_commands_without_spotify_configured_fail_cleanly(client):
    r = client.post("/spotify/restart")
    assert r.status_code == 503


def test_now_playing_without_spotify_configured_is_empty(client):
    assert client.get("/spotify/now-playing").get_json() == {"item": None, "is_playing": False}


@pytest.mark.parametrize("url", [
    "http://i.scdn.co/image/abc",          # not https
    "https://evil.example/image.jpg",      # not a Spotify CDN
    "https://evil.example/?x=.scdn.co",    # CDN name only in the query
    "https://scdn.co.evil.example/a.jpg",  # CDN name as a prefix
])
def test_art_proxy_is_not_an_open_relay(client, url):
    assert client.get("/spotify/art", query_string={"u": url}).status_code == 403
