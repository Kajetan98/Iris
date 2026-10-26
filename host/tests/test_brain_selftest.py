import pytest

from host.brain import build_stt, build_tts
from host.brain.selftest import main, word_error_rate
from host.brain.speech import Speech, SpeechConfigError, Transcript
from host.brain.stt_whisper import FasterWhisperEngine
from host.brain.tts_piper import PiperEngine
from host.core.config import STTConfig, TTSConfig
from host.tests.audio_helpers import sine


class ToneTTS:
    async def synthesize(self, text):
        return Speech(sine(300, 2.0, 22050).tobytes(), 22050)

    async def aclose(self):
        pass


class FixedSTT:
    def __init__(self, text):
        self.text, self.heard = text, []

    async def transcribe(self, pcm, rate):
        self.heard.append((len(pcm), rate))
        return Transcript(self.text, "pl", len(pcm) / 2 / rate)

    async def aclose(self):
        pass


@pytest.mark.parametrize(
    "reference, hypothesis, wer",
    [
        ("ala ma kota", "Ala ma kota.", 0.0),
        ("ala ma kota", "ala ma psa", pytest.approx(1 / 3)),
        ("ala ma kota", "ala kota", pytest.approx(1 / 3)),
        ("ala ma kota", "", 1.0),
        ("", "", 0.0),
        ("", "coś", 1.0),
        ("Dzień dobry!", "dzien dobry", pytest.approx(0.5)),  # diakrytyki to inne słowo
    ],
)
def test_word_error_rate(reference, hypothesis, wer):
    assert word_error_rate(reference, hypothesis) == wer


def test_selftest_passes_when_transcript_matches(capsys):
    stt = FixedSTT("Dzień dobry, jestem asystentem głosowym. Mam się dobrze, dziękuję.")
    assert main([], stt=stt, tts=ToneTTS()) == 0
    out = capsys.readouterr().out
    assert "TTS:" in out and "Opus:" in out and "Błędne słowa: 0%" in out
    assert stt.heard[0][1] == 16000 and abs(stt.heard[0][0] / 2 / 16000 - 2.0) < 0.1


def test_selftest_fails_when_transcript_is_wrong():
    assert main([], stt=FixedSTT("zupełnie inny tekst"), tts=ToneTTS()) == 1


def test_selftest_requires_enabled_engines(capsys):
    assert main([]) == 2
    assert "brain.stt" in capsys.readouterr().err


def test_selftest_reports_missing_model(tmp_path, capsys):
    cfg = tmp_path / "iris.toml"
    cfg.write_text(
        f'[brain.stt]\nengine = "faster-whisper"\nmodel = "{tmp_path}/brak-modelu"\n'
        f'[brain.tts]\nengine = "piper"\nvoice = "{tmp_path}/brak.onnx"\n'
    )
    assert main(["--config", str(cfg)]) == 2
    assert "nie istnieje" in capsys.readouterr().err


# --- fabryki z konfiguracji ---


def test_builders_return_none_when_disabled():
    assert build_stt(STTConfig()) is None and build_tts(TTSConfig()) is None


def test_builders_create_engines_from_config():
    stt = build_stt(STTConfig(engine="faster-whisper", model="small", beam_size=3, language="pl"))
    assert isinstance(stt, FasterWhisperEngine) and stt.model_name == "small" and stt._beam_size == 3
    tts = build_tts(TTSConfig(engine="piper", voice="/g.onnx", length_scale=1.1, max_chars=200))
    assert isinstance(tts, PiperEngine) and tts.max_chars == 200 and tts._syn_args == {"length_scale": 1.1}


@pytest.mark.parametrize("cls, kwargs", [(STTConfig, {"engine": "faster-whisper"}), (TTSConfig, {"engine": "piper"})])
def test_engine_requires_model_or_voice(cls, kwargs):
    with pytest.raises(Exception, match="engine wymaga"):
        cls(**kwargs)
