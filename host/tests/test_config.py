from datetime import time

import pytest
from pydantic import ValidationError

from host.core.clock import ManualClock
from host.core.config import ActiveHours, Config, ThresholdRule


def test_defaults_match_spec_table():
    d = Config().drives
    assert (d.battery_low.enter, d.battery_low.enter_hold_s) == (20, 10)
    assert (d.battery_low.exit, d.battery_low.exit_hold_s) == (25, 30)
    assert (d.battery_critical.enter, d.battery_critical.exit) == (10, 15)
    assert (d.temp_high.enter, d.temp_high.enter_hold_s) == (40, 30)
    assert (d.temp_high.exit, d.temp_high.exit_hold_s) == (37, 60)
    assert d.long_silence_after_s == 4 * 3600
    assert Config().link.timeout_s == 5 and Config().link.recover_s == 5


def test_load_toml_overrides_and_keeps_defaults(tmp_path):
    p = tmp_path / "iris.toml"
    p.write_text(
        '[drives.battery_low]\n'
        'direction = "below"\nenter = 30\nexit = 35\nenter_hold_s = 1\nexit_hold_s = 2\n'
        '[drives.active_hours]\nstart = "22:00"\nend = "06:00"\n'
        '[initiative]\nmax_per_hour = 5\n'
    )
    cfg = Config.load(p)
    assert cfg.drives.battery_low.enter == 30
    assert cfg.drives.active_hours.start == time(22, 0)
    assert cfg.initiative.max_per_hour == 5
    assert cfg.drives.temp_high.enter == 40


def test_unknown_key_is_rejected(tmp_path):
    p = tmp_path / "iris.toml"
    p.write_text("[link]\ntimeout = 3\n")
    with pytest.raises(ValidationError):
        Config.load(p)


def test_threshold_exit_must_be_on_return_side():
    with pytest.raises(ValidationError):
        ThresholdRule(direction="below", enter=20, exit=15, enter_hold_s=1, exit_hold_s=1)
    with pytest.raises(ValidationError):
        ThresholdRule(direction="above", enter=40, exit=45, enter_hold_s=1, exit_hold_s=1)


@pytest.mark.parametrize(
    "start, end, t, expected",
    [
        (time(8), time(22), time(8, 0), True),
        (time(8), time(22), time(21, 59), True),
        (time(8), time(22), time(22, 0), False),
        (time(8), time(22), time(3, 0), False),
        (time(22), time(6), time(23, 0), True),
        (time(22), time(6), time(5, 59), True),
        (time(22), time(6), time(12, 0), False),
    ],
)
def test_active_hours(start, end, t, expected):
    assert ActiveHours(start=start, end=end).contains(t) is expected


def test_active_hours_rejects_equal_bounds():
    with pytest.raises(ValidationError):
        ActiveHours(start=time(8), end=time(8))


def test_manual_clock_moves_both_clocks():
    c = ManualClock()
    w0, m0 = c.wall(), c.monotonic()
    c.advance(90)
    assert c.monotonic() - m0 == 90
    assert (c.wall() - w0).total_seconds() == 90


def test_store_path_default_and_override():
    assert Config().store.path == "iris.db"
    assert Config.model_validate({"store": {"path": "/var/lib/iris.db"}}).store.path == "/var/lib/iris.db"


def test_api_defaults_match_spec():
    a = Config().api
    assert (a.tick_interval_s, a.touch_every_s, a.snapshot_every_s) == (1.0, 10.0, 300.0)
    assert a.store_heartbeats is False and a.ack_timeout_s == 2.0


def test_voice_defaults_match_spec():
    b = Config().brain
    assert (b.stt.engine, b.tts.engine) == ("none", "none")
    assert (b.stt.language, b.stt.min_text_chars, b.stt.vad_filter) == ("pl", 2, True)
    a = b.audio
    assert (a.playback_rate_hz, a.frame_ms, a.bitrate_bps) == (16000, 20.0, 24000)
    assert (a.max_utterance_s, a.min_utterance_ms, a.max_speech_s) == (30.0, 300.0, 15.0)


def test_voice_sections_from_toml(tmp_path):
    p = tmp_path / "iris.toml"
    p.write_text(
        '[brain.stt]\nengine = "faster-whisper"\nmodel = "Systran/faster-whisper-small"\nbeam_size = 3\n'
        '[brain.tts]\nengine = "piper"\nvoice = "/g/pl_PL-gosia-medium.onnx"\nlength_scale = 1.1\n'
        '[brain.audio]\nmax_speech_s = 8\nplayback_rate_hz = 24000\n'
    )
    b = Config.load(p).brain
    assert (b.stt.model, b.stt.beam_size) == ("Systran/faster-whisper-small", 3)
    assert (b.tts.voice, b.tts.length_scale) == ("/g/pl_PL-gosia-medium.onnx", 1.1)
    assert (b.audio.max_speech_s, b.audio.playback_rate_hz, b.audio.frame_ms) == (8.0, 24000, 20.0)


@pytest.mark.parametrize(
    "section, bad",
    [
        ("audio", {"playback_rate_hz": 22050}),  # Opus nie obsługuje 22,05 kHz
        ("audio", {"frame_ms": 30}),
        ("audio", {"bitrate_bps": 100}),
        ("audio", {"max_speech_s": 0}),
        ("stt", {"beam_size": 0}),
        ("tts", {"length_scale": 0}),
        ("tts", {"max_chars": 0}),
    ],
)
def test_invalid_voice_settings_are_rejected(section, bad):
    with pytest.raises(ValidationError):
        Config.model_validate({"brain": {section: bad}})


def test_importing_brain_does_not_load_audio_libraries():
    import subprocess
    import sys
    from pathlib import Path

    code = (
        "import sys\n"
        "import host.brain\n"
        "loaded = sorted(m for m in ('numpy', 'av', 'faster_whisper', 'piper', 'host.brain.audio') if m in sys.modules)\n"
        "assert not loaded, loaded\n"
    )
    root = Path(__file__).parents[2]
    result = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_voice_wiring_defaults_and_validation():
    a = Config().brain.audio
    assert (a.max_pending_turns, a.error_clip) == (3, None)
    with pytest.raises(ValidationError, match="razem"):
        Config.model_validate({"brain": {"stt": {"engine": "faster-whisper", "model": "small"}}})
    with pytest.raises(ValidationError, match="razem"):
        Config.model_validate({"brain": {"tts": {"engine": "piper", "voice": "/g.onnx"}}})
    both = {
        "stt": {"engine": "faster-whisper", "model": "small"},
        "tts": {"engine": "piper", "voice": "/g.onnx"},
        "audio": {"error_clip": "error", "max_pending_turns": 1},
    }
    b = Config.model_validate({"brain": both}).brain
    assert (b.audio.error_clip, b.audio.max_pending_turns) == ("error", 1)
    with pytest.raises(ValidationError):
        Config.model_validate({"brain": {"audio": {"max_pending_turns": 0}}})
