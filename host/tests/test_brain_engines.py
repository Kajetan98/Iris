"""Silniki STT i TTS. Modele zastępują zaślepki, ale wywołania sprawdzamy też względem prawdziwych bibliotek."""

import asyncio
import inspect
from types import SimpleNamespace

import numpy as np
import pytest

from host.brain.speech import SpeechConfigError, SpeechError
from host.brain.stt_whisper import FasterWhisperEngine
from host.brain.tts_piper import PiperEngine
from host.tests.audio_helpers import sine

run = asyncio.run


# --- faster-whisper ---


class FakeModel:
    loads = []

    def __init__(self, name, **kw):
        FakeModel.loads.append((name, kw))
        self.calls = []
        self.segments = [SimpleNamespace(text=" Dzień dobry. ", avg_logprob=-0.2), SimpleNamespace(text="  ", avg_logprob=-0.9), SimpleNamespace(text="Jak się masz?", avg_logprob=-0.4)]
        self.info = SimpleNamespace(language="pl")

    def transcribe(self, audio, **kw):
        self.calls.append((audio, kw))
        return iter(self.segments), self.info


@pytest.fixture(autouse=True)
def _reset_loads():
    FakeModel.loads.clear()


def whisper(**kw):
    return FasterWhisperEngine("small", model_factory=FakeModel, **kw)


def pcm16(freq=440, seconds=1.0, rate=16000):
    return sine(freq, seconds, rate).tobytes()


def test_whisper_transcribes_with_expected_arguments():
    eng = whisper(language="pl", beam_size=2, vad_filter=False)
    t = run(eng.transcribe(pcm16(), 16000))
    assert t.text == "Dzień dobry. Jak się masz?"  # puste segmenty pominięte
    assert (t.language, t.duration_s) == ("pl", 1.0)
    assert t.avg_logprob == pytest.approx((-0.2 - 0.9 - 0.4) / 3)
    model = eng._model
    audio, kw = model.calls[0]
    assert audio.dtype == np.float32 and audio.size == 16000 and np.abs(audio).max() <= 1.0
    assert kw == {"language": "pl", "beam_size": 2, "vad_filter": False, "condition_on_previous_text": False}


def test_whisper_load_arguments_and_single_load():
    eng = whisper(device="cuda", compute_type="float16", model_dir="/modele", local_files_only=True, cpu_threads=4)
    run(eng.transcribe(pcm16(), 16000))
    run(eng.transcribe(pcm16(), 16000))
    assert FakeModel.loads == [
        ("small", {"device": "cuda", "compute_type": "float16", "download_root": "/modele",
                   "local_files_only": True, "cpu_threads": 4})
    ]


def test_whisper_resamples_other_rates_to_16k():
    eng = whisper()
    run(eng.transcribe(pcm16(rate=22050, seconds=1.0), 22050))
    audio, _ = eng._model.calls[0]
    assert abs(audio.size - 16000) <= 4


def test_whisper_no_segments_gives_empty_text():
    eng = whisper()
    eng.load()
    eng._model.segments = []
    t = run(eng.transcribe(pcm16(), 16000))
    assert t.text == "" and t.avg_logprob is None


@pytest.mark.parametrize("name", ["small", "Systran/faster-whisper-small"])
def test_whisper_repo_ids_are_not_mistaken_for_paths(name):
    eng = FasterWhisperEngine(name, model_factory=FakeModel)
    eng.load()
    assert FakeModel.loads[0][0] == name


def test_whisper_existing_local_directory_is_used(tmp_path):
    eng = FasterWhisperEngine(str(tmp_path), model_factory=FakeModel)
    eng.load()
    assert FakeModel.loads[0][0] == str(tmp_path)


