# IRIS: specyfikacja protokołu i modelu stanu (v0.1, szkic)

## 1. Transport

- WebSocket, endpoint `/ws?device=<id>`. Jedno połączenie na urządzenie (ciało); urządzeń może być kilka (niżej, "Urządzenia").
- Ramki tekstowe: JSON UTF-8. Ramki binarne: audio (sekcja 6).
- Heartbeat: ciało wysyła `body.heartbeat` co 1 s. Brak heartbeatu przez 5 s oznacza `link.lost` (wartości początkowe).
- Wersja protokołu w polu `v`. Zmiana niezgodna wstecz podnosi `v`.
- Nowe połączenie urządzenia zastępuje jego poprzednie połączenie: host zamyka stare z kodem 4000 (ciało po zerwaniu łącza łączy się ponownie, a stare gniazdo może być jeszcze otwarte po stronie hosta). Połączenia różnych urządzeń działają obok siebie.
- Ciało łączy się przez `ws://` albo, gdy ma TLS, przez `wss://` (firmware HERMES: tylko `ws://`, sekcja 13). Po zerwaniu łącza, także po zamknięciu z kodem od hosta (4000-4003), ponawia połączenie po 0,5 s, z opóźnieniem podwajanym do 5 s. Udane połączenie zeruje opóźnienie.
- Utrzymanie łącza po stronie ciała: ping WebSocket po 5 s bez danych od hosta, zerwanie połączenia po 10 s bez danych (wartości początkowe). Ciało odpowiada pong na ping hosta (uvicorn wysyła go domyślnie co 20 s).
- Wiadomość od hosta większa niż bufor łącza ciała (tekst do 512 B, sekcja 5; binarna do 5 B nagłówka i 1275 B pakietu Opus) ciało odrzuca jak uszkodzoną ramkę i liczy w `frame_errors`. Połączenie zostaje.
- Odrzucona ramka nie zamyka połączenia (sekcja 11).
- Uwierzytelnianie: gdy host ma token (zmienna środowiskowa `IRIS_TOKEN`, 16-128 znaków `A-Z`, `a-z`, `0-9`, `_`, `-`; inna wartość zatrzymuje start hosta), urządzenie oferuje w handshake podprotokoły `iris` i `token.<token>` (nagłówek `Sec-WebSocket-Protocol`; przeglądarka nie może ustawić innych nagłówków, a adres z tokenem trafiłby do logów). Host wybiera `iris`, także bez tokenu. Brak lub zły token: host przyjmuje połączenie i od razu zamyka je z kodem 4003. Token nie trafia do logów ani do bazy. Bez tokenu host przyjmuje tylko połączenia bezpośrednie: połączenie z nagłówkiem dodawanym przez proxy lub tunel (`Forwarded`, `X-Forwarded-For`, `X-Real-IP`, `CF-Connecting-IP`, `True-Client-IP`) zamyka kodem 4003, bo tunel łączy się z hostem przez 127.0.0.1 i ominąłby zasadę nasłuchiwania tylko na tym adresie.
- Akcje host wysyła jako ramki tekstowe. `id` ma postać `h<licznik>` (jeden licznik dla wszystkich urządzeń), `ts` to milisekundy od startu hosta (zegar monotoniczny).

### Urządzenia

IRIS może działać przez różne urządzenia: HERMES, przeglądarkę, program na komputerze, Raspberry Pi, telefon. Połączonych może być kilka naraz, ale słucha i mówi tylko jedno, aktywne. Pozostałe są połączone w spoczynku.

- `device` to identyfikator urządzenia: 1-32 znaki `a-z`, `0-9` i `-`, pierwszy znak to litera lub cyfra (np. `hermes`, `laptop`, `pi-kuchnia`). Brak parametru oznacza `hermes` (zgodność z wersją z jednym ciałem).
- Kody zamknięcia od hosta: 4000 (zastąpione przez nowe połączenie tego samego urządzenia), 4001 (połączonych jest już `devices.max_connected` urządzeń, wartość początkowa 8), 4002 (niepoprawny `device`), 4003 (brak lub zły token). Przy 4001-4003 host zamyka połączenie zaraz po jego przyjęciu.
- Aktywne jest najwyżej jedno urządzenie. Host pamięta wybór między uruchomieniami (sekcja 9). Gdy żadne nie jest wybrane, aktywne staje się pierwsze urządzenie, które się połączy.
- Przełączenie jest ręczne: `POST /devices/{id}/activate` (sekcja 10) albo zdarzenie `device.activate` z urządzenia, które ma zostać aktywne (np. przycisk "używaj tego urządzenia" w przeglądarce). Można wybrać urządzenie znane, ale niepołączone; IRIS jest wtedy w trybie `offline`, dopóki się nie połączy.
- Komenda przełączenia: wypowiedź albo tekst `POST /chat` w postaci "przełącz (się) na X", "przejdź na X" lub "zmień urządzenie na X" (wielkość liter, interpunkcja na końcu i słowo "IRIS" na początku nie mają znaczenia). Host rozpoznaje ją sam, przed LLM i bez niego. X pasuje do urządzenia, gdy jest równe jego `id` (z `-` jako spacją), jednej z nazw w konfiguracji `devices.aliases` (np. `laptop = ["komputer", "laptopa"]`) albo `id` z polską końcówką `a`, `u`, `em`, `ie`, `owi`, `y`. Znane urządzenie: przełączenie (`by: "voice"`) i odpowiedź "Przełączam na X.", którą IRIS mówi na nowym urządzeniu, jeśli jest połączone. Nieznana albo niejednoznaczna nazwa: odpowiedź z listą znanych urządzeń, bez zmiany.
- Po każdym połączeniu urządzenie uznaje się za aktywne, dopóki nie dostanie akcji `device.active` z `active: false`. Host wysyła ją zaraz po połączeniu urządzenia nieaktywnego. Przy przełączeniu wysyła `false` do poprzedniego i `true` do nowego urządzenia, jeśli są połączone.
- Zdarzenia z urządzenia nieaktywnego host zapisuje i uwzględnia w jego stanie na liście urządzeń (`GET /devices`), ale nie wpływają one na `body`, drives, interakcje ani rozmowę. Wyjątkiem jest `device.activate`. Audio z urządzenia nieaktywnego host pomija, a urządzenie nieaktywne nie powinno go wysyłać.
- Możliwości urządzenia opisuje `device.caps`. Host pamięta ostatnie dla każdego urządzenia do końca sesji. Bez `device.caps` host przyjmuje możliwości HERMES: wszystkie z listy `has`, dowolne klipy i bufor odtwarzania z konfiguracji (`body_buffer_bytes`, `body_max_packets`, sekcja 6). Brak pola w `device.caps` oznacza: `has` i `clips` puste, bufor z konfiguracji.
- Host dopasowuje odpowiedź głosową do mniejszego z buforów: z konfiguracji i z `playback_bytes`/`playback_packets`. Bez `speaker` w `has` host nie syntezuje odpowiedzi na wypowiedź. Klip błędu wysyła tylko, gdy jego nazwa jest w `clips`. Akcję, której urządzenie nie obsługuje (np. `gpio.set` w przeglądarce), urządzenie potwierdza z `err: "unsupported"`.
- Akcje hosta (odpowiedzi głosowe, klip błędu, `POST /actions` bez `device`) trafiają do aktywnego urządzenia. Odpowiedź na wypowiedź trafia na urządzenie, z którego wypowiedź przyszła, o ile jest ono nadal aktywne. Po przełączeniu w trakcie tury odpowiedź przepada, bez klipu błędu.

