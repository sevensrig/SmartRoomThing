"""Test harness for server.py.

server.py reads its config at import time and talks to real Cast devices and
the Spotify API, so the suite points it at a throwaway config and swaps every
Cast connection for a FakeCast. Nothing here touches the network.
"""

import copy
import importlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEST_CONFIG = {
    "server_port": 5005,
    # Placeholders, like presets.example.json: Spotify reads as unconfigured.
    "spotify_client_id": "REPLACE_WITH_SPOTIFY_CLIENT_ID",
    "spotify_client_secret": "REPLACE_WITH_SPOTIFY_CLIENT_SECRET",
    "spotify_refresh_token": "REPLACE_WITH_SPOTIFY_REFRESH_TOKEN",
    "speakers": {
        "nest_audio": {"ip_address": "192.0.2.1", "name": "Nest Audio"},
        "google_home": {"ip_address": "192.0.2.2", "name": "Google Home"},
        "chromecast": {"ip_address": "192.0.2.3", "name": "TV"},
    },
    "presets": {
        "1": {"name": "DESK", "nest_audio": 0.6, "google_home": 0.5, "chromecast": 0.7},
        "2": {"name": "BED", "nest_audio": 0.35, "google_home": 0.3, "chromecast": 0.45},
    },
}


class FakeSocket:
    def __init__(self):
        self.is_connected = True


class FakeStatus:
    def __init__(self, volume_level):
        self.volume_level = volume_level


class FakeCast:
    """Stands in for pychromecast.Chromecast. `fail` makes the next N writes raise."""

    def __init__(self, volume=0.0, fail=0):
        self.writes = []
        self.fail = fail
        self.status = FakeStatus(volume)
        self.socket_client = FakeSocket()
        self.disconnected = False

    def set_volume(self, volume):
        if self.fail:
            self.fail -= 1
            raise OSError()  # dead pychromecast sockets raise with an empty message
        self.writes.append(volume)
        self.status.volume_level = volume

    def disconnect(self):
        self.disconnected = True


@pytest.fixture(scope="session")
def server_module(tmp_path_factory):
    config_path = tmp_path_factory.mktemp("config") / "presets.json"
    config_path.write_text(json.dumps(TEST_CONFIG))
    mp = pytest.MonkeyPatch()
    mp.setenv("VOLUMEPRESETS_CONFIG", str(config_path))
    module = importlib.import_module("server")
    yield module
    mp.undo()


@pytest.fixture
def server(server_module, monkeypatch):
    """server.py with fresh state and a FakeCast per speaker."""
    s = server_module
    monkeypatch.setattr(s, "CASTS", {key: FakeCast() for key in s.SPEAKERS})
    monkeypatch.setitem(s.STATE, "volumes", {key: 0.0 for key in s.SPEAKERS})
    monkeypatch.setitem(s.STATE, "active_preset", None)
    monkeypatch.setattr(s, "WRITE_ERRORS", {key: None for key in s.SPEAKERS})
    monkeypatch.setattr(s, "VOL_TARGETS", {})
    monkeypatch.setattr(s, "_volumes_synced_at", 0.0)
    # Tests must never dial a real device.
    monkeypatch.setattr(s, "_connect_speaker", lambda key: None)
    # save_preset mutates PRESETS in place; restore it afterwards.
    saved = copy.deepcopy(s.PRESETS)
    yield s
    s.PRESETS.clear()
    s.PRESETS.update(saved)


@pytest.fixture
def client(server):
    return server.app.test_client()


def set_volumes(server, **levels):
    """Set both the server's view and the fake speakers' reported level."""
    for key, vol in levels.items():
        server.STATE["volumes"][key] = vol
        server.CASTS[key].status.volume_level = vol