@pytest.mark.parametrize("name", ["/nie/ma/takiego/modelu", "./brak", "~/brak-modelu-xyz"])
def test_whisper_missing_model_directory_fails_before_loading(name):
    eng = FasterWhisperEngine(name, model_factory=FakeModel)
    with pytest.raises(SpeechConfigError, match="nie istnieje"):
        eng.load()
    assert FakeModel.loads == []


@pytest.mark.parametrize("exc", [OSError("brak sieci"), ValueError("zła nazwa"), RuntimeError("uszkodzony"), KeyError("x")])
def test_whisper_load_failure_is_config_error(exc):
    def broken(name, **kw):
        raise exc

    eng = FasterWhisperEngine("small", model_factory=broken)
    with pytest.raises(SpeechConfigError) as e:
        eng.load()
    assert "huggingface.co" in str(e.value) and type(exc).__name__ in str(e.value)


def test_whisper_runtime_failure_is_speech_error():
    eng = whisper()
    eng.load()

    def boom(audio, **kw):
        raise RuntimeError("CUDA out of memory")

    eng._model.transcribe = boom
    with pytest.raises(SpeechError, match="RuntimeError"):
        run(eng.transcribe(pcm16(), 16000))


def test_whisper_arguments_exist_in_the_real_library():
    fw = pytest.importorskip("faster_whisper")
    transcribe = inspect.signature(fw.WhisperModel.transcribe).parameters
    for name in ("language", "beam_size", "vad_filter", "condition_on_previous_text"):
        assert name in transcribe
    init = inspect.signature(fw.WhisperModel.__init__).parameters
    for name in ("device", "compute_type", "download_root", "local_files_only", "cpu_threads"):
        assert name in init


def test_whisper_real_library_reports_missing_model_offline():
    pytest.importorskip("faster_whisper")
    eng = FasterWhisperEngine("Systran/faster-whisper-brak-takiego-modelu", local_files_only=True)
    with pytest.raises(SpeechConfigError, match="nie można załadować"):
        eng.load()


def test_whisper_real_library_rejects_directory_without_model(tmp_path):
    pytest.importorskip("faster_whisper")
    (tmp_path / "model.bin").write_text("x")
    eng = FasterWhisperEngine(str(tmp_path), local_files_only=True)
    with pytest.raises(SpeechConfigError, match="RuntimeError"):
        eng.load()


# --- Piper ---


class FakeChunk:
    def __init__(self, pcm, rate=22050, channels=1, width=2):
        self.audio_int16_bytes = pcm
        self.sample_rate = rate
        self.sample_channels = channels
        self.sample_width = width


class FakeVoice:
    def __init__(self, chunks):
        self.chunks = chunks
        self.calls = []

    def synthesize(self, text, *args):
        self.calls.append((text, args))
        return iter(self.chunks)


class FakeSynthesisConfig:
    def __init__(self, **kw):
        self.kw = kw


@pytest.fixture
def voice_file(tmp_path):
    path = tmp_path / "pl_PL-test-medium.onnx"
    path.write_bytes(b"onnx")
    return str(path)


def piper(voice_file, chunks=None, **kw):
    voice = FakeVoice(chunks if chunks is not None else [FakeChunk(b"\x01\x00" * 100)])
    eng = PiperEngine(voice_file, voice_loader=lambda path: voice, synthesis_config_cls=FakeSynthesisConfig, **kw)
    return eng, voice


def test_piper_concatenates_chunks(voice_file):
    eng, voice = piper(voice_file, [FakeChunk(b"\x01\x00" * 10), FakeChunk(b"\x02\x00" * 5)])
    speech = run(eng.synthesize("  Dzień dobry.  "))
    assert speech.rate == 22050 and speech.pcm == b"\x01\x00" * 10 + b"\x02\x00" * 5
    assert voice.calls == [("Dzień dobry.", ())]  # bez konfiguracji, gdy nic nie ustawiono


