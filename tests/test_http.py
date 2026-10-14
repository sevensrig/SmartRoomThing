def test_ping_is_an_empty_204(client):
    r = client.get("/ping")
    assert r.status_code == 204
    assert r.data == b""


def test_responses_allow_the_car_thing_origin(client):
    # The webapp loads from file:// on the device, so every call is cross-origin.
    assert client.get("/ping").headers["Access-Control-Allow-Origin"] == "*"


def test_root_serves_the_webapp(client):
    r = client.get("/")
    assert r.status_code == 200
    assert b"<title>Volume Presets</title>" in r.data


def test_the_example_config_is_one_the_server_can_start_from():
    # presets.example.json is what a fresh setup copies; it must stay loadable.
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    env = dict(os.environ, VOLUMEPRESETS_CONFIG=str(root / "presets.example.json"))
    probe = "import server; assert set(server.SPEAKERS) and set(server.PRESETS); assert not server.SPOTIFY_CONFIGURED"
    subprocess.run([sys.executable, "-c", probe], cwd=root, env=env, check=True, timeout=30)
