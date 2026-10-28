# IRIS

Asystent głosowy po polsku. Host w Pythonie trzyma stan, rozmawia przez LLM i obsługuje mowę (rozpoznawanie i synteza). Urządzeniem, przez które się z nim rozmawia, może być ciało HERMES (STM32L476), przeglądarka na telefonie lub komputerze albo program na komputer i Raspberry Pi. Aktywne jest jedno urządzenie naraz, przełączane przyciskiem lub głosem ("przełącz na laptop").

Szczegóły protokołu i modelu stanu: [docs/spec.md](docs/spec.md).

## Uruchomienie hosta

Wymaga Pythona 3.11 lub nowszego.

```sh
pip install pydantic fastapi uvicorn websockets httpx anthropic
pip install faster-whisper piper-tts av numpy        # mowa
export IRIS_TOKEN=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
export IRIS_CONFIG=iris.toml
uvicorn host.api.main:app --host 127.0.0.1 --port 8000
```

Przykładowy `iris.toml` z mową i lokalnym LLM (Ollama):

```toml
[brain.llm]
engine = "ollama"
model = "llama3.1"   # przykład, dowolny model pobrany w Ollamie

[brain.stt]
engine = "faster-whisper"
model = "small"

[brain.tts]
engine = "piper"
voice = "voices/pl_PL-gosia-medium.onnx"   # plik .onnx z .onnx.json obok, pobrany ręcznie
```

Zamiast Ollamy można użyć Claude API: `engine = "claude"`, klucz w zmiennej `ANTHROPIC_API_KEY` (nigdy w pliku). Wszystkie opcje: `host/core/config.py`. Bez `[brain.stt]` i `[brain.tts]` host działa, ale nie rozmawia głosem. Sprawdzenie toru głosowego na prawdziwych modelach: `python -m host.brain.selftest --config iris.toml`.

Na komputerze z hostem klient otwiera się pod `http://localhost:8000/app/`.

## Telefon: instalacja jako aplikacja

Przeglądarka daje mikrofon tylko przez HTTPS, a instalacja na ekranie początkowym wymaga certyfikatu, któremu telefon ufa. Najprościej dać hostowi adres HTTPS przez tunel. Host działa dalej na komputerze (albo Raspberry Pi) na `127.0.0.1:8000`. Strona statyczna, np. GitHub Pages, tu nie wystarczy, bo aplikacja rozmawia z hostem, który musi gdzieś działać.

Przy tunelu ustaw `IRIS_TOKEN`. Bez tokenu host odrzuca połączenia z nagłówkami dodawanymi przez proxy i tunele (spec, sekcja 1).

### Tailscale (stały adres, dostęp tylko z Twoich urządzeń)

1. Zainstaluj Tailscale na komputerze z hostem i na telefonie, zaloguj oba na to samo konto.
2. W panelu Tailscale włącz HTTPS (certyfikaty dla adresów `ts.net`).
3. Na komputerze: `tailscale serve --bg 8000` (składnię sprawdź w `tailscale serve --help`, zmieniała się między wersjami). Polecenie wypisze adres `https://<komputer>.<tailnet>.ts.net`. Wyłączenie: `tailscale serve off`.

Adres się nie zmienia, więc zainstalowana aplikacja działa po każdym uruchomieniu hosta. Adres jest dostępny tylko z urządzeń zalogowanych do Twojej sieci Tailscale, nie z internetu.

### Cloudflare Quick Tunnel (szybka próba)

```sh
cloudflared tunnel --url http://localhost:8000
```

Wypisze adres `https://<losowe-słowa>.trycloudflare.com`. Nie wymaga konta, ale adres jest publiczny i przy każdym uruchomieniu inny. Zainstalowana aplikacja jest związana z adresem, więc po zmianie trzeba ją zainstalować od nowa.

### Na telefonie

1. Otwórz `https://<adres>/app/`.
2. Wpisz nazwę urządzenia (np. `telefon`) i token z `IRIS_TOKEN`, potem "Połącz".
3. Android (Chrome): menu, potem "Zainstaluj aplikację". iPhone (Safari): "Udostępnij", potem "Do ekranu początkowego".
4. Jeśli aktywne jest inne urządzenie, użyj przycisku "Używaj tego urządzenia" albo powiedz "przełącz na telefon".

Bez tunelu, w sieci domowej, host może mieć własny certyfikat (`python -m host.api.serve`, opis w `CLAUDE.md`), ale samopodpisany certyfikat pozwala tylko na kartę przeglądarki, bez instalacji.

## Inne urządzenia

- Program na komputer i Raspberry Pi: `python -m client.desktop --url ws://<host>:8000/ws --device laptop` (wymaga `sounddevice` i systemowego `libportaudio2`).
- Symulator ciała: `python -m sim.body --url ws://localhost:8000/ws`.
- Firmware HERMES i jego port na komputer: katalog `firmware/`, `make -C firmware test`.

HERMES łączy się przez zwykły `ws://` (bez TLS), więc host musi być dla niego osiągalny w sieci lokalnej (`--host 0.0.0.0`, zawsze z tokenem).

## Testy

```sh
pip install pytest
pytest host/tests
make -C firmware test
```

Testy przeglądarki (`host/tests/test_browser.py`) wymagają Node i Playwright z Chromium; bez nich są pomijane.

## Dane i bezpieczeństwo

- Treść rozmowy, nagrania i transkrypcje nie są zapisywane w bazie ani w logach. Napisy na urządzeniu znikają po zamknięciu strony.
- Token i klucz API pochodzą tylko ze zmiennych środowiskowych i nie trafiają do logów ani do bazy.
- Do LLM w chmurze trafia tylko wycinek stanu potrzebny do zapytania.
- `piper-tts` ma licencję GPL-3.0-or-later; sprawdź zgodność, zanim zaczniesz rozpowszechniać hosta.
