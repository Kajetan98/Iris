# IRIS: specyfikacja protokołu i modelu stanu (v0.1, szkic)

## 1. Transport

- WebSocket, endpoint `/ws`. Jedno połączenie na ciało.
- Ramki tekstowe: JSON UTF-8. Ramki binarne: audio (sekcja 6).
- Heartbeat: ciało wysyła `body.heartbeat` co 1 s. Brak heartbeatu przez 5 s oznacza `link.lost` (wartości początkowe).
- Wersja protokołu w polu `v`. Zmiana niezgodna wstecz podnosi `v`.
- Nowe połączenie zastępuje poprzednie: host zamyka stare z kodem 4000 (ciało po zerwaniu łącza łączy się ponownie, a stare gniazdo może być jeszcze otwarte po stronie hosta).
- Odrzucona ramka nie zamyka połączenia (sekcja 11).
- Akcje host wysyła jako ramki tekstowe. `id` ma postać `h<licznik>`, `ts` to milisekundy od startu hosta (zegar monotoniczny).

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
| `body.heartbeat` | `batt_pct`, `link_rssi`, `frame_errors` (opcjonalne) | co 1 s; `frame_errors` to licznik odrzuconych ramek (sekcja 11) |
| `body.sensor` | `temp_c`, `imu`, `presence`, `gps` (pola opcjonalne) | co zmianę lub co N s |
| `body.fault` | `sensor`, `code` | sensor niedostępny lub błędny |
| `body.fault.clear` | `sensor` | sensor znów działa |
| `audio.utterance.start` | `utt_id` | po wake word |
| `audio.utterance.end` | `utt_id`, `dur_ms` | po VAD |
| `local.command` | `cmd`, `result` | komenda obsłużona lokalnie na STM32 |
| `button` | `id`, `kind` (`press`, `long`) | |

## 4. Zdarzenia hosta (wewnętrzne, widoczne w `/events`)

`link.lost`, `link.restored`, `drive.enter`, `drive.exit`, `host.start`, `host.stop`, `action.sent`, `action.ack`.

- `action.sent`: `id`, `type`, `body`. Zapisywane po wysłaniu akcji do ciała.
- `action.ack`: `ref`, `ok`, `err`. Zapisywane po odebraniu `ack` od ciała.

## 5. Akcje (host → ciało)

| type | body | uwagi |
|---|---|---|
| `speak` | `audio_id` | odtworzenie audio wysłanego binarnie |
| `speak.clip` | `clip` | gotowy klip zapisany w ciele |
| `haptic` | `pattern` (`attention`, `warn`, `critical`) | |
| `gpio.set` | `pin`, `value` | tylko piny z białej listy |
| `config.set` | `key`, `value` | progi lokalne, głośność |
| `ping` | – | odpowiedź `ack` |

Ciało odrzuca akcję spoza listy i odsyła `ack` z `ok:false`.

## 6. Audio

- Kodek: Opus, ramki binarne. Nagłówek ramki: `kind` (1 B: 1 = mikrofon, 2 = odtwarzanie), `id` (2 B), `seq` (2 B), dane.
- Mikrofon: strumień między `audio.utterance.start` a `audio.utterance.end`.
- Odtwarzanie: host wysyła ramki z `kind=2`, potem akcję `speak` z `audio_id`.
- Pola wielobajtowe nagłówka (5 bajtów) są zapisane w kolejności big-endian.
- Od ciała host przyjmuje tylko `kind=1`. Inne wartości `kind` i ramki krótsze niż nagłówek są odrzucane (sekcja 11).

## 7. Model stanu na hoście

`body` (z ostatnich zdarzeń):
```
{"batt_pct":71,"link":"ok","rssi":-58,"temp_c":24.1,
 "sensors_ok":["mic","imu","temp","presence"],"sensors_down":["gps"],"last_seen_ms":0}
```

- `link`: `unknown` (przed pierwszym heartbeatem), `ok`, `lost`. Z `unknown` do `lost` host przechodzi po `link_timeout_s` od startu, jeśli nie było heartbeatu. Wejście w `lost` generuje `link.lost`. Pierwszy heartbeat po `lost` generuje `link.restored` i ustawia `ok`.
- `sensors_down`: sensory z aktywnym `body.fault`, usuwane przez `body.fault.clear` lub `system.boot`. `sensors_ok`: pozostałe ze znanych sensorów (lista w konfiguracji: `mic`, `imu`, `temp`, `presence`, `gps`).
- `last_seen_ms`: czas od ostatniej ramki z ciała, liczony w chwili odczytu stanu.
- Wartości pól bez danych (np. `temp_c` przed pierwszym odczytem) to `null`.

