# Protokół ramek HERMES ↔ host, wersja 1

Protokół łączy firmware STM32 z bramką `iris-gateway`. Działa nad TCP (ESP8266 w trybie transparentnym), a odcinek STM32-ESP8266 przebiega przez UART.

## Zmiany względem wersji roboczej z poprzednich ustaleń

Poprzedni format: `[0xAA][LEN][CMD][PAYLOAD][CRC8][0x55]`.

| Zmiana | Powód |
|---|---|
| `LEN` 2 bajty zamiast 1 | 1 bajt ogranicza ładunek do 255 B; ramka TTS po 20 ms PCM ma 640 B |
| CRC-16 zamiast CRC8 | TCP chroni odcinek ESP8266-host, ale nie UART STM32-ESP8266; CRC8 wykrywa zbyt mało przekłamań przy ramkach ok. 1 KB |
| Dwubajtowy znacznik początku zamiast bajtu końca | Ładunek binarny może zawierać dowolne bajty, więc bajt końca nie wyznacza granicy ramki; granicę wyznacza `LEN`, a poprawność CRC |
| Pola `VER` i `SEQ` | Zgodność przy zmianach protokołu; wykrywanie zgubionych ramek po resynchronizacji |

## Format ramki

Liczby wielobajtowe w kolejności little-endian.

| Przesunięcie | Pole | Rozmiar [B] | Opis |
|---|---|---|---|
| 0 | `SOF` | 2 | `0xA5 0x5A` |
| 2 | `VER` | 1 | Wersja protokołu, `0x01` |
| 3 | `TYPE` | 1 | Typ ramki (tabela niżej) |
| 4 | `SEQ` | 2 | Licznik ramek, osobny dla każdego kierunku, przepełnienie do 0 |
| 6 | `LEN` | 2 | Długość ładunku, 0–1024 |
| 8 | `PAYLOAD` | `LEN` | Ładunek |
| 8+`LEN` | `CRC` | 2 | CRC-16/CCITT-FALSE z pól `VER` … `PAYLOAD` |

CRC-16/CCITT-FALSE: wielomian `0x1021`, wartość początkowa `0xFFFF`, bez odbicia bitów, bez XOR na wyjściu. Wektor testowy: `"123456789"` → `0x29B1`.

Narzut ramki: 10 B.

### Resynchronizacja

Odbiornik szuka sekwencji `0xA5 0x5A`. Jeżeli `VER` jest nieznany, `LEN` > 1024 albo CRC się nie zgadza, odbiornik odrzuca ramkę i wznawia szukanie `SOF` od bajtu następującego po odrzuconym `SOF`.

## Typy ramek

| `TYPE` | Nazwa | Kierunek | Ładunek |
|---|---|---|---|
| `0x01` | `HELLO` | HERMES → host | `device_id` 8 B, `fw_version` u16, `caps` u16, `nonce` 8 B |
| `0x02` | `HELLO_ACK` | host → HERMES | `nonce_host` 8 B, `mac` 16 B |
| `0x10` | `AUDIO_START` | HERMES → host | `stream_id` u8, `codec` u8, `sample_rate` u16, `trigger` u8 |
| `0x11` | `AUDIO_CHUNK` | HERMES → host | `stream_id` u8, dane audio |
| `0x12` | `AUDIO_END` | HERMES → host | `stream_id` u8, `reason` u8 |
| `0x13` | `AUDIO_STOP_REQ` | host → HERMES | `stream_id` u8 |
| `0x20` | `TTS_START` | host → HERMES | `stream_id` u8, `codec` u8, `sample_rate` u16 |
| `0x21` | `TTS_CHUNK` | host → HERMES | `stream_id` u8, dane audio |
| `0x22` | `TTS_END` | host → HERMES | `stream_id` u8 |
| `0x23` | `PLAYBACK_STOP` | oba | `stream_id` u8 |
| `0x30` | `SENSOR_REQ` | host → HERMES | `count` u8, `sensor_id` u8 × `count` |
| `0x31` | `SENSOR_DATA` | HERMES → host | `count` u8, rekordy po 9 B |
| `0x40` | `LOCAL_CMD_EVENT` | HERMES → host | `cmd_id` u8, `timestamp` u32 (czas RTC, s), `result` u8 |
| `0x41` | `DEVICE_STATUS` | HERMES → host | `battery_mv` u16, `rssi_dbm` i8, `mode` u8 |
| `0x50` | `HEARTBEAT` | HERMES → host | `uptime_ms` u32 |
| `0x51` | `HEARTBEAT_ACK` | host → HERMES | `uptime_ms` u32 (kopia) |
| `0x7F` | `ERROR` | oba | `code` u8, `ref_seq` u16 |

