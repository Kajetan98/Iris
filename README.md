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
- Możesz ocenić ostatnią odpowiedź jedną frazą. "Świetnie", "dzięki" i podobne podnoszą pewność przypuszczeń użytych w tej odpowiedzi o 10 punktów procentowych. "To nie tak", "źle" i podobne obniżają ją o 20, a bardzo słabe przypuszczenie znika. Fakty podane wprost się nie zmieniają. Działa tylko wtedy, gdy do odpowiedzi trafiły przypuszczenia; ustawienie `feedback = false` to wyłącza.
- O zdrowiu, lekach, adresie, pieniądzach, wyznaniu, poglądach i orientacji IRIS sama się nie uczy; zapisze je tylko po "zapamiętaj". Przypuszczenia wyciąga domyślnie tylko lokalny model (Ollama), nie model w chmurze.
- W aplikacji przycisk z książką otwiera listę "Co IRIS o mnie wie", gdzie każdy fakt możesz usunąć.

Progi i ustawienia: sekcja `[learning]` w `iris.toml` (lista w `host/core/config.py`), np. `infer = "never"` wyłącza przypuszczenia, `enabled = false` całą naukę.

## Telefon: instalacja jako aplikacja

Aplikacja ma stały adres, niezależny od hosta: **https://iris-asystent.vercel.app/app/** (te same pliki co w `host/api/static`, wdrażane z tego repozytorium przez `vercel.json`). Host działa osobno, na Twoim komputerze lub Raspberry Pi, a aplikacja łączy się z nim pod adresem wpisanym w ustawieniach ("Adres hosta"). Dzięki temu instalujesz ją raz, a zmiana adresu tunelu (np. Cloudflare) wymaga tylko poprawienia adresu hosta w ustawieniach.

Przeglądarka daje mikrofon tylko przez HTTPS, więc host też potrzebuje adresu HTTPS. Najprościej przez tunel: host działa na `127.0.0.1:8000`, a tunel wystawia go pod adresem https.

Przy tunelu ustaw `IRIS_TOKEN`. Bez tokenu host przyjmuje tylko połączenia z tego samego komputera: odrzuca nagłówki dodawane przez proxy i tunele, połączenia z innych adresów, obcy nagłówek `Host` (DNS rebinding) i żądania otwarte przez obce strony w przeglądarce (spec, sekcje 1 i 10). `python -m host.api.serve --host 0.0.0.0` bez tokenu nie wystartuje.

### Tailscale (stały adres, dostęp tylko z Twoich urządzeń)

1. Zainstaluj Tailscale na komputerze z hostem i na telefonie, zaloguj oba na to samo konto.
2. W panelu Tailscale włącz HTTPS (certyfikaty dla adresów `ts.net`).
3. Na komputerze: `tailscale serve --bg 8000` (składnię sprawdź w `tailscale serve --help`, zmieniała się między wersjami). Polecenie wypisze adres `https://<komputer>.<tailnet>.ts.net`. Wyłączenie: `tailscale serve off`.

Adres się nie zmienia i jest dostępny tylko z urządzeń zalogowanych do Twojej sieci Tailscale, nie z internetu. `tailscale serve --bg` według dokumentacji Tailscale działa dalej po zamknięciu terminala; czy wraca po restarcie komputera, sprawdź poleceniem `tailscale serve status`.

### Cloudflare Quick Tunnel (szybka próba)

```sh
cloudflared tunnel --url http://localhost:8000
```

Wypisze adres `https://<losowe-słowa>.trycloudflare.com`. Nie wymaga konta, ale adres jest publiczny i przy każdym uruchomieniu inny, więc po każdym uruchomieniu poprawiasz adres hosta w ustawieniach aplikacji.

### Konfiguracja hosta i telefonu

1. W `iris.toml` dopuść stronę aplikacji (bez tego przeglądarka zablokuje jej zapytania do hosta) i uruchom host ponownie:

   ```toml
   [api]
   cors_origins = ["https://iris-asystent.vercel.app"]
   ```

