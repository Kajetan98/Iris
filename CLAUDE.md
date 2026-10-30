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
- STT: faster-whisper. TTS: Piper (polskie głosy), do zweryfikowania jakości. `piper-tts` ma licencję GPL-3.0-or-later: sprawdź zgodność przed dystrybucją hosta.
- Pełny model `self` istnieje tylko na hoście. Wyjątek: aplikacja Android bez hosta ma tylko pamięć faktów (spec, sekcja 14).
- Android: Kotlin, Gradle (`android/`), moduł `core` (czysty Kotlin/JVM, testowany tutaj) i `app` (Android, budowany w CI). Mowa z systemu, model przez Claude API z telefonu (klucz wpisywany w aplikacji) albo lokalny model `.litertlm` przez LiteRT-LM (`android/app`, `LiteRtGenerator`; port `LocalGenerator` w `core`). Hasło wybudzające "Hej Iris" / "Irysku": Vosk offline (`WakeWordListener`, model polski pobierany na żądanie), tylko przy otwartej aplikacji.

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
client/          program na komputer i Raspberry Pi
android/         aplikacja Android bez hosta (Kotlin: core, app)
```

## Komendy

- zależności: `pip install pydantic fastapi uvicorn websockets pytest httpx anthropic`; głos: `pip install faster-whisper piper-tts av numpy`; program na komputer: `pip install sounddevice av numpy` (plus systemowe `libportaudio2`)
- testy: `pytest host/tests`
- serwer: `uvicorn host.api.main:app --reload` (konfiguracja TOML ze zmiennej `IRIS_CONFIG`, bez niej wartości domyślne; baza `iris.db` w bieżącym katalogu)
- uwierzytelnianie: token w zmiennej `IRIS_TOKEN` (16-128 znaków `A-Za-z0-9_-`, np. `python -c "import secrets; print(secrets.token_urlsafe(32))"`), ta sama zmienna dla hosta, symulatora i portu POSIX. REST: nagłówek `Authorization: Bearer $IRIS_TOKEN`; `/ws`: podprotokoły `iris` i `token.$IRIS_TOKEN`. Bez tokenu host nasłuchuje tylko na 127.0.0.1 i odrzuca połączenia przez proxy lub tunel (nagłówki `X-Forwarded-For`, `CF-Connecting-IP` i podobne). Telefon przez tunel HTTPS (Tailscale, Cloudflare): `README.md`
- LLM (domyślnie wyłączony), w pliku z `IRIS_CONFIG`: `[brain.llm]` z `engine = "ollama"` i `model = "..."` albo `engine = "claude"` (model domyślnie `claude-opus-5-5`, klucz w zmiennej `ANTHROPIC_API_KEY`, nie w pliku; opcje: `base_url`, `send_facts`, `max_tokens`, `effort`; lista w `host/core/config.py`)
- głos (domyślnie wyłączony): `[brain.stt]` z `engine = "faster-whisper"` i `model` (nazwa, repozytorium HF albo katalog) oraz `[brain.tts]` z `engine = "piper"` i `voice` (plik `.onnx` z `.onnx.json` obok). Modele Whispera pobiera Hugging Face; głosy Pipera trzeba pobrać ręcznie. Opcje `[brain.audio]`: `error_clip` (klip z ciała odtwarzany po nieudanej turze), `max_speech_s`, `max_pending_turns`. Błąd ładowania modeli wyłącza głos (`/health`: `voice` = `error`), host działa dalej
- uczenie się (spec, sekcja 9): polecenia "zapamiętaj, że ...", "zapomnij ...", "zapomnij wszystko", "co o mnie wiesz" (host, bez LLM); pochwała i krytyka ostatniej odpowiedzi ("świetnie", "to nie tak") zmieniają pewność użytych przypuszczeń (`parse_feedback`, `Learner.feedback`; `[learning] feedback`, `feedback_up`, `feedback_down`); przypuszczenia z rozmów proponuje LLM w osobnym zapytaniu w tle (`host/brain/learn.py`), a reguły (pewność, wzmacnianie, zanikanie, pytania "Czy dobrze rozumiem: ...?", filtr danych wrażliwych) są w `host/core/learning.py` i `host/api/learner.py`; ustawienia `[learning]` w `host/core/config.py`. Treść faktów tylko w tabeli `facts`, nigdy w zdarzeniach ani logach
- samotest toru głosowego na prawdziwych modelach: `python -m host.brain.selftest --config iris.toml` (kod wyjścia 0 przy zgodności transkrypcji)
- rozmowa tekstowa (wymaga `[brain.llm]`, inaczej 501): `curl -s localhost:8000/chat -H 'content-type: application/json' -d '{"text":"Cześć"}'`
- firmware (rdzeń C99 i klient WebSocket, natywnie, bez sprzętu): `make -C firmware test` (ASan i UBSan), `make -C firmware valgrind`, `make -C firmware lint`; testy zgodności z hostem są w `host/tests/test_firmware.py` i `host/tests/test_firmware_posix.py`. Budżet RAM i limity buforów: `firmware/core/fw_config.h`
- ciało w C z prawdziwym hostem (port POSIX): `make -C firmware posix`, potem `firmware/build/hermes --url ws://localhost:8000/ws [--device laptop]` (komendy na stdin jak w symulatorze, `help` je wypisuje; `sayopus plik` wysyła pakiety Opus z pliku: 2 B długości big-endian i dane; `--audio-out katalog` zapisuje odtwarzane audio w tym samym formacie; tylko `ws://`, bez TLS)
- klient w przeglądarce: `http://localhost:8000/app/` (na komputerze z hostem). Ten sam klient, wdrażany z `vercel.json` na `https://iris-asystent.vercel.app/app/`, łączy się z innym hostem (pole "Adres hosta" w ustawieniach); host wymienia taką stronę w `[api] cors_origins` (spec, sekcje 1 i 10). Telefon i inne komputery wymagają HTTPS: `python -m host.api.serve --host 0.0.0.0 --port 8000 --tls-port 8443 --certfile cert.pem --keyfile key.pem` (jeden stan, zwykły port dla HERMES), certyfikat np. `openssl req -x509 -newkey rsa:2048 -nodes -keyout key.pem -out cert.pem -days 365 -subj /CN=iris -addext subjectAltName=IP:<adres hosta>`. Instalacja jako aplikacja (PWA) wymaga certyfikatu zaufanego na telefonie. Testy: `pytest host/tests/test_browser.py` (Node i Playwright z Chromium)
- urządzenia: kilka może być połączonych (`/ws?device=<id>`, bez parametru `hermes`), aktywne jest jedno; `curl -s localhost:8000/devices`, przełączenie `curl -s -X POST localhost:8000/devices/laptop/activate` (z tokenem dodaj `-H "Authorization: Bearer $IRIS_TOKEN"`) albo komenda `activate` w symulatorze i porcie POSIX
- przełączanie głosem albo przez `/chat`: "przełącz na laptop" (host rozpoznaje bez LLM; odmiany nazw w `[devices.aliases]`, np. `pi-kuchnia = ["kuchnię"]`)
- program na komputer i Raspberry Pi: `python -m client.desktop --url ws://<host>:8000/ws --device laptop` (Enter zaczyna i kończy wypowiedź; `--vad` bez rąk, po głośności i bez słowa wybudzającego; `--list-devices`, `--input-device`, `--output-device`; token z `IRIS_TOKEN`)
- aplikacja Android: testy rdzenia `cd android && ./gradlew :core:test` (JDK 17+, Maven Central; Android SDK nie jest potrzebny do `core`). Reguły uczenia w Kotlinie muszą dawać te same wyniki co `host/core/learning.py`: wspólne przypadki są w `android/core/src/test/resources/learning_vectors.json`, generuje je `python -m host.tests.make_android_vectors`, a `host/tests/test_android_vectors.py` wykrywa nieaktualny plik. Po zmianie reguły w Pythonie: regeneracja pliku i poprawka Kotlina w `android/core`. APK: workflow `android` buduje `:app:assembleDebug` i publikuje wydanie `android-latest`; `:app` jest włączany tylko tam, gdzie jest Android SDK (`ANDROID_HOME` albo `android/local.properties`), a logi CI są dostępne przez adnotacje (`gh api repos/<repo>/check-runs/<id>/annotations`)
- symulator ciała: `python -m sim.body --url ws://localhost:8000/ws [--device laptop]` (komendy na stdin, `help` je wypisuje; `say [ms]` wysyła ton 440 Hz, `sayfile plik.wav` nagranie, obie jako Opus (wymagają `av` i `numpy`); scenariusze: `--script sim/scenarios/low_battery.txt`)

## Kolejność prac

1. `docs/spec.md`
2. `host/core`
3. `host/store`
4. `host/api`
5. `sim/`
6. `host/brain`
7. `firmware/`

Kroki 1-5 nie wymagają sprzętu ani LLM.
