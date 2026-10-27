import logging

import numpy as np
import pytest

from host.brain.audio import (
    STT_RATE,
    AudioError,
    OpusDecoder,
    OpusEncoder,
    UtteranceAssembler,
    encode_speech,
    frame_id_for,
    playback_frames,
    resample,
)
from host.core.config import AudioConfig
from host.core.protocol import decode_audio_frame
from host.tests.audio_helpers import dominant_hz, feed, header, mic_frame, opus_packets, sine

CFG = AudioConfig()


def finish(assembler, utt_id=7, declared=None):
    raw = assembler.take(utt_id, declared)
    return None if raw is None else assembler.decode(raw)


# --- kodek ---


@pytest.mark.parametrize("rate, frame_ms", [(8000, 20), (16000, 20), (24000, 10), (48000, 40), (16000, 60), (16000, 2.5)])
def test_encoder_frame_sizes_and_roundtrip(rate, frame_ms):
    pcm = sine(300, 1.0, rate)
    enc = OpusEncoder(rate, frame_ms, 24000)
    packets = enc.encode(pcm)
    assert enc.frame_samples == round(rate * frame_ms / 1000)
    assert abs(len(packets) - 1000 / frame_ms) <= 2  # +1 pakiet z opróżnienia kodera
    dec = OpusDecoder()
    out = np.concatenate([dec.decode(p) for p in packets])
    assert abs(out.size / 48000 - 1.0) < 0.1
    assert abs(dominant_hz(resample(out, 48000, STT_RATE)) - 300) < 5


def test_decoder_output_is_48k_whatever_the_encoder_rate():
    for rate in (8000, 16000):
        dec = OpusDecoder()
        assert dec.decode(OpusEncoder(rate, 20, 24000).encode(sine(300, 0.1, rate))[0]).size == 960


def test_empty_packet_does_not_poison_the_decoder():
    good = opus_packets(seconds=0.2)
    dec = OpusDecoder()
    assert dec.decode(good[0]).size == 960
    assert dec.decode(b"").size == 0
    assert dec.decode(good[1]).size == 960  # bez zabezpieczenia: EOF do końca strumienia


def test_undecodable_packet_raises_audio_error_and_decoder_recovers():
    good = opus_packets(seconds=0.2)
    dec = OpusDecoder()
    with pytest.raises(AudioError):
        dec.decode(b"\xff")
    assert dec.decode(good[0]).size == 960


def test_resample_lengths_and_identity():
    x = sine(440, 1.0, 22050)
    y = resample(x, 22050, 16000)
    assert abs(y.size - 16000) <= 4 and abs(dominant_hz(y, 16000) - 440) < 5
    assert resample(x, 22050, 22050) is x
    assert resample(np.zeros(0, dtype=np.int16), 22050, 16000).size == 0


# --- odtwarzanie ---


def test_encode_speech_resamples_and_frames_have_spec_header():
    pcm = sine(300, 1.0, 22050).tobytes()
    packets = encode_speech(pcm, 22050, CFG)
    assert abs(len(packets) - 51) <= 2
    frames = playback_frames(5, packets)
    assert [header(f) for f in frames[:3]] == [(2, 5, 0), (2, 5, 1), (2, 5, 2)]
    assert header(frames[-1]) == (2, 5, len(frames) - 1)
    assert frames[0][5:] == packets[0]
    # ramki kind=2 nie są poprawnymi ramkami od ciała, więc host odrzuciłby je na wejściu
    with pytest.raises(Exception):
        decode_audio_frame(frames[0])


def test_encode_speech_honours_config():
    cfg = AudioConfig(playback_rate_hz=8000, frame_ms=40, bitrate_bps=12000)
    packets = encode_speech(sine(300, 1.0, 16000).tobytes(), 16000, cfg)
    assert abs(len(packets) - 26) <= 2


@pytest.mark.parametrize("audio_id", [0, 65536, -1])
def test_playback_frames_rejects_bad_audio_id(audio_id):
    with pytest.raises(AudioError):
        playback_frames(audio_id, [b"x"])


def test_playback_frames_accepts_bounds():
    assert header(playback_frames(65535, [b"x"])[0]) == (2, 65535, 0)
    assert header(playback_frames(1, [b"x"])[0]) == (2, 1, 0)


# --- składanie wypowiedzi ---


def test_assembles_utterance_from_real_opus():
    a = UtteranceAssembler(CFG)
    feed(a, 7, opus_packets(440, 1.5))
    u = finish(a, 7, 1500)
    assert (u.lost, u.truncated, u.utt_id) == (0, False, 7)
    assert abs(u.duration_ms - 1500) < 60
    assert abs(dominant_hz(u.pcm) - 440) < 5
    assert len(u.pcm) == round(u.duration_ms * STT_RATE / 1000) * 2  # s16le