### Klient w przeglądarce

Host serwuje klienta pod `/app/` (pliki publiczne, sekcja 10). Ten sam klient działa na komputerze i na telefonie, gdzie można go zainstalować jako aplikację (PWA).

- Wymaga bezpiecznego kontekstu (`localhost` albo HTTPS), bo bez niego przeglądarka nie daje mikrofonu ani WebCodecs. Mowę koduje i dekoduje przez WebCodecs (Opus, 16 kHz, mono, pakiety 20 ms, 24 kbit/s), jak opisuje sekcja 6.
- Mówi się z przyciskiem: wciśnięcie wysyła `audio.utterance.start`, puszczenie kończy wypowiedź (`audio.utterance.end`, najwyżej 60 s). Sekundę po połączeniu klient sprawdza `GET /health` (z tokenem); przy `voice` równym `disabled` albo `error` przycisk jest wyłączony, bo wypowiedź nie dostałaby odpowiedzi. Bez napisu `nothing`, `failed` ani odpowiedzi w 30 s klient kończy turę z uwagą, że host nie odpowiedział. Urządzenie, które przestało być aktywne w trakcie tury, kończy ją od razu (host pomija jego odpowiedź); odpowiedź już odtwarzana gra do końca.
- `device.caps`: `mic`, `speaker`, `display`, `haptic` (gdy przeglądarka ma wibracje), `battery` (gdy podaje poziom baterii), bez klipów. Napisy (sekcja 6) pokazuje pod falą głosu: rozpoznaną wypowiedź i słowa IRIS odsłaniane w tempie odtwarzania (według liczby znaków, więc w przybliżeniu). `gpio.set` i `haptic` bez wibracji dają `unsupported`, `speak.clip` daje `unknown_clip`. Pozostałe reguły `ack` jak w firmware.
- Heartbeat bez `link_rssi`. Przeglądarka nie może wysyłać pingów WebSocket, więc zerwane łącze wykrywa dopiero przy błędzie wysyłki albo po pingu hosta. Dlatego strona, która wraca na ekran po co najmniej 20 s w tle, łączy się od nowa: telefon mógł w tym czasie zerwać połączenie bez zdarzenia zamknięcia. Nowe połączenie zastępuje stare (4000).
- Nazwę urządzenia i token klient trzyma w pamięci przeglądarki (`localStorage`) na tym urządzeniu.
- Adres hosta: pole "Adres hosta" w ustawieniach. Puste oznacza host, z którego klient został pobrany (ten sam adres). Wypełnione (np. `https://iris.example.ts.net`) kieruje na ten host wszystkie połączenia klienta: `/ws` (`wss://`), `/health` i `/facts`. Adres musi być `https://` (bez ścieżki, zapytania i danych logowania); `http://` jest przyjmowany tylko dla `localhost`, `127.0.0.1` i `[::1]`, a gdy klient sam jest pobrany przez HTTPS, tylko adres `https://`, bo przeglądarka blokuje połączenia niezaszyfrowane ze strony HTTPS. Adres trafia do `localStorage` obok nazwy urządzenia i tokenu.
- Klient to zwykłe pliki statyczne (`host/api/static`) i można go serwować spoza hosta, np. z Vercel, dzięki czemu adres do instalacji jako aplikacja jest stały, niezależnie od adresu tunelu do hosta. Manifest i service worker używają adresów względnych, więc działają pod `/app/` hosta i pod głównym adresem takiej strony. Host odpowiada klientowi z innego adresu tylko przy `api.cors_origins` (sekcja 10).
- Okno "Co IRIS o mnie wie" pokazuje fakty z `GET /facts` (tekst, "od ciebie" albo przypuszczenie z pewnością w procentach, fakty, które nie trafiają do kontekstu LLM, oznaczone jako jeszcze nieużywane w rozmowie) i usuwa je przez `DELETE /facts/{id}`, z tokenem w nagłówku. Lista nie jest zapisywana w przeglądarce.
- HTTPS: `python -m host.api.serve` uruchamia host na zwykłym porcie (dla HERMES) i drugim porcie z TLS (dla przeglądarek), z tym samym stanem. Instalacja jako aplikacja wymaga certyfikatu, któremu telefon ufa; z samopodpisanym klient działa w karcie po akceptacji ostrzeżenia.

### Program na komputer i Raspberry Pi

`python -m client.desktop` (katalog `client/`) używa mikrofonu i głośnika systemu przez PortAudio.

- Wypowiedź zaczyna i kończy Enter. Z `--vad` wypowiedź zaczyna się po trzech ramkach głośniejszych niż próg (wartość początkowa -40 dBFS) i kończy po 800 ms ciszy (`--vad-threshold`, `--vad-end-ms`), z 200 ms dźwięku sprzed startu. Nie ma słowa wybudzającego, więc w tym trybie IRIS odpowiada na każdą wykrytą mowę.
- `device.caps`: `mic`, `speaker`, `display` (napisy wypisywane w terminalu) i `battery` tylko wtedy, gdy system podaje poziom baterii (Linux: `/sys/class/power_supply`). Heartbeat bez `link_rssi`, bez `body.sensor`: program nie wysyła danych, których nie mierzy.
- `haptic` i `gpio.set` dają `unsupported`, `speak.clip` daje `unknown_clip`. Odtwarzanie ma jeden bufor jak w firmware (`busy`).

## 2. Koperta

```
{"v":1,"t":"event|action|ack","id":"<string>","ts":<int>,"type":"<string>","body":{...}}
```

- `id`: unikalny w obrębie nadawcy (licznik lub krótki ciąg).
- `ts`: dla ciała milisekundy od startu (zegar monotoniczny). Host dodaje czas zegarowy przy zapisie.
- `boot_id`: ciało wysyła w `system.boot`. Host łączy nim `ts` z czasem zegarowym.
- `ack`: odpowiedź na `action`. W `body`: `ref` (id akcji), `ok` (bool), opcjonalnie `err`. Pole `type` powtarza typ akcji.
- Pola nieznane w kopercie i w `body` są pomijane (zmiana zgodna wstecz). Brak wymaganego pola oznacza odrzucenie ramki.

## 3. Zdarzenia (ciało → host)