2. Na telefonie otwórz `https://iris-asystent.vercel.app/app/`. W ustawieniach wpisz nazwę urządzenia (np. `telefon`), adres hosta (`https://<komputer>.<tailnet>.ts.net`) i token z `IRIS_TOKEN`, potem "Połącz".
3. Zainstaluj aplikację. Android (Chrome): menu, potem "Zainstaluj aplikację". iPhone (Safari): "Udostępnij", potem "Do ekranu początkowego".
4. Jeśli aktywne jest inne urządzenie, użyj przycisku "Używaj tego urządzenia" albo powiedz "przełącz na telefon".

Hosta nie musisz wystawiać pod adresem aplikacji: `https://<host>/app/` też działa, bez pola "Adres hosta" i bez `cors_origins`.

Własny adres aplikacji: w Vercel utwórz projekt z tego repozytorium (ustawienia budowania są w `vercel.json`), a jego adres wpisz w `cors_origins`.

Bez tunelu, w sieci domowej, host może mieć własny certyfikat (`python -m host.api.serve`, opis w `CLAUDE.md`), ale samopodpisany certyfikat pozwala tylko na kartę przeglądarki, bez instalacji.

## Android: aplikacja bez hosta

Osobna aplikacja na Androida (`android/`, Kotlin i Jetpack Compose) rozmawia bez hosta: mowa i synteza z systemu, model przez Claude API albo uruchamiany na telefonie (bez opłat), fakty o Tobie w bazie SQLite na telefonie. Reguły uczenia się są te same co na hoście (spec, sekcje 9 i 14), a zgodność pilnuje wspólny plik testowy. Nie ma w niej HERMES, przełączania urządzeń ani zdarzeń.

Instalacja:

1. Na telefonie otwórz repozytorium na GitHubie (zalogowany na konto z dostępem), zakładka Releases, wydanie "IRIS na Androida (ostatni build)".
2. Pobierz `iris-debug.apk` i otwórz go. System poprosi o zgodę na instalację aplikacji z tego źródła (przeglądarki albo menedżera plików).
3. W aplikacji wejdź w Ustawienia i wpisz klucz API Claude, potem Zapisz. Klucz utworzysz w Claude Console (`platform.claude.com/settings/keys`, przycisk Create key). Wybierz przy tworzeniu workspace, do którego ma być przypisany: wtedy nic więcej nie trzeba wpisywać. Klucz niepowiązany z workspace wymaga w aplikacji pola "ID workspace" (`wrkspc_...`, kolumna ID na stronie `platform.claude.com/settings/workspaces`). Limit wydatków ustawisz na tej samej stronie (workspace, Spend limits).
4. Dotknij "Mów" i zezwól na mikrofon. Możesz też pisać w polu na dole.

Eksport faktów: ekran "Pamięć", przycisk "Eksportuj do pliku" zapisuje wszystkie fakty do pliku JSON w miejscu, które wskażesz (np. Pobrane albo Dysk Google). Plik jest nieszyfrowany i zawiera Twoje dane osobiste. Wczytywania z pliku jeszcze nie ma.

Hasło wybudzające: w Ustawieniach włącz "Hasło wybudzające", pobierz model (ok. 50 MB, jednorazowo) i zezwól na mikrofon. Potem przy otwartej aplikacji możesz powiedzieć "Hej Iris" albo "Irysku" zamiast dotykać Mów. Rozpoznawanie hasła jest offline na telefonie (Vosk), nic nie jest wysyłane ani zapisywane. Domyślnie aplikacja musi być na ekranie (ekran nie wygasa). Przełącznik "Słuchaj też w tle" uruchamia usługę ze stałym powiadomieniem "IRIS słucha hasła": hasło działa wtedy po zamknięciu aplikacji i przy wyłączonym ekranie, odpowiedź w tle jest tylko głosowa, a bateria schodzi szybciej (Android pokazuje też wskaźnik mikrofonu). Usługę trzeba uruchomić z otwartej aplikacji; jeśli system ją zabije (oszczędzanie baterii), otwórz IRIS ponownie, a na niektórych telefonach wyłącz dla IRIS optymalizację baterii. Hasło zużywa baterię i czasem się myli. Gdy pobieranie modelu się nie uda, pobierz `vosk-model-small-pl-0.22.zip` z alphacephei.com/vosk/models i wskaż plik przyciskiem "Wybierz plik .zip". Mów po tym, jak aplikacja zacznie słuchać; słowa tuż po haśle mogą zginąć.

