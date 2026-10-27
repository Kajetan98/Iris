"""Rdzeń firmware (C) kontra host i symulator: te same ramki, te same odpowiedzi.

Biblioteka jest budowana z firmware/ (make lib) do katalogu tymczasowego i wołana przez ctypes.
"""

import ctypes
import json
import random
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from host.core.clock import ManualClock
from host.core.core import HostCore
from host.core.protocol import (
    AckBody,
    AudioUtteranceEnd,
    AudioUtteranceStart,
    BodyFault,
    BodyFaultClear,
    BodyHeartbeat,
    BodySensor,
    Button,
    DecodedAck,
    DecodedEvent,
    LocalCommand,
    SystemBoot,
    decode_audio_frame,
    decode_frame,
    encode_action,
)
from sim.model import BodyModel, SimConfig

FIRMWARE = Path(__file__).parents[2] / "firmware"
SENSORS = {"mic": 0, "imu": 1, "temp": 2, "presence": 3, "gps": 4}
PINS = (5, 6, 7)

needs_cc = pytest.mark.skipif(not (shutil.which("cc") and shutil.which("make")), reason="brak kompilatora C lub make")


@pytest.fixture(scope="session")
def fwlib(tmp_path_factory):
    if not (shutil.which("cc") and shutil.which("make")):
        pytest.skip("brak kompilatora C lub make")
    build = tmp_path_factory.mktemp("fwbuild")
    subprocess.run(["make", "-C", str(FIRMWARE), "lib", f"BUILD={build}"], check=True, capture_output=True)
    lib = ctypes.CDLL(str(build / "libfwcore.so"))
    u32, i32, sz, cstr, bytes_p = ctypes.c_uint32, ctypes.c_int32, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_char_p
    sig = {
        "ffi_init": ([u32, u32, cstr, bytes_p, ctypes.c_int], None),
        "ffi_link": ([ctypes.c_int], None),
        "ffi_transport_ok": ([ctypes.c_int], None),
        "ffi_tick": ([u32], None),
        "ffi_on_text": ([bytes_p, sz, u32], None),
        "ffi_on_binary": ([bytes_p, sz], None),
        "ffi_playback_done": ([], None),
        "ffi_set_battery": ([i32], None),
        "ffi_set_rssi": ([ctypes.c_int16], None),
        "ffi_set_temp": ([i32], None),
        "ffi_set_presence": ([ctypes.c_int, u32], None),
        "ffi_gps": ([i32, i32, u32], None),
        "ffi_fault": ([ctypes.c_int, cstr, u32], None),
        "ffi_fault_clear": ([ctypes.c_int, u32], None),
        "ffi_button": ([cstr, ctypes.c_int, u32], None),
        "ffi_local_command": ([cstr, cstr, u32], None),
        "ffi_utterance_start": ([u32], u32),
        "ffi_mic_packet": ([bytes_p, sz], None),
        "ffi_utterance_end": ([u32, u32], None),
        "ffi_out_count": ([], ctypes.c_int),
        "ffi_out_binary": ([ctypes.c_int], ctypes.c_int),
        "ffi_out_len": ([ctypes.c_int], sz),
        "ffi_out_data": ([ctypes.c_int], ctypes.c_void_p),
        "ffi_out_clear": ([], None),
        "ffi_hal_log": ([], cstr),
        "ffi_counter": ([cstr], u32),
        "ffi_setting": ([cstr], i32),
        "ffi_playback_packet": ([ctypes.c_uint16, ctypes.POINTER(ctypes.c_void_p)], sz),
        "ffi_sizeof_body": ([], sz),
    }
    for name, (args, res) in sig.items():
        fn = getattr(lib, name)
        fn.argtypes, fn.restype = args, res
    return lib