| type | body | uwagi |
|---|---|---|
| `system.boot` | `boot_id`, `fw`, `reset_reason` | wysyłane po starcie |
| `body.heartbeat` | `batt_pct`, `link_rssi`, `frame_errors` (wszystkie opcjonalne) | co 1 s; `frame_errors` to licznik odrzuconych ramek (sekcja 11); urządzenie bez baterii pomija `batt_pct`, bez pomiaru sygnału (przeglądarka) pomija `link_rssi` |
| `body.sensor` | `temp_c`, `imu`, `presence`, `gps` (pola opcjonalne) | co zmianę lub co N s |
| `body.fault` | `sensor`, `code` | sensor niedostępny lub błędny |
| `body.fault.clear` | `sensor` | sensor znów działa |
| `audio.utterance.start` | `utt_id` | po wake word |
| `audio.utterance.end` | `utt_id`, `dur_ms` | po VAD |
| `local.command` | `cmd`, `result` | komenda obsłużona lokalnie na STM32 |
| `button` | `id`, `kind` (`press`, `long`) | |
| `device.caps` | `has` (lista: `mic`, `speaker`, `haptic`, `gpio`, `battery`, `display`), `clips` (lista nazw klipów), `playback_bytes`, `playback_packets` (pola opcjonalne) | możliwości urządzenia (sekcja 1); wysyłane po każdym zestawieniu połączenia, przed zaległymi zdarzeniami, bez buforowania |
| `device.activate` | – | urządzenie prosi o przełączenie na siebie (sekcja 1); liczy się jako interakcja; bez łącza przepada (nie jest buforowane), bo spóźnione przełączyłoby IRIS nieoczekiwanie |

## 4. Zdarzenia hosta (wewnętrzne, widoczne w `/events`)

`link.lost`, `link.restored`, `drive.enter`, `drive.exit`, `host.start`, `host.stop`, `action.sent`, `action.ack`, `device.switch`, `fact.learn`, `fact.confirm`, `fact.forget`, `fact.ask`.

- `device.switch`: `from` (poprzednie aktywne urządzenie albo `null`), `to`, `by` (`first`: pierwsze połączone urządzenie, `rest`, `device`: zdarzenie `device.activate`, `voice`: komenda przełączenia, sekcja 1).
- Zdarzenia hosta mają w zapisie `device_id` urządzenia, którego dotyczą: `link.*` i `action.*` urządzenia, które je wywołało, `drive.*` (poza `long_silence`) aktywnego urządzenia. Pozostałe mają `null`.

- `action.sent`: `id`, `type`, `body`. Zapisywane po wysłaniu akcji do ciała.
- `action.ack`: `ref`, `ok`, `err`. Zapisywane po odebraniu `ack` od ciała.
- `fact.learn`: `id`, `source`, `confidence`, `how` (`told`: polecenie "zapamiętaj", `corrected`: poprawka po "nie, ...", `inferred`: nowy wniosek z rozmowy, `reinforced`: powtórzony wniosek). `fact.confirm`: `id` (odpowiedź "tak" na pytanie IRIS). `fact.forget`: `id` (albo `null` i `count` przy `why: "all"`), `why` (`user`: polecenie "zapomnij" lub `DELETE /facts`, `denied`: odpowiedź "nie", `decay`: zanik, `all`: "zapomnij wszystko" po potwierdzeniu). `fact.ask`: `id`. Zdarzenia `fact.*` nie zawierają treści faktu (sekcja 9).

## 5. Akcje (host → ciało)

| type | body | uwagi |
|---|---|---|
| `speak` | `audio_id` | odtworzenie audio wysłanego binarnie |
| `speak.clip` | `clip` | gotowy klip zapisany w ciele |
| `haptic` | `pattern` (`attention`, `warn`, `critical`) | |
| `gpio.set` | `pin`, `value` | tylko piny z białej listy |
| `config.set` | `key`, `value` | progi lokalne, głośność |
| `ping` | – | odpowiedź `ack` |
| `device.active` | `active` (bool) | czy urządzenie jest aktywne (sekcja 1) |
| `display` | `turn`, `kind` (`heard`, `reply`, `nothing`, `failed`), `text`, `truncated` | napisy tury głosowej (sekcja 6); tylko dla urządzeń z `display` |

Ciało odrzuca akcję spoza listy i odsyła `ack` z `ok:false`.

- Ramka tekstowa od hosta do ciała ma najwyżej 512 bajtów (UTF-8), a do urządzenia z `display` w `device.caps` najwyżej 4096 bajtów. Host nie wysyła większych (`POST /actions` daje 422). Ciało większe odrzuca bez `ack` i zwiększa `frame_errors`.
- `config.set` zna klucze `volume` (0-100), `batt_warn_pct` (0-100) i `temp_warn_c` (liczba). Inne klucze dają `unknown_key`.
- `err` w `ack` to krótki kod ASCII (najwyżej 32 znaki):

| kod | znaczenie |
|---|---|
| `unknown_action` | typ spoza tabeli |
| `bad_body` | brak pola albo pole złego typu |
| `bad_value` | wartość spoza dozwolonego zbioru lub zakresu |
| `pin_not_allowed` | pin spoza białej listy |
| `unknown_key` | klucz `config.set` nieznany ciału |
| `unknown_clip` | klip, którego ciało nie ma |
| `no_audio` | `speak` bez odebranych ramek tego `audio_id` |
| `busy` | trwa odtwarzanie innego `audio_id` |
| `unsupported` | urządzenie nie ma sprzętu do tej akcji (sekcja 1) |

## 6. Audio

- Kodek: Opus, ramki binarne. Nagłówek ramki: `kind` (1 B: 1 = mikrofon, 2 = odtwarzanie), `id` (2 B), `seq` (2 B), dane.
- Mikrofon: strumień między `audio.utterance.start` a `audio.utterance.end`.
- Odtwarzanie: host wysyła ramki z `kind=2`, potem akcję `speak` z `audio_id`.
- Pola wielobajtowe nagłówka (5 bajtów) są zapisane w kolejności big-endian.
- Od ciała host przyjmuje tylko `kind=1`. Inne wartości `kind` i ramki krótsze niż nagłówek są odrzucane (sekcja 11).

Mikrofon (ciało → host):

- Jedna ramka binarna to jeden pakiet Opus. `id` ramki to młodsze 16 bitów `utt_id` z `audio.utterance.start` (`utt_id` musi więc być liczbą), a `seq` rośnie od 0 co pakiet.
- Host dekoduje pakiety niezależnie od częstotliwości próbkowania zakodowanej przez ciało (dekoder zwraca 48 kHz) i sprowadza dźwięk do 16 kHz mono na potrzeby rozpoznawania mowy.
- Wypowiedź jest składana od `audio.utterance.start` do `audio.utterance.end`. Ramki, dla których nie trwa wypowiedź o takim `id`, są odrzucane. Powtórzony `seq` jest pomijany. Brakujący `seq` oraz pakiet, którego nie da się zdekodować, zastępuje cisza o długości poprzedniego pakietu (20 ms, gdy nie było poprzedniego). Nowe `audio.utterance.start` przed `end` porzuca niedokończoną wypowiedź. Wypowiedź jest ucinana po `max_utterance_s` (30 s), a krótsza niż `min_utterance_ms` (300 ms) jest pomijana.

Odtwarzanie (host → ciało):

