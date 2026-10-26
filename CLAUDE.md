# IRIS

Asystent głosowy po polsku, zbudowany z ciała (HERMES, STM32L476) i hosta (Python). Host utrzymuje model własnego stanu ("self", "body"), który działa bez LLM. LLM jest opcjonalny i wywoływany tylko przy rozmowie.

## Cel

- Warstwa stanu (body, self, drives, continuity) działa bez LLM, STT i TTS.
- LLM, STT i TTS są w module `host/brain` i można je wyłączyć bez wpływu na resztę.
- Źródłem prawdy dla protokołu jest `docs/spec.md`. Kod ma być z nim zgodny, a nie odwrotnie.

## Decyzje

- Host: Python 3.11+, FastAPI, SQLite.
- Łącze ciało-host: WebSocket, ramki JSON. Audio jako ramki binarne (Opus).
- Firmware: C (HAL/LL), FreeRTOS do rozważenia po pierwszym działającym prototypie.
- LLM: adapter z wymiennym silnikiem (Ollama lokalnie, Claude API opcjonalnie).
- STT: faster-whisper. TTS: Piper (polskie głosy), do zweryfikowania jakości.
- Pełny model `self` istnieje tylko na hoście.

## Ograniczenia

- STM32L476: ok. 1 MB flash, 128 KB RAM. Firmware nie parsuje dużych JSON-ów i nie trzyma historii.
- Dane z HERMES (tętno, lokalizacja) to dane o zdrowiu i położeniu. Do LLM w chmurze trafia tylko wycinek potrzebny do zapytania.
- IRIS jest programem. Opisy stanu ("bateria ciała 15%") pochodzą z danych, nie z odczuwania. Prompty systemowe nie mogą twierdzić inaczej.

## Zasady pracy

- Najpierw plan modułu, potem kod. Przed zmianą protokołu zaktualizuj `docs/spec.md`.
- Jeden moduł na sesję, z testami jednostkowymi, commit po każdym kroku.
- Nie dodawaj zależności bez pytania.
- Reguły potrzeb (drives) są deterministyczne i mają histerezę. Nie używaj LLM do ich obliczania.
- Każde zdarzenie i akcja ma typ zdefiniowany w spec. Nieznany typ jest logowany i odrzucany, nie powoduje awarii.
- Progi liczbowe w spec to wartości początkowe. Trzymaj je w konfiguracji, nie w kodzie.

## Układ repozytorium

```
docs/spec.md     protokół i model stanu
host/core/       body, self, drives, continuity
host/store/      SQLite, zapis zdarzeń i stanu
host/api/        WebSocket i REST
host/brain/      adapter LLM, STT, TTS
host/tests/
firmware/        STM32L476
sim/             symulator ciała
```

## Komendy

- zależności: `pip install pydantic fastapi uvicorn websockets pytest httpx`
- testy: `pytest host/tests`
- serwer: `uvicorn host.api.main:app --reload` (konfiguracja TOML ze zmiennej `IRIS_CONFIG`, bez niej wartości domyślne; baza `iris.db` w bieżącym katalogu)
- LLM (domyślnie wyłączony), w pliku z `IRIS_CONFIG`: `[brain.llm]` z `engine = "ollama"` i `model = "..."` (opcje: `base_url`, `send_facts`, `max_tokens`; lista w `host/core/config.py`)
- symulator ciała: `python -m sim.body --url ws://localhost:8000/ws` (komendy na stdin, `help` je wypisuje; scenariusze: `--script sim/scenarios/low_battery.txt`)

## Kolejność prac

1. `docs/spec.md`
2. `host/core`
3. `host/store`
4. `host/api`
5. `sim/`
6. `host/brain`
7. `firmware/`

Kroki 1-5 nie wymagają sprzętu ani LLM.
