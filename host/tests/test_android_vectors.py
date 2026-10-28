"""Wektory zgodności Python/Kotlin (docs/spec.md, sekcja 14) są aktualne i niezależne od czasu."""

import json

from host.tests import make_android_vectors as vectors


def test_the_committed_vectors_match_the_host_rules():
    # Zmiana reguły w hoście musi pociągnąć regenerację pliku i poprawkę Kotlina w android/core.
    assert vectors.VECTORS_PATH.read_text(encoding="utf-8") == vectors.render(), (
        "android/core/src/test/resources/learning_vectors.json jest nieaktualny: "
        "uruchom python -m host.tests.make_android_vectors"
    )


def test_generation_is_deterministic():
    assert vectors.render() == vectors.render()


def test_scenarios_cover_every_reply_the_phone_can_give():
    data = json.loads(vectors.render())
    replies = {step["reply"] for s in data["scenarios"] for step in s["expected"] if step["reply"]}
    for fragment in ("Zapamiętane:", "Zapomniane:", "Nie znam takiej informacji.", "Wiem od ciebie:", "Przypuszczam, że",
                     "Na pewno usunąć wszystko", "Dobrze, niczego nie usuwam.", "Dobrze, usuwam wszystko",
                     "Dobrze, zapamiętane.", "Dobrze, poprawione:", "Dobrze, usuwam to przypuszczenie.",
                     "Czy dobrze rozumiem:", "Nic o tobie nie wiem", "Jeszcze nic o tobie nie wiem"):
        assert any(fragment in r for r in replies), fragment