- Host koduje mowę do Opus: mono, `playback_rate_hz` (16000), pakiety `frame_ms` (20 ms), `bitrate_bps` (24000), ze stałą przepływnością (CBR). Przy zmiennej przepływności ton potrafi zająć 41 kbit/s zamiast 24, więc rozmiar odpowiedzi nie byłby przewidywalny. Wysyła ramki `kind=2` z `id` równym `audio_id` (1-65535, numerowane cyklicznie) i `seq` od 0, a po nich akcję `speak` z tym `audio_id`.
- Ciało ma jeden bufor odtwarzania. Ramki innego `audio_id` przychodzące w trakcie odtwarzania są odrzucane, a `speak` dla nich daje `busy`. Gdy nic nie gra, ramki nowego `audio_id` zastępują niezapowiedziane ramki poprzedniego.
- Host wysyła ramki kolejnego odtwarzania dopiero po upływie czasu trwania poprzedniego, liczonego od `ack` dla `speak` (`serialize_playback`, domyślnie włączone).
- Bufor odtwarzania ciała ma `body_buffer_bytes` (49 152 B) danych i `body_max_packets` (1024) pakietów (wartości początkowe, zgodne z firmware). Host nie wysyła więcej: nadmiarowe pakiety z końca odpowiedzi odcina. Konfiguracja, w której `max_speech_s` przy danej przepływności nie mieści się w buforze, jest błędem.
- Ciało odtwarza dopiero po `speak`, więc cały dźwięk musi się zmieścić w jego pamięci. Host ogranicza odpowiedź do `max_speech_s` (15 s): syntezuje zdanie po zdaniu i przestaje dodawać zdania, gdy kolejne przekroczyłoby limit. Pierwsze zdanie dłuższe niż limit jest ucinane.

Rozpoznawanie i synteza mowy:

- STT (faster-whisper) i TTS (Piper) działają lokalnie, więc dźwięk i tekst nie opuszczają maszyny hosta, z wyjątkiem napisów (`display`) dla urządzenia, które nagrało wypowiedź. Dźwięk, transkrypcje i teksty odpowiedzi nie są zapisywane w bazie ani logowane. Logi zawierają tylko czasy i liczby.
- Transkrypcja krótsza niż `min_text_chars` (2) znaków kończy wypowiedź bez odpowiedzi.
- Transkrypcja jest traktowana jak tekst z `POST /chat` (sekcja 12): trafia do LLM z tym samym kontekstem, a odpowiedź jest odtwarzana na ciele.

Tura głosowa na hoście:

- `audio.utterance.start` otwiera składanie wypowiedzi, a `audio.utterance.end` uruchamia turę w osobnym zadaniu, więc odbiór ramek nie jest blokowany. Równolegle trwa najwyżej `max_pending_turns` (3) tur, a kolejne wypowiedzi są pomijane z ostrzeżeniem w logu.
- Tura przechodzi: transkrypcja, ta sama ścieżka co `POST /chat` (kontekst, kolejność tur, cofanie wycinka stanu), synteza, ramki `kind=2` wysyłane jako binarne ramki WebSocket, akcja `speak` z `ack`. Odpowiedzi różnych tur są odtwarzane po kolei, a ramki jednej odpowiedzi nie mieszają się z drugą.
- Wypowiedź bez rozpoznanego tekstu i wypowiedź zbyt krótka nie dają odpowiedzi ani klipu.
- Gdy tura się nie powiedzie (błąd STT, LLM lub TTS, utrata łącza w trakcie), host zapisuje ostrzeżenie w logu i, jeśli ustawiono `error_clip`, wysyła `speak.clip` z tą nazwą (klipy zapisane w ciele, nazwy zależą od firmware). Odrzucone `speak` (`ack` z `ok: false`) lub brak `ack` kończy się samym ostrzeżeniem.
- Modele ładują się przy starcie hosta. Błąd ładowania, albo włączony głos przy wyłączonym LLM, wyłącza głos (`/health`: `voice` ma wartość `error`), a reszta hosta działa dalej.
- `brain.stt` i `brain.tts` włącza się razem.

Napisy (urządzenia z `display` w `device.caps`, np. przeglądarka):

- Host wysyła akcję `display` tylko urządzeniu, z którego przyszła wypowiedź, z `turn` równym jej `utt_id`. Kolejność w turze: `heard` z rozpoznanym tekstem po transkrypcji, potem `reply` z tekstem, który IRIS wypowie (tylko zdania zmieszczone w `max_speech_s`, `truncated: true`, gdy odpowiedź była dłuższa), wysyłane przed ramkami audio. Wypowiedź bez rozpoznanego tekstu kończy się `nothing`, nieudana tura `failed` (oba bez `text`).
- Akcja nie czeka na `ack`. Zapis `action.sent` zawiera zamiast `text` liczbę znaków (`chars`), więc treść rozmowy nie trafia do bazy.

## 7. Model stanu na hoście

`body` to stan aktywnego urządzenia, budowany z jego ostatnich zdarzeń:
```
{"batt_pct":71,"link":"ok","rssi":-58,"temp_c":24.1,
 "sensors_ok":["mic","imu","temp","presence"],"sensors_down":["gps"],"last_seen_ms":0}
```

- `link`: `unknown` (przed pierwszym heartbeatem), `ok`, `lost`. Z `unknown` do `lost` host przechodzi po `link_timeout_s` od startu, jeśli nie było heartbeatu. Wejście w `lost` generuje `link.lost`. Pierwszy heartbeat po `lost` generuje `link.restored` i ustawia `ok`.
- `sensors_down`: sensory z aktywnym `body.fault`, usuwane przez `body.fault.clear` lub `system.boot`. `sensors_ok`: pozostałe ze znanych sensorów (lista w konfiguracji: `mic`, `imu`, `temp`, `presence`, `gps`).
- `last_seen_ms`: czas od ostatniej ramki z ciała, liczony w chwili odczytu stanu.
- Wartości pól bez danych (np. `temp_c` przed pierwszym odczytem) to `null`.
- Host prowadzi taki stan dla każdego urządzenia połączonego w bieżącej sesji (`GET /devices`). `link` każdego z nich zmienia się według tych samych reguł, a `link.lost` i `link.restored` mają `device_id` tego urządzenia.
- Przed wyborem pierwszego urządzenia `body` nie należy do żadnego (`link` przechodzi z `unknown` w `lost`). Pierwsze urządzenie przejmuje ten stan, więc jego pierwszy heartbeat daje `link.restored`, jak przy jednym ciele.
- Po przełączeniu na inne urządzenie `body` pokazuje jego stan.

`self`:
```
{"name":"IRIS","version":"0.1.0","host":"<nazwa>","mode":"nominal|saving|offline",
 "uptime_s":0,"last_restart":"<ISO 8601>","last_downtime_s":0,
 "drives":["battery_low"],"continuity":{"sessions":0,"known_facts":0},"device":"hermes"}
```

`device`: aktywne urządzenie albo `null`.

`mode` wynika z aktywnych drives, w kolejności pierwszeństwa: `offline`, jeśli aktywne jest `link_lost`; `saving`, jeśli aktywne jest `battery_low` lub `battery_critical`; w przeciwnym razie `nominal`.

`continuity.sessions`: liczba sesji hosta łącznie z bieżącą. `last_restart`: czas startu bieżącej sesji.

Do promptu LLM trafia wycinek: `mode`, aktywne `drives`, zmiany `body` od ostatniego zapytania i `last_downtime_s`, jeśli była przerwa.

