"""Komenda przełączenia urządzenia rozpoznawana bez LLM (docs/spec.md, sekcja 1)."""

import pytest

from host.core.commands import parse_switch, resolve_device


@pytest.mark.parametrize(
    "text, name",
    [
        ("Przełącz na laptop", "laptop"),
        ("przełącz się na telefon.", "telefon"),
        ("IRIS, przełącz na Pi Kuchnia!", "pi kuchnia"),
        ("Iris przejdź na laptopa", "laptopa"),
        ("zmień urządzenie na hermes?", "hermes"),
        ("  PRZEŁĄCZ   NA   laptop  ", "laptop"),
    ],
)
def test_switch_phrases(text, name):
    assert parse_switch(text) == name


@pytest.mark.parametrize(
    "text",
    ["Jaka jest pogoda?", "przełącz", "przełącz na", "nie przełączaj na laptop", "przełącz światło", "na laptop"],
)
def test_other_text_is_not_a_command(text):
    assert parse_switch(text) is None


def test_device_names_ids_endings_and_aliases():
    devices = ["hermes", "laptop", "pi-kuchnia", "telefon"]
    aliases = {"pi-kuchnia": ["kuchnia", "kuchnię", "kuchni"], "laptop": ["komputer"]}
    assert resolve_device("laptop", devices, aliases) == ["laptop"]
    assert resolve_device("laptopa", devices, aliases) == ["laptop"]  # końcówka przypadku
    assert resolve_device("telefonu", devices, aliases) == ["telefon"]
    assert resolve_device("hermesem", devices, aliases) == ["hermes"]
    assert resolve_device("pi kuchnia", devices, aliases) == ["pi-kuchnia"]
    assert resolve_device("Kuchnię", devices, aliases) == ["pi-kuchnia"]
    assert resolve_device("komputer", devices, aliases) == ["laptop"]
    assert resolve_device("lodówkę", devices, aliases) == []
    assert resolve_device("lapto", devices, aliases) == []  # bez dopasowań częściowych
    both = {"laptop": ["komputer"], "pc": ["komputer"]}
    assert resolve_device("komputer", ["laptop", "pc"], both) == ["laptop", "pc"]  # niejednoznaczne