def test_piper_passes_only_configured_synthesis_options(voice_file):
    eng, voice = piper(voice_file, length_scale=1.2, volume=0.8)
    run(eng.synthesize("Cześć"))
    (cfg,) = voice.calls[0][1]
    assert cfg.kw == {"length_scale": 1.2, "volume": 0.8}


def test_piper_truncates_long_text(voice_file, caplog):
    eng, voice = piper(voice_file, max_chars=10)
    run(eng.synthesize("a" * 50))
    assert voice.calls[0][0] == "a" * 10


@pytest.mark.parametrize("text", ["", "   \n"])
def test_piper_rejects_empty_text(voice_file, text):
    eng, voice = piper(voice_file)
    with pytest.raises(SpeechError, match="pusty"):
        run(eng.synthesize(text))
    assert voice.calls == []


@pytest.mark.parametrize(
    "chunks, fragment",
    [([], "nie zwróciła"), ([FakeChunk(b"xx", channels=2)], "formacie"), ([FakeChunk(b"xx", width=4)], "formacie"),
     ([FakeChunk(b"xx", rate=22050), FakeChunk(b"xx", rate=16000)], "formacie")],
)
def test_piper_rejects_unexpected_output(voice_file, chunks, fragment):
    eng, _ = piper(voice_file, chunks)
    with pytest.raises(SpeechError, match=fragment):
        run(eng.synthesize("tekst"))


def test_piper_synthesis_failure_is_speech_error(voice_file):
    eng, voice = piper(voice_file)

    def boom(text, *args):
        raise RuntimeError("onnx")

    voice.synthesize = boom
    with pytest.raises(SpeechError, match="RuntimeError"):
        run(eng.synthesize("tekst"))


def test_piper_loads_once(voice_file):
    loads = []
    voice = FakeVoice([FakeChunk(b"\x00\x00")])
    eng = PiperEngine(voice_file, voice_loader=lambda p: loads.append(p) or voice)
    run(eng.synthesize("a"))
    run(eng.synthesize("b"))
    assert loads == [voice_file]


def test_piper_missing_files_are_config_errors(tmp_path):
    with pytest.raises(SpeechConfigError, match="nie istnieje"):
        PiperEngine(str(tmp_path / "brak.onnx")).load()
    path = tmp_path / "glos.onnx"
    path.write_bytes(b"x")
    with pytest.raises(SpeechConfigError, match="konfiguracji"):
        PiperEngine(str(path)).load()  # brak glos.onnx.json


def test_piper_loader_failure_is_config_error(voice_file):
    def broken(path):
        raise KeyError("num_symbols")

    with pytest.raises(SpeechConfigError, match="KeyError"):
        PiperEngine(voice_file, voice_loader=broken).load()


def test_piper_arguments_exist_in_the_real_library():
    piper_lib = pytest.importorskip("piper")
    assert "config_path" in inspect.signature(piper_lib.PiperVoice.load).parameters
    cfg = piper_lib.SynthesisConfig(length_scale=1.1, speaker_id=0, volume=0.9)
    assert (cfg.length_scale, cfg.speaker_id, cfg.volume) == (1.1, 0, 0.9)
    synth = inspect.signature(piper_lib.PiperVoice.synthesize).parameters
    assert list(synth)[:3] == ["self", "text", "syn_config"]
    fields = set(piper_lib.AudioChunk.__dataclass_fields__)
    assert {"sample_rate", "sample_width", "sample_channels"} <= fields
    assert isinstance(piper_lib.AudioChunk.audio_int16_bytes, property)


@pytest.mark.parametrize("config_text", ["{}", "nie json"])
def test_piper_real_library_reports_corrupt_voice(tmp_path, config_text):
    pytest.importorskip("piper")
    path = tmp_path / "glos.onnx"
    path.write_bytes(b"to nie jest model")
    (tmp_path / "glos.onnx.json").write_text(config_text)
    with pytest.raises(SpeechConfigError, match="nie można załadować"):
        PiperEngine(str(path)).load()