- Zmiana pola `body` jest raportowana, gdy różni się od wartości ostatnio raportowanej o więcej niż tolerancja z konfiguracji (wartości początkowe: `batt_pct` 5, `temp_c` 2, `rssi` 10). Pola nieliczbowe (`link`, `sensors_ok`, `sensors_down`) raportowane są przy każdej różnicy. Wartość odniesienia aktualizuje się tylko dla raportowanych pól. Przy pierwszym zapytaniu odniesieniem jest brak wartości (`null`).
- `last_downtime_s` trafia do pierwszego wycinka w sesji, gdy przerwa wynosiła co najmniej `min_downtime_s` (wartość początkowa: 60 s). Kolejne wycinki go nie zawierają.
- Wycinek nie zawiera odczytów `gps`, `imu` ani `presence`, tylko dostępność sensorów.
- Gdy aktywne urządzenie jest inne niż przy poprzednim wycinku, wartości odniesienia `body` wracają do `null`, a wycinek zawiera `device_change`: `{"from", "to"}` (urządzenie z poprzedniego wycinka i bieżące). Przełączenie tam i z powrotem między wycinkami niczego nie zmienia. Przejęcie stanu przez pierwsze urządzenie nie jest przełączeniem.
- Gdy zapytanie do LLM się nie powiedzie (błąd silnika albo anulowanie), wycinek jest cofany: wartości odniesienia, `device_change` i znacznik przerwy wracają do stanu sprzed pobrania, więc następny wycinek zawiera te same zmiany. Cofnąć można tylko ostatnio pobrany wycinek, dlatego tury rozmowy są obsługiwane po kolei.

## 8. Potrzeby (drives)

Deterministyczne reguły z histerezą. Wartości początkowe, do konfiguracji.

| drive | wejście | wyjście |
|---|---|---|
| `battery_low` | `batt_pct` < 20% przez 10 s | `batt_pct` ≥ 25% przez 30 s |
| `battery_critical` | `batt_pct` < 10% przez 10 s | `batt_pct` ≥ 15% przez 30 s |
| `link_lost` | brak heartbeatu > 5 s | heartbeat przez 5 s |
| `temp_high` | `temp_c` > 40 przez 30 s | `temp_c` < 37 przez 60 s |
| `long_silence` | brak interakcji > 4 h w godzinach aktywności | interakcja |

Wejście lub wyjście z drive zapisuje zdarzenie `drive.enter` / `drive.exit`.

Reguły obliczania:

- Czas w warunku ("przez 10 s") oznacza ciągłe spełnianie warunku przez co najmniej tyle sekund. Niespełnienie warunku zeruje licznik. Strefa między progiem wejścia i wyjścia nie zmienia stanu.
- `battery_low` i `battery_critical` są niezależne. Przy `batt_pct` < 10 oba mogą być aktywne.
- Gdy dane wejściowe są niedostępne, drive nie zmienia stanu, a licznik jest zerowany. Dla `battery_*` niedostępne znaczy: brak odczytu lub `link` ≠ `ok`. Dla `temp_high`: brak odczytu, `link` ≠ `ok` lub `temp` w `sensors_down`.
- `link_lost`: wejście natychmiast po przejściu `link` w `lost`. Wyjście, gdy `link` jest `ok` nieprzerwanie przez `link_recover_s`. Kolejna utrata łącza zeruje licznik.
- `long_silence`: wejście, gdy od ostatniej interakcji minęło więcej niż `long_silence_after_s` (4 h), a bieżąca godzina lokalna mieści się w godzinach aktywności. Wyjście przy pierwszej interakcji. Czas liczony zegarem ściennym. Przed pierwszą interakcją odniesieniem jest czas startu hosta.
- Interakcja to zdarzenie `audio.utterance.start`, `local.command` lub `button` albo przyjęte wywołanie `POST /chat` (także gdy silnik LLM potem zawiedzie).
- Przełączenie na inne urządzenie (sekcja 1) kończy drives zależne od ciała (`battery_low`, `battery_critical`, `link_lost`, `temp_high`): aktywne dostają `drive.exit` z `reason: "device_switch"`, a liczniki są zerowane. Dalej reguły działają na danych nowego urządzenia. `long_silence` się nie zmienia.
- Godziny aktywności: konfiguracja `active_hours`, wartość początkowa 08:00-22:00 czasu lokalnego. Przedział może przechodzić przez północ (np. 22:00-06:00).

Inicjatywa (wypowiedź bez wywołania użytkownika) wymaga zgody bramki: odmowa, gdy włączony jest tryb ciszy (`quiet`, ustawiany przez użytkownika, domyślnie wyłączony) lub gdy w ciągu ostatnich 60 minut zgoda była udzielona co najmniej `max_per_hour` razy (wartość początkowa: 2). Bramka nie zależy od rodzaju drive.

## 9. Ciągłość

- Przy starcie host wyznacza `last_downtime_s` jako różnicę między czasem startu a ostatnią aktywnością poprzedniej sesji (`sessions.last_seen_at`). Przy pierwszym uruchomieniu `last_downtime_s` wynosi 0.
- Zdarzenia i stany zapisywane w SQLite (tabele: `sessions`, `events`, `state_snapshots`, `facts`, `devices`). Wersja schematu w `PRAGMA user_version` (3); baza z nowszą wersją niż obsługiwana nie jest otwierana, starsza jest migrowana przy otwarciu.
- Czas w bazie: UTC, ISO 8601 ze stałą szerokością (`2026-03-01T12:00:00.000+00:00`), co pozwala porównywać go jako tekst. Czas bez strefy jest odrzucany.

`sessions`: `id`, `started_at`, `ended_at` (ustawiane przy czystym zamknięciu), `last_seen_at`. `last_seen_at` odświeża się przy każdym zapisanym zdarzeniu i przez jawne `touch`, więc przerwa jest poprawna także wtedy, gdy nie zapisujemy heartbeatów. Sesja bez aktywności ma `last_seen_at` równe `started_at`.

`events`: `id`, `session_id`, `at` (czas zegarowy hosta przy zapisie), `source` (`body` lub `host`), `type`, `ts_ms` (zegar ciała, tylko dla `body`), `boot_id` (ostatni `system.boot` urządzenia, łączy `ts_ms` z czasem zegarowym), `data` (JSON z `body` zdarzenia), `device_id` (urządzenie zdarzenia z ciała albo urządzenie, którego dotyczy zdarzenie hosta, sekcja 4).

`devices`: `id`, `first_seen_at`, `last_seen_at`, `active` (najwyżej jeden wiersz z 1). Urządzenie trafia do tabeli przy pierwszym połączeniu. `last_seen_at` odświeża się przy połączeniu, rozłączeniu i co `touch_every_s` dla połączonych urządzeń.

- Migracja z wersji 1 (jedno ciało): zdarzenia z ciała oraz `link.*` i `action.*` dostają `device_id` `hermes`. Jeśli były zdarzenia z ciała, `hermes` trafia do `devices` jako aktywne (czas pierwszego i ostatniego z nich).

`state_snapshots`: `id`, `session_id`, `at`, `self_json`, `body_json`. Moment zapisu wybiera warstwa api.

