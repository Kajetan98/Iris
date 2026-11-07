"""Wektory zgodności Python/Kotlin (docs/spec.md, sekcja 14) są aktualne i niezależne od czasu."""

import json

from host.tests import make_android_vectors as vectors


def test_the_committed_vectors_match_the_host_rules():
    # Zmiana reguły w hoście musi pociągnąć regenerację pliku i poprawkę Kotlina w android/core.
    assert vectors.VECTORS_PATH.read_text(encoding="utf-8") == vectors.render(), (
        "android/core/src/test/resources/memory_vectors.json jest nieaktualny: "
        "uruchom python -m host.tests.make_android_vectors"
    )


def test_generation_is_deterministic():
    assert vectors.render() == vectors.render()


def test_scenario_covers_every_reply_the_phone_can_give():
    data = json.loads(vectors.render())
    replies = {str(s["reply"]) for s in data["scenario"] if s["reply"]}
    for fragment in ("Zapamiętane:", "Zapomniane:", "Nie pamiętam takiego pojęcia.", "Najmocniej kojarzę:",
                     "Na pewno usunąć całą pamięć", "Dobrze, niczego nie usuwam.", "Pamięć wyczyszczona.",
                     "Nic nie pamiętam", "Jeszcze nic nie pamiętam", "Dobrze, wzmacniam", "Dobrze, osłabiam",
                     "Nie znalazłem w tym zdaniu", "(siła "):
        assert any(fragment in r for r in replies), fragment