`self`:
```
{"name":"IRIS","version":"0.1.0","host":"<nazwa>","mode":"nominal|saving|offline",
 "uptime_s":0,"last_restart":"<ISO 8601>","last_downtime_s":0,
 "drives":["battery_low"],"continuity":{"sessions":0,"known_facts":0}}
```

`mode` wynika z aktywnych drives, w kolejności pierwszeństwa: `offline`, jeśli aktywne jest `link_lost`; `saving`, jeśli aktywne jest `battery_low` lub `battery_critical`; w przeciwnym razie `nominal`.

`continuity.sessions`: liczba sesji hosta łącznie z bieżącą. `last_restart`: czas startu bieżącej sesji.

Do promptu LLM trafia wycinek: `mode`, aktywne `drives`, zmiany `body` od ostatniego zapytania i `last_downtime_s`, jeśli była przerwa.

- Zmiana pola `body` jest raportowana, gdy różni się od wartości ostatnio raportowanej o więcej niż tolerancja z konfiguracji (wartości początkowe: `batt_pct` 5, `temp_c` 2, `rssi` 10). Pola nieliczbowe (`link`, `sensors_ok`, `sensors_down`) raportowane są przy każdej różnicy. Wartość odniesienia aktualizuje się tylko dla raportowanych pól. Przy pierwszym zapytaniu odniesieniem jest brak wartości (`null`).
- `last_downtime_s` trafia do pierwszego wycinka w sesji, gdy przerwa wynosiła co najmniej `min_downtime_s` (wartość początkowa: 60 s). Kolejne wycinki go nie zawierają.
- Wycinek nie zawiera odczytów `gps`, `imu` ani `presence`, tylko dostępność sensorów.

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
- Interakcja to zdarzenie `audio.utterance.start`, `local.command` lub `button`.
- Godziny aktywności: konfiguracja `active_hours`, wartość początkowa 08:00-22:00 czasu lokalnego. Przedział może przechodzić przez północ (np. 22:00-06:00).

Inicjatywa (wypowiedź bez wywołania użytkownika) wymaga zgody bramki: odmowa, gdy włączony jest tryb ciszy (`quiet`, ustawiany przez użytkownika, domyślnie wyłączony) lub gdy w ciągu ostatnich 60 minut zgoda była udzielona co najmniej `max_per_hour` razy (wartość początkowa: 2). Bramka nie zależy od rodzaju drive.

## 9. Ciągłość

- Przy starcie host wyznacza `last_downtime_s` jako różnicę między czasem startu a ostatnią aktywnością poprzedniej sesji (`sessions.last_seen_at`). Przy pierwszym uruchomieniu `last_downtime_s` wynosi 0.
- Zdarzenia i stany zapisywane w SQLite (tabele: `sessions`, `events`, `state_snapshots`, `facts`). Wersja schematu w `PRAGMA user_version`; baza z nowszą wersją niż obsługiwana nie jest otwierana.
- Czas w bazie: UTC, ISO 8601 ze stałą szerokością (`2026-03-01T12:00:00.000+00:00`), co pozwala porównywać go jako tekst. Czas bez strefy jest odrzucany.

`sessions`: `id`, `started_at`, `ended_at` (ustawiane przy czystym zamknięciu), `last_seen_at`. `last_seen_at` odświeża się przy każdym zapisanym zdarzeniu i przez jawne `touch`, więc przerwa jest poprawna także wtedy, gdy nie zapisujemy heartbeatów. Sesja bez aktywności ma `last_seen_at` równe `started_at`.

`events`: `id`, `session_id`, `at` (czas zegarowy hosta przy zapisie), `source` (`body` lub `host`), `type`, `ts_ms` (zegar ciała, tylko dla `body`), `boot_id` (ostatni `system.boot` ciała, łączy `ts_ms` z czasem zegarowym), `data` (JSON z `body` zdarzenia).

`state_snapshots`: `id`, `session_id`, `at`, `self_json`, `body_json`. Moment zapisu wybiera warstwa api.

`facts`: `id`, `text`, `source` (`stated` lub `inferred`), `confidence` (0 do 1), `created_at`, `updated_at`, `session_id` (sesja, w której fakt powstał).

- Fakt `stated` ma domyślnie `confidence` 1. Fakt `inferred` wymaga podania `confidence`.
- Użytkownik może wyświetlić, poprawić i usunąć fakty. Poprawa tekstu przez użytkownika zmienia `source` na `stated` i `confidence` na 1, chyba że podano inaczej. Usunięcie jest trwałe.
- `continuity.known_facts` to liczba wierszy w `facts`.

Co host zapisuje (wartości początkowe w konfiguracji `api`):

