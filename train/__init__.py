"""Potok dostrajania własnego modelu IRIS (docs/spec.md, sekcja 15; docs/adr/0002-wlasny-model-iris.md).

Zbiór danych, kontrole i ocena używają tylko hosta i biblioteki standardowej. Biblioteki treningowe
(torch, transformers, peft) importuje dopiero train/finetune.py; host ich nie potrzebuje.
"""