Aktualizacje: od wersji z tą funkcją aplikacja sama sprawdza przy starcie wydanie `android-latest`. Gdy jest nowsza wersja, nad falą pojawia się "Dostępna aktualizacja" z przyciskiem Zainstaluj (też w Ustawieniach: Sprawdź aktualizacje). Aplikacja pobiera plik, sprawdza jego skrót SHA-256 i otwiera instalator systemu; jedno dotknięcie "Zainstaluj" w oknie systemu zostaje. Przy pierwszej aktualizacji system zapyta o zgodę dla IRIS na instalowanie aplikacji. Wersję z tą funkcją trzeba zainstalować ręcznie z linku, dopiero kolejne aktualizują się same. Sprawdzanie można wyłączyć w Ustawieniach.

Model na telefonie (bez konta, klucza i opłat): w Ustawieniach wybierz "Model na telefonie" i wskaż plik `.litertlm` (format LiteRT-LM), pobrany wcześniej na telefon z Hugging Face, z organizacji `litert-community` (np. `Qwen3-0.6B`, mały, albo `gemma-4-E2B-it-litert-lm`, ok. 2,6 GB). Niektóre modele wymagają zalogowania i akceptacji licencji. Aplikacja kopiuje plik do swojej pamięci, więc potrzeba tyle wolnego miejsca, ile zajmuje model. Wymagany jest 64-bitowy telefon (arm64) z kilkoma GB pamięci RAM. Rozmowa, fakty i wnioski zostają wtedy na telefonie. Mały model odpowiada wyraźnie gorzej niż Claude, zwłaszcza po polsku. Ta ścieżka nie była uruchamiana na prawdziwym telefonie w czasie pisania; jeśli model się nie wczyta, aplikacja pokaże powód pod odpowiedzią.

Przy silniku Claude wychodzi z telefonu: wypowiedź, kilka ostatnich wymian i czas lokalny trafiają do Anthropic. Fakty o Tobie trafiają tam tylko po włączeniu "Używaj pamięci w rozmowie", a wnioski z rozmów powstają tylko po włączeniu "Ucz się z rozmów" (oba przełączniki są domyślnie wyłączone). Rozpoznawanie mowy zwykle wysyła nagranie do usługi systemu (Google), chyba że telefon ma pakiet offline i włączysz "Preferuj rozpoznawanie offline". Klucz API jest zaszyfrowany kluczem z Android Keystore i nie trafia do kopii zapasowej. Polecenia "zapamiętaj, że ...", "zapomnij ...", "co o mnie wiesz" działają lokalnie i bez klucza.

Wymagania i ograniczenia: Android 8.0 lub nowszy, polski głos w systemie do czytania odpowiedzi (bez niego są tylko napisy). Fala podczas mowy porusza się według stałego wzoru, bo synteza systemowa nie udostępnia dźwięku. Aplikacja nie była uruchamiana na prawdziwym telefonie przez autora kodu: pierwsza wersja może wymagać poprawek.

Podpis: klucz debug jest publiczny, więc po dodaniu w ustawieniach repozytorium sekretów `IRIS_RELEASE_KEYSTORE` i `IRIS_RELEASE_PASSWORD` CI podpisuje APK też kluczem prywatnym, z rotacją klucza (Android 9+ instaluje taką wersję na starą bez utraty danych). Instrukcja tworzenia klucza i zasady: [docs/spec.md](docs/spec.md), sekcja 14, "Podpis". Kopię pliku klucza i hasła trzymaj poza repozytorium; bez nich kolejnych wersji nie da się zainstalować na starą.

Budowanie: APK buduje CI (workflow `android`) i publikuje w wydaniu `android-latest`. Plik jest podpisany kluczem debug z repozytorium (`android/app/debug.keystore`), więc każda nowa wersja instaluje się na poprzednią bez utraty faktów. To klucz do testów, nie wydawniczy. Testy rdzenia lokalnie: `cd android && ./gradlew :core:test` (JDK 17). Moduł `app` wymaga Android SDK i bez niego jest pomijany.

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

