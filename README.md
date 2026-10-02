# IRIS

Osobisty asystent głosowy działający po polsku. Urządzenie HERMES (STM32L476 z modułami peryferyjnymi) nagrywa mowę, odtwarza odpowiedzi i zbiera dane z czujników. Host z IRIS Core (Python, na bazie frameworka OpenJarvis) rozpoznaje mowę, prowadzi rozmowę przez model językowy i wywołuje narzędzia: sterowanie domem, przypomnienia, kalendarz, odczyt czujników. Proste komendy działają lokalnie na mikrokontrolerze, także bez połączenia z hostem.

Stan: specyfikacja v2 i wzorcowa implementacja protokołu ramek. Firmware i potok głosowy nie są jeszcze napisane.

## Struktura repozytorium

```
iris/
├── CLAUDE.md          kontekst i zasady pracy dla Claude Code
├── docs/
│   ├── spec.md        specyfikacja funkcjonalna i architektura
│   ├── protocol.md    protokół ramek HERMES <-> host
│   └── decisions.md   rejestr decyzji architektonicznych
├── firmware/          firmware STM32L476 (C, STM32 HAL/LL)
├── host/
│   └── iris_protocol.py   wzorcowa implementacja protokołu z testem
└── scripts/
    └── github_setup.sh    utworzenie repozytorium, kamieni milowych i zadań
```

## Kamienie milowe

| Etap | Zakres | Sprzęt |
|---|---|---|
| M0 | Czat tekstowy po polsku: OpenJarvis z silnikiem Claude | nie |
| M1 | Pełna ścieżka głosowa na hoście: mikrofon laptopa, VAD, STT, agent, TTS | nie |
| M2 | Bramka iris-gateway i protokół ramek, symulator HERMES w Pythonie | nie |
| M3 | Firmware: nagrywanie INMP441, odtwarzanie MAX98357A, transmisja przez ESP8266 | tak |
| M4 | Słowo budzące, komendy lokalne, wykrywanie utraty hosta | tak |
| M5 | Narzędzia: czujniki, dom, przypomnienia, kalendarz | tak |

Szczegóły i kryteria ukończenia: [docs/spec.md](docs/spec.md), rozdział 13.

## Zależności zewnętrzne

- [OpenJarvis](https://github.com/open-jarvis/OpenJarvis), Apache 2.0
- faster-whisper (STT), Piper (TTS, głosy polskie)
- Claude API lub Ollama (LLM)

## Licencja

Do ustalenia. Fragmenty kodu przejęte z OpenJarvis zachowują licencję Apache 2.0 wraz z informacją o autorstwie.
