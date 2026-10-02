# IRIS: kontekst dla Claude Code

Osobisty asystent głosowy po polsku. HERMES (STM32L476) to urządzenie z mikrofonem, głośnikiem i czujnikami. IRIS Core to host w Pythonie na bazie OpenJarvis. Źródła prawdy: `docs/spec.md` (wymagania IR-xx, plan M0–M5), `docs/protocol.md` (ramki), `docs/decisions.md` (decyzje D-xxx).

## Język

Rozmowa, dokumentacja, komentarze, komunikaty commitów i opisy zadań: po polsku. Identyfikatory w kodzie po angielsku.

## Zasady pracy

- Pracuj nad jednym kamieniem milowym naraz, w kolejności M0 do M5. Zadania są w GitHub Issues.
- Nie opisuj jako zrobionego niczego, czego nie uruchomiono. Przy każdym zadaniu podaj, co uruchomiono i jaki był wynik.
- Wartości liczbowe w `docs/spec.md` są docelowe. Pomiar zapisuj w issue przy odpowiednim IR-xx, a nie w specyfikacji.
- Przed użyciem modułu OpenJarvis sprawdź go w jego kodzie, nie zakładaj jego działania z nazwy pliku. `ws_bridge` i `stream_bridge` nie zostały jeszcze sprawdzone.
- Decyzje oznaczone "do weryfikacji" w `docs/decisions.md` (D-004, D-007) rozstrzyga pomiar. Wynik wpisz do rejestru i zmień status.
- Zmiana protokołu wymaga jednoczesnej zmiany `docs/protocol.md`, `host/iris_protocol.py` i jego testu.

## Ograniczenia

- Funkcje IRIS nie wpływają na alarmy fizjologiczne ani na kanał bezpieczeństwa z dokumentacji HERMES rev B (D-009).
- Odczyty MAX30102 (tętno, SpO₂) trafiają do LLM tylko przez narzędzie wywołane w odpowiedzi na pytanie użytkownika (IR-09).
- Klucze API tylko w `.env`, który jest w `.gitignore`. Nie commituj nagrań audio ani baz SQLite.
- `scripts/github_setup.sh` uruchamia się jeden raz. Drugie uruchomienie dubluje kamienie milowe i zadania.
- Nie wykonuj `git push --force` ani zmian w ustawieniach repozytorium bez wyraźnego polecenia.

## Polecenia

```bash
python3 host/iris_protocol.py      # test protokołu, oczekiwany wynik: OK
gh issue list --milestone "M0 Czat tekstowy"
```

```bash
# OpenJarvis (poza repozytorium): ~/src/OpenJarvis; po każdym `uv sync` zbuduj ponownie rozszerzenie Rust
cd ~/src/OpenJarvis && uv run maturin develop -m rust/crates/openjarvis-python/Cargo.toml
bash host/tests/run_pl_questions.sh   # 20 pytań M0 przez `jarvis ask`; wymaga ANTHROPIC_API_KEY w .env
```

Polecenia firmware (STM32CubeIDE lub CMake z arm-none-eabi-gcc) uzupełnij tutaj, gdy powstanie katalog firmware.

## Stan

Zrobione: specyfikacja v2, protokół ramek v1 z testem, rejestr decyzji.
Następne: M0 (OpenJarvis lokalnie, silnik Claude, 20 pytań testowych po polsku).
