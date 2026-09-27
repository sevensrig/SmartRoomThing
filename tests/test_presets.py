import json

import pytest
from conftest import set_volumes


def test_activating_a_preset_sets_every_speaker(server, client):
    r = client.post("/preset/1")
    assert r.status_code == 200
    assert r.get_json()["preset"] == "DESK"
    assert server.STATE["volumes"] == {"nest_audio": 0.6, "google_home": 0.5, "chromecast": 0.7}
    assert server.VOL_TARGETS == {"nest_audio": 0.6, "google_home": 0.5, "chromecast": 0.7}
    assert server.STATE["active_preset"] == "1"


def test_unknown_preset_is_a_404(client):
    assert client.post("/preset/9").status_code == 404
    assert client.post("/preset/9/save").status_code == 404


def test_saving_writes_the_live_mix_to_the_config_file(server, client):
    set_volumes(server, nest_audio=0.12, google_home=0.34, chromecast=0.56)
    r = client.post("/preset/2/save")
    assert r.status_code == 200

    on_disk = json.loads(open(server.PRESETS_PATH).read())
    assert on_disk["presets"]["2"] == {
        "name": "BED", "nest_audio": 0.12, "google_home": 0.34, "chromecast": 0.56,
    }
    # The rest of the config survives the rewrite.
    assert on_disk["speakers"] == server.SPEAKERS


def test_presets_endpoint_never_exposes_secrets(client):
    body = client.get("/presets").get_data(as_text=True)
    assert "spotify_client_secret" not in body
    assert "refresh_token" not in body
    assert "ip_address" not in body


@pytest.mark.parametrize("value,expected", [
    ("REPLACE_WITH_SOMETHING", ""),
    ("real-value", "real-value"),
])
def test_config_placeholders_read_as_unset(server, monkeypatch, value, expected):
    monkeypatch.setitem(server.CONFIG, "spotify_client_id", value)
    assert server._config_value("spotify_client_id") == expected