- każde zdarzenie z ciała poza `body.heartbeat` (heartbeaty tylko przy `store_heartbeats`, domyślnie wyłączone) i każde zdarzenie hosta,
- migawkę stanu przy starcie, przy zamknięciu, przy każdym `drive.enter` i `drive.exit` oraz co `snapshot_every_s` (300 s),
- `last_seen_at` sesji co `touch_every_s` (10 s).

## 10. REST

Odpowiedzi w JSON, czas w UTC (ISO 8601). Parametr `since` wymaga strefy (`Z` lub przesunięcie, w URL `+` jako `%2B`), czas bez strefy daje 422. API nie ma jeszcze uwierzytelniania (sekcja 12), więc host domyślnie nasłuchuje tylko na 127.0.0.1.

| metoda i ścieżka | opis |
|---|---|
| `GET /self` | aktualny `self` i `body` |
| `GET /self/history?since=<ISO>&limit=` | zmiany stanu i zdarzenia |
| `GET /events?type=&since=&after_id=&limit=` | zdarzenia |
| `POST /actions` | wysłanie akcji do ciała (debug) |
| `GET /facts`, `PATCH /facts/{id}`, `DELETE /facts/{id}` | pamięć o użytkowniku |
| `GET /health` | stan hosta i łącza |

- `GET /self`: `{"self": {...}, "body": {...}}` (sekcja 7).
- `GET /self/history`: `{"since": "<ISO>", "snapshots": [{id, session_id, at, self, body}], "events": [...]}`. `since` domyślnie oznacza start bieżącej sesji. Zdarzenia to typy zmian stanu: `host.start`, `host.stop`, `link.lost`, `link.restored`, `drive.enter`, `drive.exit`, `system.boot`, `body.fault`, `body.fault.clear`. `limit` (1-1000, domyślnie 1000) dotyczy każdej z list.
- `GET /events`: `{"events": [{id, session_id, at, source, type, ts_ms, boot_id, data}]}` rosnąco po `id`. `type` można powtórzyć. `since` jest włączone, `after_id` wyłączone (stronicowanie). `limit` jak wyżej.
- `POST /actions`: ciało `{"type": "haptic", "body": {"pattern": "warn"}, "wait_ack": true}`. Odpowiedź 200: `{"id": "h3", "ack": {"ref", "ok", "err"}}` (`ack` jest `null`, gdy `wait_ack` ma wartość `false`). 409, gdy ciało nie jest połączone. 422 dla nieznanego typu lub niepoprawnego `body`. 504, gdy `ack` nie przyszedł w `ack_timeout_s` (2 s); akcja została wtedy wysłana.
- `GET /facts?source=`: `{"facts": [{id, text, source, confidence, created_at, updated_at}]}`.
- `PATCH /facts/{id}`: ciało z co najmniej jednym polem z `text`, `source`, `confidence`. 200 z faktem, 404 lub 422. Reguły korekty w sekcji 9.
- `DELETE /facts/{id}`: 204 lub 404.
- `GET /health`: zawsze 200, `{"status": "ok|degraded", "uptime_s", "session_id", "link": {"state", "connected", "rssi", "last_seen_ms"}, "store": "ok|error"}`. `status` to `ok` tylko przy `link.state` = `ok`. `connected` oznacza otwarte połączenie WebSocket.

## 11. Obsługa błędów

- Nieznany `type` lub `v`: log i odrzucenie, połączenie pozostaje.
- Uszkodzona ramka: odrzucenie, licznik błędów w `body.heartbeat`.
- Utrata łącza: ciało kontynuuje komendy lokalne i buforuje zdarzenia (limit bufora do ustalenia), host oznacza `link_lost`.
- Błąd zapisu do bazy jest logowany. Host pracuje dalej na stanie w pamięci.
- Nieoczekiwany wyjątek przy obsłudze ramki jest logowany, połączenie pozostaje.

## 12. Otwarte kwestie

- Limit bufora zdarzeń w ciele przy braku łącza.
- Format ramki audio po testach na STM32 (Opus na L476 wymaga sprawdzenia pod kątem CPU i RAM).
- Wybór między JSON a CBOR dla zdarzeń z ciała po pomiarze zużycia RAM.
- Zestaw lokalnych komend i ich wzorców.
- Sposób ustawiania trybu ciszy (endpoint REST, komenda głosowa lub oba).
- Czy `battery_critical` może omijać limit inicjatywy.
- Retencja i usuwanie zdarzeń, w tym zapisanych odczytów `gps` i `imu`.
- Uwierzytelnianie API i WebSocket (ciało łączy się przez sieć, więc host będzie musiał nasłuchiwać poza 127.0.0.1).