`facts`: `id`, `text`, `source` (`stated` lub `inferred`), `confidence` (0 do 1), `created_at`, `updated_at`, `session_id` (sesja, w której fakt powstał), `reinforced` (ile razy wniosek się powtórzył), `asked_at` (kiedy host ostatnio o niego pytał, `null` gdy nigdy). Migracja z wersji 2 dodaje dwie ostatnie kolumny (`0` i `null`).

- Fakt `stated` ma domyślnie `confidence` 1. Fakt `inferred` wymaga podania `confidence`.
- Użytkownik może wyświetlić, poprawić i usunąć fakty. Poprawa tekstu przez użytkownika zmienia `source` na `stated` i `confidence` na 1, chyba że podano inaczej. Usunięcie jest trwałe.
- `continuity.known_facts` to liczba wierszy w `facts`.

### Uczenie się

IRIS uczy się faktów o użytkowniku jak program, przez zapis w tabeli `facts`; model językowy się nie zmienia. Fakt podany wprost jest pewny (`stated`, `confidence` 1). Wniosek z rozmowy jest przypuszczeniem (`inferred`): zaczyna z niską pewnością, rośnie, gdy się powtarza, maleje, gdy się nie powtarza, a IRIS pyta o niego użytkownika. Reguły są deterministyczne; LLM tylko proponuje wnioski. Wartości początkowe w konfiguracji `learning`; `enabled = false` wyłącza naukę: polecenia pamięci (trafiają wtedy do LLM jak zwykła wypowiedź), wnioski, pytania i zanikanie. Fakty już zapisane zostają.

Polecenia rozpoznaje host przed LLM i bez niego, jak komendę przełączenia (sekcja 1), w mowie i w `POST /chat` (odpowiedź z `model` równym `null`). Kolejność: odpowiedź na otwarte pytanie (niżej), komenda przełączenia, polecenia pamięci, LLM. Wielkość liter, interpunkcja na końcu i słowo "IRIS" na początku nie mają znaczenia:

- "zapamiętaj (, że) X", "zapisz (, że) X", "pamiętaj (, że) X": X (3-300 znaków) trafia do `facts` jako `stated` dosłownie, w słowach użytkownika. Jeśli podobny fakt już jest (podobieństwo niżej), zostaje zastąpiony nowym tekstem. Odpowiedź: `Zapamiętane: "X".` Zdarzenie `fact.learn` z `how: "told"`.
- "zapomnij (, że | o) X": usuwa najbardziej podobny fakt, jeśli podobieństwo wynosi co najmniej `forget_match` (0,5). Odpowiedź `Zapomniane: "<tekst faktu>".` albo `Nie znam takiej informacji.`
- "zapomnij wszystko": IRIS pyta `Na pewno usunąć wszystko, co o tobie wiem (N informacji)? Powiedz tak albo nie.` (z odmianą liczebnika) i usuwa wszystkie fakty dopiero po odpowiedzi "tak" (jak przy pytaniu niżej, w ciągu `answer_window_s`). Bez faktów: `Nic o tobie nie wiem, nie ma czego usuwać.`
- "co o mnie wiesz", "co wiesz o mnie", "czego się o mnie nauczyłaś" (także "nauczyłeś"): najwyżej 5 faktów `stated` (w cudzysłowie) i 3 przypuszczenia o pewności co najmniej `min_inferred_confidence` (z pewnością w procentach), potem liczba pozostałych. Bez faktów: `Jeszcze nic o tobie nie wiem. Możesz powiedzieć: zapamiętaj, że ...`

Podobieństwo dwóch tekstów to indeks Jaccarda zbiorów rdzeni słów: słowa małymi literami, bez interpunkcji, słów krótszych niż 3 znaki i słów pomocniczych ("mam", "masz", "jest", zaimki), skrócone do 4 pierwszych liter (słowa 3-4-literowe do 3), bo odmiana zmienia końcówki ("pracuję" i "pracujesz", "kawa" i "kawę"). Liczby porównywane są w całości.

Wnioski z rozmowy (`infer`: `local_only`, wartość początkowa, tylko przy silniku lokalnym; `always`; `never`):

- Po każdej turze rozmowy z LLM (nie po poleceniach) host w tle wysyła do tego samego silnika osobne zapytanie: prompt wyciągania faktów i sama wypowiedź użytkownika, bez historii, stanu i znanych faktów. Silnik zwraca listę JSON `[{"fakt": "...", "pewnosc": 0.0-1.0}]` ze zdaniami w drugiej osobie ("Masz psa o imieniu Burek."). Błąd silnika albo niepoprawna odpowiedź są logowane bez treści i pomijane; tura rozmowy na nie nie czeka.
- Host bierze najwyżej `max_per_turn` (3) wniosków o długości 3-300 znaków. Odrzuca wnioski o pewności poniżej `forget_below` (silnik sam nie jest ich pewien) i wnioski z danymi wrażliwymi: tekst zawiera jeden z rdzeni `sensitive_words` (zdrowie, leki, tętno, położenie i adres, pieniądze, wyznanie, poglądy, orientacja). Takie fakty IRIS zapisuje tylko z polecenia "zapamiętaj".
- Wniosek podobny (co najmniej `similar` 0,6) do faktu `stated` jest pomijany. Podobny do przypuszczenia wzmacnia je: `confidence` rośnie o `reinforce_step` (0,2), najwyżej do `max_inferred` (0,9), `reinforced` rośnie o 1, `updated_at` się odświeża (`how: "reinforced"`). Nowy wniosek trafia do `facts` jako `inferred` z pewnością podaną przez silnik, ale najwyżej `start_max` (0,45), czyli poniżej progu użycia w kontekście (sekcja 12): pojedyncza obserwacja nie wystarcza, potrzebne jest powtórzenie albo potwierdzenie (`how: "inferred"`).

Pytania (`ask`, domyślnie włączone; wyłączone także przy `initiative.quiet`):

- Po odpowiedzi LLM w turze rozmowy host może dopisać do niej jedno pytanie: ` Czy dobrze rozumiem: <przypuszczenie>?` (pierwsza litera mała, bez kropki na końcu). Wybiera przypuszczenie o pewności co najmniej `ask_min` (0,4), o które nie pytał przez ostatnie `ask_again_days` (7) dni: najwyższa pewność, przy remisie najnowsze. Między pytaniami mija co najmniej `ask_interval_s` (900 s). Host zapisuje `asked_at` i zdarzenie `fact.ask`.
- Następna wypowiedź w ciągu `answer_window_s` (120 s) to odpowiedź, jeśli zaczyna się od "tak" ("tak", "zgadza się", "dokładnie", "owszem") albo "nie" ("nie", "nieprawda", "nie zgadza się"). "Tak": fakt staje się `stated` z `confidence` 1 (`fact.confirm`), odpowiedź `Dobrze, zapamiętane.` "Nie": fakt jest usuwany (`why: "denied"`); jeśli po "nie" jest dalszy tekst (co najmniej 3 znaki), trafia on do `facts` jako `stated` (`how: "corrected"`, odpowiedź `Dobrze, poprawione: "<tekst>".`), inaczej odpowiedź `Dobrze, usuwam to przypuszczenie.` Inna wypowiedź zamyka pytanie bez zmiany faktu i trafia do zwykłej obsługi.

