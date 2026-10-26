# IRIS: specyfikacja protokołu i modelu stanu (v0.1, szkic)

## 1. Transport

- WebSocket, endpoint `/ws`. Jedno połączenie na ciało.
- Ramki tekstowe: JSON UTF-8. Ramki binarne: audio (sekcja 6).
- Heartbeat: ciało wysyła `body.heartbeat` co 1 s. Brak heartbeatu przez 5 s oznacza `link.lost` (wartości początkowe).
- Wersja protokołu w polu `v`. Zmiana niezgodna wstecz podnosi `v`.

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

`link.lost`, `link.restored`, `drive.enter`, `drive.exit`, `host.start`, `host.stop`.

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
- `last_downtime_s` trafia do wycinka, gdy przerwa wynosiła co najmniej `min_downtime_s` (wartość początkowa: 60 s).
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

- Przy starcie host odczytuje ostatnie zdarzenie z poprzedniej sesji i wyznacza `last_downtime_s` jako różnicę między czasem startu a czasem tego zdarzenia. Przy pierwszym uruchomieniu (brak poprzedniej sesji) `last_downtime_s` wynosi 0.
- Zdarzenia i stany zapisywane w SQLite (tabele: `events`, `state_snapshots`, `facts`, `sessions`).
- `facts` ma pole `source` (`stated` lub `inferred`) i `confidence`.
- Użytkownik może wyświetlić, poprawić i usunąć fakty.

## 10. REST

| metoda i ścieżka | opis |
|---|---|
| `GET /self` | aktualny `self` i `body` |
| `GET /self/history?since=<ISO>` | zmiany stanu i zdarzenia |
| `GET /events?type=&since=` | zdarzenia |
| `POST /actions` | wysłanie akcji do ciała (debug) |
| `GET /facts`, `PATCH /facts/{id}`, `DELETE /facts/{id}` | pamięć o użytkowniku |
| `GET /health` | stan hosta i łącza |

## 11. Obsługa błędów

- Nieznany `type` lub `v`: log i odrzucenie, połączenie pozostaje.
- Uszkodzona ramka: odrzucenie, licznik błędów w `body.heartbeat`.
- Utrata łącza: ciało kontynuuje komendy lokalne i buforuje zdarzenia (limit bufora do ustalenia), host oznacza `link_lost`.

## 12. Otwarte kwestie

- Limit bufora zdarzeń w ciele przy braku łącza.
- Format ramki audio po testach na STM32 (Opus na L476 wymaga sprawdzenia pod kątem CPU i RAM).
- Wybór między JSON a CBOR dla zdarzeń z ciała po pomiarze zużycia RAM.
- Zestaw lokalnych komend i ich wzorców.