class Firmware:
    def __init__(self, lib):
        self.lib = lib
        self.lib.ffi_init(1000, 0xA1B2C3D4, b"power", bytes(PINS), len(PINS))
        self.lib.ffi_set_battery(715)
        self.lib.ffi_set_rssi(-61)

    def link(self, up=True):
        self.lib.ffi_link(1 if up else 0)

    def outputs(self):
        out = []
        for i in range(self.lib.ffi_out_count()):
            data = ctypes.string_at(self.lib.ffi_out_data(i), self.lib.ffi_out_len(i))
            out.append((bool(self.lib.ffi_out_binary(i)), data))
        return out

    def texts(self):
        return [d.decode("utf-8") for binary, d in self.outputs() if not binary]

    def clear(self):
        self.lib.ffi_out_clear()

    def counter(self, name):
        return self.lib.ffi_counter(name.encode())

    def send(self, frame: str | bytes, now=2000):
        data = frame.encode("utf-8") if isinstance(frame, str) else frame
        self.lib.ffi_on_text(data, len(data), now)

    def acks(self):
        return [decode_frame(t) for t in self.texts() if json.loads(t)["t"] == "ack"]

    def hal_log(self):
        return self.lib.ffi_hal_log().decode()


@pytest.fixture
def fw(fwlib):
    return Firmware(fwlib)


# --- testy C w tym samym przebiegu pytest ---