Zanikanie: raz na dobę i przy starcie host zmniejsza o `decay_step` (0,1) pewność przypuszczeń, które nie zmieniły się przez `decay_after_days` (30) dni (`updated_at` też się odświeża), i usuwa przypuszczenia poniżej `forget_below` (0,2) (`why: "decay"`). Fakty `stated` nie zanikają.

Co host zapisuje (wartości początkowe w konfiguracji `api`):

- każde zdarzenie z ciała poza `body.heartbeat` (heartbeaty tylko przy `store_heartbeats`, domyślnie wyłączone) i każde zdarzenie hosta,
- migawkę stanu przy starcie, przy zamknięciu, przy każdym `drive.enter`, `drive.exit` i `device.switch` oraz co `snapshot_every_s` (300 s),
- `last_seen_at` sesji co `touch_every_s` (10 s).

## 10. REST

Odpowiedzi w JSON, czas w UTC (ISO 8601). Parametr `since` wymaga strefy (`Z` lub przesunięcie, w URL `+` jako `%2B`), czas bez strefy daje 422.

CORS: domyślnie host nie odpowiada na żądania z innych adresów (przeglądarka je blokuje). Konfiguracja `api.cors_origins` (lista, domyślnie pusta) wymienia adresy stron, które mogą wywoływać REST hosta z przeglądarki, np. `["https://iris-asystent.vercel.app"]`. Adres to dokładnie `https://host[:port]` (albo `http://localhost[:port]`), bez ścieżki, bez `*`; inny zapis zatrzymuje start hosta z błędem konfiguracji. Dla wymienionych adresów host odpowiada na `OPTIONS` (zapytanie wstępne) i dodaje `Access-Control-Allow-Origin` z tym adresem, dopuszcza nagłówki `Authorization` i `Content-Type` oraz metody `GET`, `POST`, `PATCH`, `DELETE`. Nie używa ciasteczek (`Allow-Credentials` nie jest ustawiane). CORS nie zastępuje tokenu: bez poprawnego tokenu host nadal odpowiada 401, a `/ws` zamyka połączenie kodem 4003. Dla innych adresów zapytanie wstępne dostaje 400.

Gdy host ma token (sekcja 1), każde żądanie wymaga nagłówka `Authorization: Bearer <token>`, inaczej 401. Wyjątkiem są pliki klienta przeglądarkowego (`/app/`): są publiczne i nie zawierają danych. Bez tokenu host powinien nasłuchiwać tylko na 127.0.0.1, a żądanie z nagłówkiem proxy (sekcja 1) dostaje 401. Dostęp z telefonu przez tunel HTTPS wymaga więc tokenu.

| metoda i ścieżka | opis |
|---|---|
| `GET /self` | aktualny `self` i `body` |
| `GET /self/history?since=<ISO>&limit=` | zmiany stanu i zdarzenia |
| `GET /events?type=&device=&since=&after_id=&limit=` | zdarzenia |
| `POST /actions` | wysłanie akcji do urządzenia (debug) |
| `GET /devices` | urządzenia i aktywne urządzenie |
| `POST /devices/{id}/activate` | przełączenie aktywnego urządzenia |
| `POST /chat` | wypowiedź tekstowa do asystenta (wymaga włączonego LLM) |
| `GET /facts`, `PATCH /facts/{id}`, `DELETE /facts/{id}` | pamięć o użytkowniku |
| `GET /health` | stan hosta i łącza |
| `GET /app/` | klient w przeglądarce (publiczny; `/` przekierowuje tutaj) |

- `GET /self`: `{"self": {...}, "body": {...}}` (sekcja 7).
- `GET /self/history`: `{"since": "<ISO>", "snapshots": [{id, session_id, at, self, body}], "events": [...]}`. `since` domyślnie oznacza start bieżącej sesji. Zdarzenia to typy zmian stanu: `host.start`, `host.stop`, `link.lost`, `link.restored`, `drive.enter`, `drive.exit`, `system.boot`, `body.fault`, `body.fault.clear`, `device.switch`. `limit` (1-1000, domyślnie 1000) dotyczy każdej z list.
- `GET /events`: `{"events": [{id, session_id, at, source, type, ts_ms, boot_id, device, data}]}` rosnąco po `id`. `type` można powtórzyć, `device` zawęża do jednego urządzenia. `since` jest włączone, `after_id` wyłączone (stronicowanie). `limit` jak wyżej.
- `POST /actions`: ciało `{"type": "haptic", "body": {"pattern": "warn"}, "wait_ack": true, "device": "laptop"}` (`device` opcjonalne, domyślnie aktywne urządzenie). Odpowiedź 200: `{"id": "h3", "device": "laptop", "ack": {"ref", "ok", "err"}}` (`ack` jest `null`, gdy `wait_ack` ma wartość `false`). 409, gdy urządzenie nie jest połączone albo nie ma aktywnego. 422 dla nieznanego typu lub niepoprawnego `body`. 504, gdy `ack` nie przyszedł w `ack_timeout_s` (2 s); akcja została wtedy wysłana.
- `POST /chat`: ciało `{"text": "..."}` (1-4000 znaków po obcięciu białych znaków, brak dodatkowych pól). 200: `{"text", "truncated", "model", "usage": {"input_tokens", "output_tokens"}}`. 501, gdy `brain.llm.engine` ma wartość `none`. 503, gdy silnik jest niedostępny (brak połączenia, limit czasu). 502 przy błędzie silnika (zły model, niepoprawna lub pusta odpowiedź). 422 dla niepoprawnego ciała i dla odmowy silnika. Kontekst jak w sekcji 12. Treść rozmowy nie jest zapisywana w bazie ani logowana. Tury są obsługiwane po kolei. Komenda przełączenia (sekcja 1) nie trafia do LLM i działa także przy `engine = "none"`: odpowiedź ma `model` równe `null` i zerowe `usage`.
- `GET /facts?source=`: `{"facts": [{id, text, source, confidence, reinforced, asked_at, in_context, created_at, updated_at}]}`. `in_context`: czy fakt trafia teraz do kontekstu LLM według `send_facts` i progu `min_inferred_confidence` (sekcja 12), bez limitu `max_facts`; `false`, gdy LLM jest wyłączony.
- `PATCH /facts/{id}`: ciało z co najmniej jednym polem z `text`, `source`, `confidence`. 200 z faktem, 404 lub 422. Reguły korekty w sekcji 9.
- `DELETE /facts/{id}`: 204 lub 404. Usunięcie zapisuje zdarzenie `fact.forget` z `why: "user"`.
- `GET /devices`: `{"active": "<id>|null", "devices": [{id, active, connected, link, batt_pct, rssi, caps, first_seen_at, last_seen_at}]}` rosnąco po `id`. `link`, `batt_pct` i `rssi` są `null` dla urządzeń bez stanu w bieżącej sesji, `caps` (body ostatniego `device.caps`) przed pierwszym `device.caps` w sesji.
- `POST /devices/{id}/activate`: 200 `{"active": "<id>"}`, także gdy urządzenie już było aktywne (wtedy bez zdarzenia). 404 dla urządzenia, które nigdy się nie połączyło, 422 dla niepoprawnego `id`.
- `GET /health`: zawsze 200, `{"status": "ok|degraded", "uptime_s", "session_id", "device", "devices_connected", "link": {"state", "connected", "rssi", "last_seen_ms"}, "store": "ok|error", "voice": "disabled|ready|error"}`. `link` dotyczy aktywnego urządzenia (`device`, albo `null`). `status` to `ok` tylko przy `link.state` = `ok`. `connected` oznacza otwarte połączenie WebSocket aktywnego urządzenia.