def test_lost_packets_become_silence_without_shifting_time():
    packets = opus_packets(440, 2.0)
    intact = UtteranceAssembler(CFG)
    feed(intact, 7, packets)
    reference = finish(intact)

    a = UtteranceAssembler(CFG)
    feed(a, 7, packets, drop={10, 11, 30})
    u = finish(a)
    assert u.lost == 3 and u.packets == len(packets) - 3
    assert u.duration_ms == pytest.approx(reference.duration_ms, abs=1)

    # Pakiety 10-11 to 200-240 ms, czyli próbki 3200-3840 przy 16 kHz. Bierzemy wnętrze luki,
    # z marginesem na opóźnienie kodera i rozmycie filtra resamplera.
    window = slice(3450, 3590)
    assert np.abs(np.frombuffer(reference.pcm, dtype=np.int16)[window]).max() > 8000
    assert np.abs(np.frombuffer(u.pcm, dtype=np.int16)[window]).max() < 1500


def test_empty_and_undecodable_packets_count_as_lost():
    packets = opus_packets(440, 1.0)
    packets[5] = b""
    packets[6] = b"\xff"
    a = UtteranceAssembler(CFG)
    feed(a, 7, packets)
    u = finish(a)
    assert u.lost == 2 and abs(u.duration_ms - 1000) < 60


def test_duplicates_and_out_of_order_are_handled():
    packets = opus_packets(440, 1.0)
    a = UtteranceAssembler(CFG)
    a.start(7)
    order = list(range(len(packets)))
    order[3], order[4] = order[4], order[3]
    for seq in order + [0, 1, 2]:
        a.add(mic_frame(7, seq, packets[seq]))
    u = finish(a)
    assert u.packets == len(packets) and u.lost == 0
    assert abs(dominant_hz(u.pcm) - 440) < 5


def test_sequence_may_start_above_zero():
    packets = opus_packets(440, 0.5)
    a = UtteranceAssembler(CFG)
    a.start(7)
    for seq, p in enumerate(packets):
        a.add(mic_frame(7, 100 + seq, p))
    u = finish(a)
    assert u.lost == 0 and abs(u.duration_ms - 500) < 60


def test_frames_without_matching_utterance_are_dropped(caplog):
    packets = opus_packets(440, 0.5)
    a = UtteranceAssembler(CFG)
    assert a.add(mic_frame(7, 0, packets[0])) is False  # bez start
    a.start(7)
    assert a.add(mic_frame(8, 0, packets[0])) is False  # inne id
    assert a.add(mic_frame(7, 0, packets[0])) is True
    assert a.dropped_frames == 2


def test_new_start_abandons_unfinished_utterance(caplog):
    a = UtteranceAssembler(CFG)
    feed(a, 7, opus_packets(440, 0.5))
    with caplog.at_level(logging.WARNING):
        feed(a, 8, opus_packets(880, 0.5))
    assert any("porzucono" in r.getMessage() for r in caplog.records)
    assert a.take(7) is None  # stara wypowiedź nie istnieje
    u = finish(a, 8)
    assert abs(dominant_hz(u.pcm) - 880) < 5


def test_end_without_start_or_second_end():
    a = UtteranceAssembler(CFG)
    assert a.take(7) is None
    feed(a, 7, opus_packets(440, 0.5))
    assert finish(a, 7) is not None
    assert a.take(7) is None and not a.active


def test_end_with_matching_low_bits_only():
    a = UtteranceAssembler(CFG)
    feed(a, 70000, opus_packets(440, 0.5))  # id ramek = 70000 & 0xFFFF = 4464
    assert finish(a, 70000) is not None


@pytest.mark.parametrize("utt_id, expected", [(7, 7), ("12", 12), (65536 + 5, 5), ("abc", None), (None, None), (True, None)])
def test_frame_id_for(utt_id, expected):
    assert frame_id_for(utt_id) == expected


def test_non_numeric_utterance_id_is_skipped(caplog):
    a = UtteranceAssembler(CFG)
    with caplog.at_level(logging.WARNING):
        assert a.start("abc") is False
    assert not a.active


def test_too_short_utterance_is_ignored():
    a = UtteranceAssembler(CFG)
    feed(a, 7, opus_packets(440, 0.2))  # 200 ms < 300 ms
    assert finish(a) is None


def test_empty_utterance_is_ignored():
    a = UtteranceAssembler(CFG)
    a.start(7)
    assert finish(a) is None


def test_utterance_is_truncated_at_max_length():
    cfg = AudioConfig(max_utterance_s=1.0, min_utterance_ms=0)
    a = UtteranceAssembler(cfg)
    feed(a, 7, opus_packets(440, 2.0))
    u = finish(a)
    assert u.truncated and u.packets == 50 and abs(u.duration_ms - 1000) < 60


def test_playback_is_constant_bitrate_whatever_the_signal():
    rng = np.random.default_rng(0)
    signals = [
        sine(300, 2.0, 16000),  # przy VBR ton dawał ok. 41 kbit/s
        (rng.normal(0, 6000, 32000)).clip(-32767, 32767).astype(np.int16),
        np.zeros(32000, dtype=np.int16),
    ]
    for pcm in signals:
        packets = encode_speech(pcm.tobytes(), 16000, CFG)
        assert {len(p) for p in packets[:-1]} == {60}  # 24 kbit/s * 20 ms = 60 B
