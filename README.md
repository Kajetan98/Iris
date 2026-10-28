<p align="center">
  <img src="docs/brand/iris-icon.png" width="128" alt="">
</p>

<p align="center">
  <img src="docs/brand/iris-header.png" height="72" alt="IRIS">
</p>

Asystent głosowy po polsku. Host w Pythonie trzyma stan, rozmawia przez LLM i obsługuje mowę (rozpoznawanie i synteza). Urządzeniem, przez które się z nim rozmawia, może być ciało HERMES (STM32L476), przeglądarka na telefonie lub komputerze albo program na komputer i Raspberry Pi. Aktywne jest jedno urządzenie naraz, przełączane przyciskiem lub głosem ("przełącz na laptop").

Szczegóły protokołu i modelu stanu: [docs/spec.md](docs/spec.md).

## Uruchomienie hosta

Wymaga Pythona 3.11 lub nowszego.

```sh
git clone https://github.com/Kajetan98/Iris.git iris && cd iris
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[voice]"            # host z mową; bez mowy: pip install -e .
cp iris.example.toml iris.toml       # konfiguracja: LLM, rozpoznawanie i synteza mowy
export IRIS_TOKEN=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
export IRIS_CONFIG=iris.toml
uvicorn host.api.main:app --host 127.0.0.1 --port 8000
```

`iris.example.toml` ustawia lokalny LLM (Ollama), Whisper do rozpoznawania mowy i polski głos Pipera. Głos (plik `.onnx` z `.onnx.json` obok) trzeba pobrać ręcznie do katalogu `voices/`. `iris.toml`, `voices/`, certyfikaty i baza nie trafiają do repozytorium (`.gitignore`).

Zamiast Ollamy można użyć Claude API: `engine = "claude"`, klucz w zmiennej `ANTHROPIC_API_KEY` (nigdy w pliku). Wszystkie opcje: `host/core/config.py`. Bez `[brain.stt]` i `[brain.tts]` host działa, ale nie rozmawia głosem. Sprawdzenie toru głosowego na prawdziwych modelach: `python -m host.brain.selftest --config iris.toml`.

Na komputerze z hostem klient otwiera się pod `http://localhost:8000/app/`.

### Na stałe (systemd)

Żeby aplikacja na telefonie działała po każdym włączeniu komputera lub Raspberry Pi, host może działać jako usługa użytkownika: `deploy/iris.service` (instrukcja w komentarzu na początku pliku). Token trzyma wtedy plik `~/.config/iris/env` z prawami tylko dla właściciela. `tailscale serve --bg` według dokumentacji Tailscale działa dalej po zamknięciu terminala; czy wraca po restarcie komputera, sprawdź poleceniem `tailscale serve status`.

## Jak IRIS się uczy

Model językowy się nie zmienia. IRIS uczy się faktów o Tobie, które host zapisuje w bazie i dodaje do kontekstu rozmowy (zasady: [docs/spec.md](docs/spec.md), sekcja 9, "Uczenie się"):

- Mówisz wprost: "zapamiętaj, że w piątki gram w kosza". Fakt jest pewny od razu. "Zapomnij, że ..." go usuwa, "co o mnie wiesz?" wymienia, co IRIS wie, a "zapomnij wszystko" usuwa wszystko po potwierdzeniu. Te polecenia obsługuje host, także bez LLM.
- IRIS wyciąga przypuszczenia z rozmów: po każdej odpowiedzi model w tle proponuje fakty z Twojej wypowiedzi ("Masz psa o imieniu Burek."). Nowe przypuszczenie jest słabe (45%) i nie trafia jeszcze do rozmowy. Gdy się powtórzy, zyskuje pewność. Przypuszczenie, które przez 30 dni się nie powtórzy, słabnie, a bardzo słabe znika.
- IRIS pyta o przypuszczenia: dopisuje do odpowiedzi "Czy dobrze rozumiem: masz psa o imieniu Burek?". "Tak" zmienia je w pewny fakt, "nie" je usuwa, a "nie, mam kota" zapisuje poprawkę. Najwyżej jedno pytanie na 15 minut, żadnych w trybie ciszy.
- O zdrowiu, lekach, adresie, pieniądzach, wyznaniu, poglądach i orientacji IRIS sama się nie uczy; zapisze je tylko po "zapamiętaj". Przypuszczenia wyciąga domyślnie tylko lokalny model (Ollama), nie model w chmurze.
- W aplikacji przycisk z książką otwiera listę "Co IRIS o mnie wie", gdzie każdy fakt możesz usunąć.

Progi i ustawienia: sekcja `[learning]` w `iris.toml` (lista w `host/core/config.py`), np. `infer = "never"` wyłącza przypuszczenia, `enabled = false` całą naukę.

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

- Program na komputer i Raspberry Pi: `python -m client.desktop --url ws://<host>:8000/ws --device laptop` (instalacja: `pip install -e ".[desktop]"` i systemowe `libportaudio2`).
- Symulator ciała: `python -m sim.body --url ws://localhost:8000/ws`.
- Firmware HERMES i jego port na komputer: katalog `firmware/`, `make -C firmware test`.

HERMES łączy się przez zwykły `ws://` (bez TLS), więc host musi być dla niego osiągalny w sieci lokalnej (`--host 0.0.0.0`, zawsze z tokenem).

## Testy

```sh
pip install -e ".[dev,voice,desktop]"
pytest host/tests
make -C firmware test
```

Testy przeglądarki (`host/tests/test_browser.py`) wymagają Node i Playwright z Chromium; bez nich są pomijane. Na GitHubie te same testy uruchamia `.github/workflows/tests.yml` przy każdym push (Python 3.11 i 3.13, Chromium, firmware z ASan, UBSan i valgrindem).

## Dane i bezpieczeństwo

- Treść rozmowy, nagrania i transkrypcje nie są zapisywane w bazie ani w logach. Napisy na urządzeniu znikają po zamknięciu strony.
- Token i klucz API pochodzą tylko ze zmiennych środowiskowych i nie trafiają do logów ani do bazy.
- Do LLM w chmurze trafia tylko wycinek stanu potrzebny do zapytania.
- `piper-tts` ma licencję GPL-3.0-or-later; sprawdź zgodność, zanim zaczniesz rozpowszechniać hosta.

## Logo

Pliki w `docs/brand/`: `iris-icon.png` (ikona aplikacji, 1024×1024, przezroczyste rogi), `iris-wordmark` i `iris-wordmark-white` w wersji SVG i PNG (napis IRIS na jasne i ciemne tło). `iris-header.png` to napis na ciemnej pastylce, czytelny w obu motywach GitHuba; README używa go w nagłówku, bez przełączania motywu. Ikony klienta w przeglądarce (`host/api/static/icon-*.png`, `apple-touch-icon.png`) powstały z tej samej ikony.