## 11. Obsługa błędów

- Nieznany `type` lub `v`: log i odrzucenie, połączenie pozostaje.
- Uszkodzona ramka: odrzucenie, licznik błędów w `body.heartbeat`.
- Utrata łącza: ciało kontynuuje komendy lokalne i buforuje zdarzenia (limit bufora do ustalenia), host oznacza `link_lost`.
- Błąd zapisu do bazy jest logowany. Host pracuje dalej na stanie w pamięci.
- Nieoczekiwany wyjątek przy obsłudze ramki jest logowany, połączenie pozostaje.

## 12. Kontekst dla LLM

LLM jest opcjonalny (`brain.llm.engine = "none"` wyłącza go bez wpływu na resztę). Jedno zapytanie składa się z:

- promptu systemowego (stały tekst),
- historii rozmowy: ostatnie `history_turns` par (wypowiedź, odpowiedź), bez kontekstu z minionych tur,
- bieżącej wypowiedzi użytkownika z doklejonym blokiem kontekstu: czas lokalny, wycinek stanu z sekcji 7 (tryb, aktywne drives, zmiany `body`, przerwa) i fakty.

Reguły:

- Prompt systemowy mówi, że IRIS jest programem, że opisy stanu pochodzą z danych w bloku kontekstu, a nie z odczuwania, i że nie twierdzi on, iż coś czuje lub jest świadomy. Blok kontekstu jest oznaczony jako dane, nie polecenia. Prompt opisuje pamięć tak, jak działa: fakty podane wprost i przypuszczenia z rozmów (sekcja 9); IRIS nie twierdzi, że zapamiętuje całe rozmowy.
- Wycinek stanu nie zawiera odczytów `gps`, `imu` ani `presence` (sekcja 7), więc nie trafiają one do żadnego silnika.
- Fakty (sekcja 9) trafiają do kontekstu według `send_facts`: `never` (nigdy), `local_only` (tylko do silnika lokalnego, wartość początkowa), `always`. Fakty `inferred` poniżej `min_inferred_confidence` (0,5) są pomijane, pozostałe oznaczane jako przypuszczenie. W kontekście jest najwyżej `max_facts` (30) faktów, najpierw `stated`.
- Zapytanie o wnioski z rozmowy (sekcja 9, "Uczenie się") to osobne zapytanie z własnym promptem i samą wypowiedzią użytkownika. Przy `infer = "local_only"` nie trafia do silnika zdalnego.
- Silnik jest lokalny, gdy jego adres wskazuje na pętlę zwrotną (`localhost`, `127.0.0.1`, `::1`). Każdy inny adres, w tym sieć lokalna, liczy się jako zdalny.

Silnik Claude API (`brain.llm.engine = "claude"`):

- Dane trafiają do chmury Anthropic, więc silnik jest zdalny: fakty obowiązują według `send_facts`, a do żądania trafia dokładnie to, co wymienia ta sekcja (prompt, historia, blok kontekstu, wypowiedź). Odczyty `gps`, `imu` i `presence` nigdy nie opuszczają hosta.
- Klucz API pochodzi ze zmiennych środowiskowych SDK (`ANTHROPIC_API_KEY`) i nie jest zapisywany w konfiguracji ani w logach. Brak klucza to błąd konfiguracji silnika. Logger SDK jest ograniczony do poziomu INFO (także przy `ANTHROPIC_LOG=debug`), bo na poziomie DEBUG wypisuje całe żądanie z treścią rozmowy.
- Model domyślny to `claude-opus-5-5`; można go zmienić w `model`. Żądanie nie zawiera `temperature`, `top_p` ani `top_k` (API od 2026-10-21 je odrzuca), dlatego `temperature` w konfiguracji tego silnika jest błędem.
- `effort` (domyślnie `low`, wartość `off` pomija parametr) steruje głębokością myślenia i opóźnieniem odpowiedzi. Tokeny myślenia liczą się do limitu odpowiedzi, więc do `max_tokens` dodawany jest zapas `thinking_headroom_tokens` (2000).
- Dla modeli z serwerowym zapasem (`claude-fable-5-1`, `claude-opus-5`, `claude-opus-5-5`, `claude-sonnet-5-5`) żądanie zawiera `fallbacks: "default"`: odmowa klasyfikatora bezpieczeństwa jest powtarzana po stronie serwera na modelu zapasowym (`fallbacks = false` to wyłącza). Odmowa, która przetrwa, kończy się błędem silnika (`POST /chat`: 422).
- Ponowienia i limity czasu obsługuje SDK (`retries`, `timeout_s`). Błędy 429, 5xx, przeciążenie, brak połączenia i przekroczony czas to silnik niedostępny. Błędy klucza, uprawnień i nieznanego modelu to błąd konfiguracji silnika, a pozostałe odpowiedzi 4xx to zwykły błąd silnika.
- Odpowiedź jest czytana na głos, więc z tekstu usuwane są znaczniki markdown i podziały wierszy.
- Błąd silnika (brak połączenia, limit czasu, odmowa) jest zgłaszany wywołującemu, a historia rozmowy pozostaje bez zmian. Rdzeń, API i baza działają dalej.

## 13. Otwarte kwestie

- Limit bufora zdarzeń w ciele przy braku łącza (symulator przyjmuje 64 i przy przepełnieniu odrzuca najstarsze).
- Format ramki audio po testach na STM32 (Opus na L476 wymaga sprawdzenia pod kątem CPU i RAM). Host zakłada 16 kHz, mono i pakiety 20 ms w obu kierunkach.
- Rozmiar bufora odtwarzania w ciele (firmware: 48 KB danych, cały stan rdzenia ok. 70 KB z 128 KB RAM); od niego zależy `max_speech_s` (15 s przy 24 kbit/s CBR to 45 000 B).
- Wybór między JSON a CBOR dla zdarzeń z ciała po pomiarze zużycia RAM.
- Zestaw lokalnych komend i ich wzorców.
- Sposób ustawiania trybu ciszy (endpoint REST, komenda głosowa lub oba).
- Czy `battery_critical` może omijać limit inicjatywy.
- Retencja i usuwanie zdarzeń, w tym zapisanych odczytów `gps` i `imu`.
- TLS w firmware HERMES (`wss://`, np. mbedTLS: pamięć RAM i flash). Bez TLS token i rozmowa idą przez sieć jawnie, więc HERMES powinien łączyć się tylko w zaufanej sieci.
- Automatyczne przełączanie urządzenia (np. według obecności albo tego, gdzie użytkownik mówi).
- Port firmware na STM32L476: moduł sieciowy (L476 nie ma Wi-Fi ani Ethernetu) z transportem TCP dla klienta `fw_ws`, kodek mikrofonu i koder Opus (CPU i RAM), czujniki, sterownik haptyki, pomiar baterii.
