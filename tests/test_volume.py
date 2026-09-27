import pytest
from conftest import FakeCast, set_volumes


def adjust(client, **body):
    r = client.post("/volume/adjust", json=body)
    assert r.status_code == 200
    return r.get_json()


def test_all_speakers_scale_together_keeping_their_ratio(server, client):
    set_volumes(server, nest_audio=0.4, google_home=0.2, chromecast=0.1)
    out = adjust(client, delta=0.1)
    # The loudest speaker moves by delta; the others keep their ratio to it.
    assert out["volumes"] == pytest.approx({"nest_audio": 0.5, "google_home": 0.25, "chromecast": 0.125})


def test_scaling_up_never_pushes_a_speaker_past_full(server, client):
    set_volumes(server, nest_audio=0.9, google_home=0.45, chromecast=0.3)
    out = adjust(client, delta=0.2)
    assert out["volumes"]["nest_audio"] == 1.0
    assert max(out["volumes"].values()) <= 1.0
    assert out["volumes"]["google_home"] == pytest.approx(0.5)


def test_scaling_down_bottoms_out_at_zero(server, client):
    set_volumes(server, nest_audio=0.1, google_home=0.05, chromecast=0.02)
    out = adjust(client, delta=-0.5)
    assert out["volumes"] == {"nest_audio": 0.0, "google_home": 0.0, "chromecast": 0.0}


def test_from_silence_every_speaker_steps_up_evenly(server, client):
    out = adjust(client, delta=0.05)
    assert out["volumes"] == {"nest_audio": 0.05, "google_home": 0.05, "chromecast": 0.05}


def test_single_speaker_adjust_leaves_the_rest_alone(server, client):
    set_volumes(server, nest_audio=0.4, google_home=0.2, chromecast=0.1)
    server.STATE["active_preset"] = "1"
    out = adjust(client, delta=0.1, speaker="google_home")
    assert out["volumes"] == pytest.approx({"nest_audio": 0.4, "google_home": 0.3, "chromecast": 0.1})
    assert server.STATE["active_preset"] is None  # the mix no longer matches a preset


def test_adjust_queues_the_target_for_the_writer(server, client):
    set_volumes(server, nest_audio=0.4, google_home=0.2, chromecast=0.1)
    adjust(client, delta=-0.2)
    assert server.VOL_TARGETS == pytest.approx({"nest_audio": 0.2, "google_home": 0.1, "chromecast": 0.05})


def test_after_a_restart_the_dial_starts_from_the_speakers_real_levels(server, client):
    # STATE is all zeros, as it is right after the service starts, but the
    # speakers are actually playing. The dial must not yank them to 0.05.
    for key, vol in {"nest_audio": 0.4, "google_home": 0.2, "chromecast": 0.1}.items():
        server.CASTS[key].status.volume_level = vol
    out = adjust(client, delta=0.1)
    assert out["volumes"]["nest_audio"] == pytest.approx(0.5)


def test_offline_speaker_is_reported_instead_of_silently_ignored(server, client):
    set_volumes(server, nest_audio=0.4, google_home=0.2, chromecast=0.1)
    server.CASTS["chromecast"] = None
    out = adjust(client, delta=0.1, speaker="chromecast")
    assert out["ok"] is False
    assert out["error"] == "offline"
    assert server.STATE["volumes"]["chromecast"] == 0.1


def test_dropped_socket_shows_offline_before_any_write_fails(server, client):
    server.CASTS["nest_audio"].socket_client.is_connected = False
    assert client.get("/status").get_json()["unavailable"] == {"nest_audio": "offline"}


def test_failed_write_is_surfaced_through_status(server, client):
    server._note_write_error("google_home", "write failed")
    assert client.get("/status").get_json()["unavailable"] == {"google_home": "write failed"}


# --- the writer: reconnect instead of failing silently -----------------------

def test_write_goes_to_the_speaker(server):
    server._write_volume("nest_audio", 0.3)
    assert server.CASTS["nest_audio"].writes == [0.3]
    assert server.WRITE_ERRORS["nest_audio"] is None


def test_dead_connection_is_reopened_and_the_write_retried(server, monkeypatch):
    dead = FakeCast(fail=1)
    fresh = FakeCast()
    server.CASTS["nest_audio"] = dead
    monkeypatch.setattr(server, "_connect_speaker", lambda key: fresh)

    server._write_volume("nest_audio", 0.3)

    assert fresh.writes == [0.3]
    assert server.CASTS["nest_audio"] is fresh
    assert server.WRITE_ERRORS["nest_audio"] is None  # recovered, so no error shown


def test_gives_up_after_one_retry_and_reports_it(server, monkeypatch):
    server.CASTS["nest_audio"] = FakeCast(fail=1)
    monkeypatch.setattr(server, "_connect_speaker", lambda key: FakeCast(fail=1))
    server._write_volume("nest_audio", 0.3)
    assert server.WRITE_ERRORS["nest_audio"] == "write failed"


def test_unreachable_speaker_is_marked_offline(server):
    server.CASTS["nest_audio"] = None  # and _connect_speaker returns None
    server._write_volume("nest_audio", 0.3)
    assert server.WRITE_ERRORS["nest_audio"] == "offline"