Wartości pól:

- `codec`: `0` PCM 16 bit ze znakiem, `1` IMA-ADPCM,
- `trigger`: `0` słowo budzące, `1` przycisk, `2` wywołanie z hosta,
- `reason`: `0` żądanie hosta, `1` limit czasu (15 s), `2` anulowanie przez użytkownika,
- `mode`: `0` normalny, `1` lokalny (brak hosta), `2` oszczędny.

### Audio

Ramka niesie 20 ms dźwięku przy 16 kHz (320 próbek).

- PCM: 640 B danych.
- IMA-ADPCM: 4 B nagłówka bloku (`predictor` i16, `step_index` u8, `reserved` u8) i 160 B danych, 4 bity na próbkę, młodsza połówka bajtu pierwsza. Każda ramka jest niezależnym blokiem, więc utrata jednej ramki nie psuje dekodowania kolejnych.

Przepływ TTS: host wysyła ramki w tempie odtwarzania z wyprzedzeniem ok. 200 ms. Bufor odtwarzania na STM32: 400 ms (12,8 KB w PCM, 3,2 KB w ADPCM przed dekodowaniem).

### Rekord czujnika (9 B)

| Pole | Typ | Opis |
|---|---|---|
| `sensor_id` | u8 | Tabela niżej |
| `flags` | u8 | bit 0: wartość ważna, bit 1: czujnik niedostępny, bit 2: niska jakość |
| `value` | f32 | Wartość w jednostce z tabeli |
| `age_ms` | u16 | Czas od pomiaru, nasycenie na 65 535 |
| `reserved` | u8 | 0 |

| `sensor_id` | Wielkość | Jednostka | Źródło |
|---|---|---|---|
| `0x01` | Temperatura | °C | BME680 |
| `0x02` | Wilgotność względna | % | BME680 |
| `0x03` | Ciśnienie | hPa | BME680 |
| `0x04` | Rezystancja czujnika gazu | kΩ | BME680 |
| `0x10` | Tętno | bpm | MAX30102 |
| `0x11` | SpO₂ | % | MAX30102 |
| `0x20` | Obecność | 0/1 | LD2410 |
| `0x21` | Odległość celu | m | LD2410 |
| `0x30` | Szerokość geograficzna | ° | GPS |
| `0x31` | Długość geograficzna | ° | GPS |
| `0x40` | Moduł przyspieszenia | m/s² | MPU-6050 |
| `0x50` | Odległość | m | VL53L1X |
| `0x60` | Indeks UV | – | czujnik UV |

Odczyty `0x10` i `0x11` host wysyła do LLM tylko zgodnie z wymaganiem IR-09.

## Uwierzytelnienie

1. HERMES po połączeniu TCP wysyła `HELLO` z losowym `nonce`.
2. Host odpowiada `HELLO_ACK`: `mac` = pierwsze 16 B z HMAC-SHA256(klucz, `nonce` ‖ `nonce_host` ‖ `device_id`).
3. HERMES sprawdza `mac`. Od tej chwili obie strony przyjmują pozostałe typy ramek; wcześniej host przyjmuje tylko `HELLO`.

Mechanizm potwierdza, że obie strony znają klucz. Nie szyfruje transmisji; audio w sieci lokalnej jest jawne. Szyfrowanie (np. AES-CCM na ładunku) jest kwestią otwartą ze względu na koszt na MCU.

## Liczniki czasu

| Zdarzenie | Wartość |
|---|---|
| Interwał `HEARTBEAT` | 1 s |
| Utrata hosta | 3 kolejne `HEARTBEAT` bez `HEARTBEAT_ACK` |
| Limit wypowiedzi | 15 s od `AUDIO_START` |
| Ponowne wyszukiwanie hosta (mDNS) | co 5 s w trybie lokalnym |

Implementacja wzorcowa po stronie hosta: [../host/iris_protocol.py](../host/iris_protocol.py).