@needs_cc
def test_c_unit_tests_pass_under_sanitizers(tmp_path):
    r = subprocess.run(["make", "-C", str(FIRMWARE), "test", f"BUILD={tmp_path}"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "0 błędów" in r.stdout


@pytest.mark.skipif(not shutil.which("valgrind"), reason="brak valgrinda")
@needs_cc
def test_c_unit_tests_pass_under_valgrind(tmp_path):
    r = subprocess.run(["make", "-C", str(FIRMWARE), "valgrind", f"BUILD={tmp_path}"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


# --- ramki z firmware przechodzą dekoder hosta ---


def emit_everything(fw):
    fw.link()
    fw.lib.ffi_tick(1000)
    fw.lib.ffi_set_temp(243)
    fw.lib.ffi_tick(1001)
    fw.lib.ffi_set_presence(1, 1100)
    fw.lib.ffi_gps(522297000, 210122000, 1200)
    fw.lib.ffi_fault(SENSORS["gps"], b"no_fix", 1300)
    fw.lib.ffi_fault_clear(SENSORS["gps"], 1400)
    fw.lib.ffi_button(b"b1", 0, 1500)
    fw.lib.ffi_button(b"b1", 1, 1600)
    fw.lib.ffi_local_command("głośniej".encode(), b"ok", 1700)
    utt = fw.lib.ffi_utterance_start(1800)
    for _ in range(3):
        fw.lib.ffi_mic_packet(b"\xf8\x01\x02", 3)
    fw.lib.ffi_utterance_end(60, 1860)
    return utt


def test_every_firmware_event_decodes_on_the_host(fw):
    emit_everything(fw)
    texts = [d for binary, d in fw.outputs() if not binary]
    decoded = [decode_frame(t) for t in texts]
    assert all(isinstance(d, DecodedEvent) for d in decoded)
    by_type = {}
    for d in decoded:
        by_type.setdefault(d.type, []).append(d.body)
    assert isinstance(by_type["system.boot"][0], SystemBoot)
    assert by_type["system.boot"][0].boot_id == "a1b2c3d4" and by_type["system.boot"][0].reset_reason == "power"
    hb = by_type["body.heartbeat"][0]
    assert isinstance(hb, BodyHeartbeat) and (hb.batt_pct, hb.link_rssi, hb.frame_errors) == (71.5, -61, 0)
    sensors = by_type["body.sensor"]
    assert all(isinstance(s, BodySensor) for s in sensors)
    assert [s.temp_c for s in sensors if s.temp_c is not None] == [24.3]
    assert [s.presence for s in sensors if s.presence is not None] == [True]
    assert [s.gps for s in sensors if s.gps is not None] == [{"lat": 52.2297, "lon": 21.0122}]
    assert isinstance(by_type["body.fault"][0], BodyFault) and by_type["body.fault"][0].code == "no_fix"
    assert isinstance(by_type["body.fault.clear"][0], BodyFaultClear)
    assert [(b.id, b.kind) for b in by_type["button"]] == [("b1", "press"), ("b1", "long")]
    assert isinstance(by_type["button"][0], Button)
    assert isinstance(by_type["local.command"][0], LocalCommand) and by_type["local.command"][0].cmd == "głośniej"
    assert isinstance(by_type["audio.utterance.start"][0], AudioUtteranceStart)
    assert isinstance(by_type["audio.utterance.end"][0], AudioUtteranceEnd) and by_type["audio.utterance.end"][0].dur_ms == 60


def test_mic_frames_decode_on_the_host(fw):
    utt = emit_everything(fw)
    frames = [decode_audio_frame(d) for binary, d in fw.outputs() if binary]
    assert [(f.kind, f.id, f.seq, f.data) for f in frames] == [(1, utt, i, b"\xf8\x01\x02") for i in range(3)]


def test_envelope_ids_are_unique_and_ts_is_relative_to_boot(fw):
    emit_everything(fw)
    envs = [json.loads(t) for t in fw.texts()]
    ids = [e["id"] for e in envs]
    assert len(ids) == len(set(ids)) and all(i.startswith("a1b2.") for i in ids)
    assert envs[0]["ts"] == 0 and envs[-1]["ts"] == 860


def test_host_core_builds_state_from_firmware_frames(fw):
    emit_everything(fw)
    fw.lib.ffi_fault(SENSORS["imu"], b"spi_timeout", 1900)
    core = HostCore(clock=ManualClock())
    for binary, data in fw.outputs():
        if not binary:
            core.handle_frame(data)
    body = core.body.snapshot()
    assert (body["batt_pct"], body["rssi"], body["temp_c"], body["link"]) == (71.5, -61, 24.3, "ok")
    assert body["sensors_down"] == ["imu"]
    assert core.body.boot_id == "a1b2c3d4"


# --- akcje z hosta: firmware i symulator odpowiadają tak samo ---


CASES = [
    ("ping", {}),
    ("haptic", {"pattern": "warn"}),
    ("haptic", {"pattern": "loud"}),
    ("haptic", {"pattern": 1}),
    ("haptic", {}),
    ("gpio.set", {"pin": 5, "value": 1}),
    ("gpio.set", {"pin": 9, "value": 1}),
    ("gpio.set", {"pin": 5, "value": 2}),
    ("gpio.set", {"pin": "5", "value": 1}),
    ("gpio.set", {"pin": True, "value": 1}),
    ("gpio.set", {"pin": 5.0, "value": 1}),
    ("gpio.set", {"pin": 5}),
    ("speak.clip", {"clip": "ready"}),
    ("speak.clip", {"clip": "nope"}),
    ("speak.clip", {"clip": 3}),
    ("config.set", {"key": "volume", "value": 80}),
    ("config.set", {"key": "volume", "value": 0}),
    ("config.set", {"key": "volume", "value": 100.0}),
    ("config.set", {"key": "batt_warn_pct", "value": 15.5}),
    ("config.set", {"key": "temp_warn_c", "value": -12.25}),
    ("config.set", {"key": "volume", "value": 101}),
    ("config.set", {"key": "volume", "value": -1}),
    ("config.set", {"key": "volume", "value": "x"}),
    ("config.set", {"key": "volume", "value": None}),
    ("config.set", {"key": "volume", "value": True}),
    ("config.set", {"key": "color", "value": 1}),
    ("config.set", {"key": 1, "value": 1}),
    ("speak", {"audio_id": 9}),
    ("speak", {"audio_id": "9"}),
    ("speak", {"audio_id": "abc"}),
    ("speak", {"audio_id": -3}),
    ("speak", {}),
    ("speak", {"audio_id": True}),
    ("speak", {"audio_id": None}),
    ("self.destruct", {}),
]


def raw_action(type_, body, id_="h1"):
    """Ramka jak z encode_action, ale bez walidacji hosta (żeby sprawdzić odmowy po stronie ciała)."""
    return json.dumps({"v": 1, "t": "action", "id": id_, "ts": 0, "type": type_, "body": body}, separators=(",", ":"))


def sim_ack(frame):
    m = BodyModel(SimConfig(gpio_pins=frozenset(PINS)), 0.0)
    m.take_out()
    m.on_host_text(frame, 1.0)
    acks = [decode_frame(o.data) for o in m.take_out() if isinstance(o.data, str)]
    return acks[-1] if acks else None, m.frame_errors


@pytest.mark.parametrize("type_, body", CASES, ids=[f"{t}-{json.dumps(b)}" for t, b in CASES])
def test_firmware_and_simulator_ack_the_same_way(fw, type_, body):
    frame = raw_action(type_, body)
    fw.link()
    fw.clear()
    fw.send(frame)
    (c_ack,) = fw.acks()
    py_ack, _ = sim_ack(frame)
    assert isinstance(c_ack, DecodedAck) and isinstance(c_ack.body, AckBody)
    assert (c_ack.body.ref, c_ack.envelope.type) == ("h1", type_)
    assert (c_ack.body.ok, c_ack.body.err) == (py_ack.body.ok, py_ack.body.err)


def test_actions_encoded_by_the_host_are_accepted(fw):
    fw.link()
    fw.clear()
    for i, (type_, body) in enumerate(
        [("ping", {}), ("haptic", {"pattern": "critical"}), ("gpio.set", {"pin": 6, "value": 0}),
         ("speak.clip", {"clip": "low_battery"}), ("config.set", {"key": "volume", "value": 35})]
    ):
        fw.send(encode_action(type_, body, id=f"h{i}", ts_ms=i * 10))
    acks = fw.acks()
    assert [(a.body.ref, a.body.ok) for a in acks] == [(f"h{i}", True) for i in range(5)]
    assert fw.hal_log() == "haptic critical\ngpio 6 0\nclip low_battery\nconfig volume 35000\n"
    assert fw.lib.ffi_setting(b"volume") == 35000


def test_numeric_config_values_match_python_float_parsing(fw):
    fw.link()
    for value in (0, 7, 99.999, 12.5, 100, 0.001):
        fw.clear()
        fw.send(encode_action("config.set", {"key": "batt_warn_pct", "value": value}, id="h", ts_ms=0))
        assert fw.acks()[0].body.ok
        assert fw.lib.ffi_setting(b"batt_warn_pct") == round(value * 1000)


INVALID = [
    "nie json",
    "[1,2]",
    '{"v":2,"t":"action","id":"h","type":"ping"}',
    '{"v":1,"t":"event","id":"h","type":"ping"}',
    '{"v":1,"t":"action","type":"ping"}',
    '{"v":1,"t":"action","id":7,"type":"ping"}',
    '{"v":1,"t":"action","id":"h","type":"ping","body":[1]}',
    '{"v":1,"t":"action","id":"h","type":5}',
    '{"v":1,"t":"action","id":"h","type":"ping"',
    '{"v":"1","t":"action","id":"h","type":"ping"}',
    "",
]


@pytest.mark.parametrize("frame", INVALID)
def test_invalid_frames_are_rejected_by_both_without_ack(fw, frame):
    fw.link()
    fw.clear()
    fw.send(frame)
    py_ack, py_errors = sim_ack(frame)
    assert fw.acks() == [] and py_ack is None
    assert fw.counter("frame_errors") == 1 == py_errors


def test_oversized_frames_are_rejected_by_both(fw):
    frame = raw_action("config.set", {"key": "volume", "value": "x" * 600})
    fw.link()
    fw.clear()
    fw.send(frame)
    py_ack, py_errors = sim_ack(frame)
    assert fw.acks() == [] and py_ack is None and fw.counter("frame_errors") == 1 == py_errors
    with pytest.raises(ValueError, match="512"):
        encode_action("config.set", {"key": "volume", "value": "x" * 600}, id="h", ts_ms=0)


def test_mutated_frames_never_disagree_when_both_accept(fw):
    """Losowe uszkodzenia poprawnych ramek: firmware nie może się wywrócić, a gdy obie strony
    przyjmą ramkę, odpowiadają tym samym kodem."""
    rng = random.Random(1234)
    seeds = [raw_action(t, b) for t, b in CASES]
    fw.link()
    disagreements = []
    for _ in range(3000):
        data = bytearray(rng.choice(seeds).encode())
        for _ in range(rng.randint(1, 3)):
            op = rng.random()
            pos = rng.randrange(len(data))
            if op < 0.4:
                data[pos] = rng.randrange(32, 127)
            elif op < 0.7:
                del data[pos]
            else:
                data.insert(pos, rng.choice(b'{}[]",:0123456789-.eE\\ tfn'))
        frame = bytes(data)
        fw.clear()
        fw.send(frame)
        c_acks = fw.acks()
        try:
            text = frame.decode("utf-8")
        except UnicodeDecodeError:
            continue
        py_ack, _ = sim_ack(text)
        if c_acks and py_ack is not None:
            c = (c_acks[0].body.ok, c_acks[0].body.err)
            p = (py_ack.body.ok, py_ack.body.err)
            if c != p:
                disagreements.append((text, c, p))
    assert disagreements == [], disagreements[:5]


# --- odtwarzanie: dźwięk z kodera hosta w buforze firmware ---


def test_host_encoded_speech_is_stored_and_played(fw):
    from host.brain.audio import encode_speech, playback_frames
    from host.core.config import AudioConfig
    from host.tests.audio_helpers import sine

    packets = encode_speech(sine(300, 2.0, 22050).tobytes(), 22050, AudioConfig())
    fw.link()
    fw.clear()
    for frame in playback_frames(42, packets):
        fw.lib.ffi_on_binary(frame, len(frame))
    stored = []
    ptr = ctypes.c_void_p()
    for seq in range(len(packets)):
        n = fw.lib.ffi_playback_packet(seq, ctypes.byref(ptr))
        stored.append(ctypes.string_at(ptr.value, n))
    assert stored == packets  # bajt w bajt to, co zakodował host
    fw.send(encode_action("speak", {"audio_id": 42}, id="s1", ts_ms=0))
    assert fw.acks()[0].body.ok
    assert fw.hal_log() == f"play 42 {len(packets)} {len(packets)} {sum(map(len, packets))}\n"


def test_fifteen_seconds_of_speech_fit_the_playback_buffer(fw):
    from host.brain.audio import encode_speech, playback_frames
    from host.core.config import AudioConfig
    from host.tests.audio_helpers import sine

    cfg = AudioConfig()
    packets = encode_speech(sine(300, cfg.max_speech_s, 16000).tobytes(), 16000, cfg)
    for frame in playback_frames(7, packets):
        fw.lib.ffi_on_binary(frame, len(frame))
    assert fw.counter("pb_packets") == len(packets) == 751
    assert fw.counter("pb_used") == sum(map(len, packets)) == 751 * 60  # CBR: 45 060 B z 49 152 B


def test_busy_while_playing(fw):
    fw.link()
    fw.lib.ffi_on_binary(struct.pack(">BHH", 2, 1, 0) + b"abc", 8)
    fw.send(encode_action("speak", {"audio_id": 1}, id="s1", ts_ms=0))
    fw.clear()
    fw.lib.ffi_on_binary(struct.pack(">BHH", 2, 2, 0) + b"def", 8)
    fw.send(encode_action("speak", {"audio_id": 2}, id="s2", ts_ms=0))
    assert fw.acks()[0].body.err == "busy" and fw.counter("busy_drops") == 1
    fw.lib.ffi_playback_done()
    fw.lib.ffi_on_binary(struct.pack(">BHH", 2, 2, 0) + b"def", 8)
    fw.clear()
    fw.send(encode_action("speak", {"audio_id": 2}, id="s3", ts_ms=0))
    assert fw.acks()[0].body.ok


def test_ram_budget_is_what_the_config_says(fw):
    assert fw.lib.ffi_sizeof_body() == 71960
