# IRIS: specyfikacja funkcjonalna i architektura, wersja 2

Dokument roboczy. Wartości liczbowe w wymaganiach są docelowe i wymagają pomiaru.

## 1. Cel i zakres

IRIS to osobisty asystent głosowy prowadzący swobodną rozmowę po polsku. System składa się z dwóch części:

- HERMES: urządzenie z mikrokontrolerem STM32L476, mikrofonem, głośnikiem, czujnikami i modułami łączności,
- IRIS Core: oprogramowanie hosta (laptop, Raspberry Pi), oparte na frameworku OpenJarvis.

Host zwykle ma dostęp do sieci. Praca bez hosta lub bez internetu jest trybem awaryjnym ograniczonym do komend lokalnych.

## 2. Zmiany względem wersji 1

| Obszar | Wersja 1 | Wersja 2 | Powód |
|---|---|---|---|
| Łącze HERMES-host | Klient WebSocket na STM32, `ws_bridge` OpenJarvis | Surowy TCP z własnym protokołem ramek, bramka `iris-gateway` na hoście | STM32L476 nie ma WiFi; ESP8266 sterowany komendami AT. Klient WebSocket wymagałby własnej implementacji handshake i maskowania ramek na MCU. Przeznaczenie `ws_bridge` w OpenJarvis nie zostało sprawdzone w kodzie. |
| TTS | Kokoro lub Cartesia | Piper (`pl_PL`) jako bazowy, pozostałe do porównania | Oficjalny Kokoro nie obsługuje polskiego; istnieje społecznościowy fine-tuning (Software Mansion, `KOKORO_POLISH`). |
| Interfejs audio | I2S1, I2S2 | SAI1 (bloki A i B) lub SAI1 i SAI2 | Rodzina STM32L4 realizuje audio przez SAI; SPI w L476 nie ma trybu I2S. Do potwierdzenia w RM0351. |
| Słowo budzące | Model na STM32 lub Porcupine | Etap 1: moduł DF2301Q z wyuczonym słowem; etap 2: ocena modelu KWS na MCU | Iduino SE019 jest detektorem poziomu dźwięku i nie rozróżnia słów. |
| Kodowanie audio | Opus | PCM 16 bit w M2, IMA-ADPCM od M3; Opus odłożony | ADPCM ma pomijalny koszt obliczeniowy na Cortex-M4 i redukuje strumień 4:1; libopus wymaga istotnej części 128 KB SRAM. |
| Kontekst z czujników | Dołączany do każdego zapytania | Udostępniany jako narzędzie, które agent wywołuje w razie potrzeby | Mniej tokenów; dane fizjologiczne (MAX30102) nie trafiają do chmury bez potrzeby. |
| Protokół | `LEN` 1 bajt, CRC8 | `LEN` 2 bajty, CRC-16, numer sekwencyjny, wersja | Zob. [protocol.md](protocol.md). |
| Synchronizacja pamięci | Syncthing na plikach SQLite | Jeden aktywny host, kopia bazy przy zmianie hosta | Synchronizacja otwartego pliku SQLite grozi uszkodzeniem bazy. |

## 3. Funkcje

| Domena | Przykład | Wykonanie |
|---|---|---|
| Rozmowa i pytania | "Jak działa silnik indukcyjny?" | Host, LLM |
| Dom | "Zapal światło w salonie" | Lokalnie, jeśli komenda jest na liście komend lokalnych; w pozostałych przypadkach narzędzie agenta |
| Przypomnienia, notatki, kalendarz | "Przypomnij mi za godzinę o praniu" | Host, narzędzie i baza SQLite |
| Czujniki | "Czy potrzebuję kurtki?" | Odczyt na HERMES, interpretacja przez LLM po wywołaniu narzędzia |
| Stan urządzenia | "Ile zostało baterii?" | Host, narzędzie `hermes_device_status` |

Komenda trafia do obsługi lokalnej tylko wtedy, gdy jednocześnie:

1. należy do zamkniętej listy (tabela 5.2),
2. jej wykonanie jest deterministyczne i dotyczy znanego urządzenia,
3. opóźnienie przez host byłoby uciążliwe albo host jest niedostępny.

## 4. Architektura

```
HERMES (STM32L476)                              HOST (IRIS Core)
+-----------------------------+                 +--------------------------------+
| INMP441 --SAI--> bufor      |                 | iris-gateway (TCP, ramki)      |
| VAD energetyczny, ADPCM     |                 |   dekodowanie ADPCM            |
| DF2301Q: słowo budzące,     |   UART   WiFi   |   VAD (webrtcvad)              |
|   komendy lokalne           | <----> ESP8266  |        |                       |
| MAX98357A <--SAI-- TTS      |   AT, TCP <---> |   faster-whisper (STT, pl)     |
| DFPlayer: dźwięki systemowe |                 |        |                       |
| Czujniki (I2C, UART, SPI)   |                 |   OpenJarvis: agent            |
| GPIO / przekaźniki          |                 |     orchestrator + narzędzia   |
| Heartbeat, tryb awaryjny    |                 |     silnik: Claude / Ollama    |
+-----------------------------+                 |     pamięć: SQLite             |
                                                |        |                       |
                                                |   Piper (TTS, pl_PL)           |
                                                |   mDNS: _iris._tcp.local       |
                                                +--------------------------------+
```

`iris-gateway` jest jedynym elementem, który zna protokół ramek. Z OpenJarvis korzysta przez Python SDK (`Jarvis().ask_full(...)`), więc zmiany w serwerze HTTP OpenJarvis nie wpływają na firmware.

## 5. Przepływy

### 5.1. Zapytanie przez host

1. DF2301Q rozpoznaje słowo budzące i zgłasza je do STM32.
2. STM32 wysyła `AUDIO_START`, następnie ramki `AUDIO_CHUNK` po 20 ms; DFPlayer odtwarza krótki sygnał potwierdzenia.
3. Koniec wypowiedzi wykrywa VAD na hoście; host wysyła `AUDIO_STOP_REQ`. Limit wypowiedzi na STM32: 15 s.
4. Host: faster-whisper zwraca transkrypcję.
5. Host: agent otrzymuje transkrypcję, historię sesji, czas i listę narzędzi. Dane z czujników pobiera narzędziem.
6. Host: odpowiedź jest dzielona na zdania; każde zdanie przechodzi przez Piper i trafia do HERMES jako `TTS_CHUNK`, zanim LLM skończy generować całość.
7. STM32 odtwarza audio przez MAX98357A.

### 5.2. Komenda lokalna

1. DF2301Q rozpoznaje komendę z listy i przekazuje jej identyfikator.
2. STM32 wykonuje akcję i odtwarza potwierdzenie z DFPlayera.
3. STM32 wysyła `LOCAL_CMD_EVENT` do hosta albo kolejkuje zdarzenie do czasu odzyskania łącza.

Propozycja listy początkowej: zapal i zgaś światło, która godzina, temperatura w pomieszczeniu, cisza (przerwanie odtwarzania), głośniej, ciszej. Odczyt godziny i temperatury wymaga nagranych wcześniej plików z liczbami na karcie DFPlayera.

### 5.3. Utrata hosta

Brak trzech kolejnych odpowiedzi na `HEARTBEAT` (interwał 1 s) oznacza utratę hosta. HERMES odtwarza komunikat o pracy lokalnej, przechodzi do trybu komend lokalnych i co 5 s ponawia wyszukiwanie przez mDNS.

## 6. Wymagania

| ID | Wymaganie | Kryterium | Weryfikacja |
|---|---|---|---|
| IR-01 | Wykrywanie słowa budzącego | ≥ 95% skuteczności z 1 m w cichym pomieszczeniu; ≤ 1 fałszywe wybudzenie na 8 h przy włączonym radiu lub TV | Test 100 wywołań, nagranie 8 h |
| IR-02 | Opóźnienie odpowiedzi | Od końca wypowiedzi do początku dźwięku odpowiedzi: mediana ≤ 3 s, p95 ≤ 5 s | Pomiar znaczników czasu w bramce |
| IR-03 | Opóźnienie komendy lokalnej | ≤ 0,5 s od końca komendy do wykonania akcji | Pomiar oscyloskopem lub analizatorem logicznym |
| IR-04 | Jakość STT po polsku | WER ≤ 15% na własnym zbiorze 100 wypowiedzi nagranych mikrofonem HERMES | Skrypt porównujący z transkrypcją wzorcową |
| IR-05 | Wykrycie utraty hosta | ≤ 4 s, sygnalizacja dźwiękowa | Odłączenie hosta w trakcie pracy |
| IR-06 | Komendy lokalne bez hosta | Wszystkie komendy z listy 5.2 działają przy wyłączonym hoście | Test funkcjonalny |
| IR-07 | Zmiana hosta | HERMES łączy się z nowym hostem ≤ 30 s bez zmiany konfiguracji | Test z dwoma hostami |
| IR-08 | Akcje o skutkach fizycznych | Urządzenia z kategorii "wymaga potwierdzenia" (np. grzejnik, zamek) są przełączane dopiero po potwierdzeniu głosowym | Test scenariuszowy |
| IR-09 | Dane fizjologiczne | Odczyty MAX30102 trafiają do LLM wyłącznie przez narzędzie wywołane w odpowiedzi na pytanie użytkownika | Przegląd logów zapytań |
| IR-10 | Przechowywanie danych | Historia rozmów i transkrypcje tylko na hoście; okres przechowywania konfigurowalny | Przegląd konfiguracji |

### 6.1. Budżet opóźnienia (szacunek do weryfikacji)

| Etap | Szacunek | Uwagi |
|---|---|---|
| Wykrycie końca wypowiedzi (VAD) | 0,5–0,8 s | Parametr ciszy kończącej |
| STT (faster-whisper, model small) | 0,5–1,5 s | Silnie zależy od CPU/GPU hosta; do zmierzenia na RPi 5 i laptopie |
| LLM do pierwszego zdania | 0,5–1,5 s | Zależy od modelu i obciążenia API |
| TTS pierwszego zdania (Piper) | 0,1–0,4 s | |
| Transmisja i start odtwarzania | 0,1–0,2 s | |
| Razem | 1,7–4,4 s | Górna granica przekracza IR-02; decyduje wybór modelu STT i hosta |

## 7. Sprzęt HERMES

| Element | Interfejs | Rola w IRIS |
|---|---|---|
| STM32L476 (Cortex-M4F, 1 MB Flash, 128 KB SRAM) | – | Sterowanie, bufory audio, ramkowanie, komendy lokalne |
| INMP441 | SAI, protokół I2S, DMA | Mikrofon, 16 kHz |
| MAX98357A | SAI, protokół I2S, DMA | Wzmacniacz i głośnik |
| ESP8266 | UART, AT, tryb transparentny TCP | Łącze z hostem |
| DF2301Q | I2C (zalecane) lub UART | Słowo budzące i komendy lokalne |
| DFPlayer Mini | UART | Dźwięki systemowe bez hosta |
| HC-05 | UART | Zapasowe łącze lub konfiguracja z telefonu; poza ścieżką audio |
| BME680, VL53L1X, MPU-6050, MAX30102, RTC | I2C | Czujniki |
| LD2410 | UART | Obecność |
| GPS | UART | Lokalizacja |
| TFT, RFID, karta SD | SPI | Wyświetlanie, identyfikacja, zapis |

Do sprawdzenia w projekcie płytki:

- liczba UART: L476 ma USART1–3, UART4–5 i LPUART1. ESP8266, HC-05, DFPlayer, LD2410 i GPS zajmują pięć z nich; DF2301Q należy podłączyć przez I2C, a konsolę debug przenieść na SWO;
- przepustowość UART do ESP8266: PCM 16 kHz/16 bit to 32 KB/s, ADPCM 8 KB/s; przy 921 600 bit/s pasmo wynosi ok. 92 KB/s;
- zasilanie: ESP8266EX pobiera do ok. 170 mA podczas nadawania 802.11b (karta katalogowa); wymagany kondensator buforowy i osobna gałąź stabilizatora.

## 8. Host: komponenty

| Funkcja | Komponent | Status |
|---|---|---|
| Bramka i protokół | `iris-gateway` (własny, asyncio) | Do napisania |
| VAD | webrtcvad | Do integracji |
| STT | faster-whisper (`openjarvis/speech/faster_whisper.py`) | Do testu jakości PL |
| Agent i narzędzia | OpenJarvis `orchestrator` | Do testu z polskimi poleceniami |
| LLM | Claude przez `engine/cloud.py` lub `engine/litellm.py`; Ollama jako zapas | Do konfiguracji |
| Pamięć | OpenJarvis `memory/` na SQLite | Do oceny |
| Przypomnienia | OpenJarvis `scheduler/` | Do oceny |
| Kalendarz | OpenJarvis `connectors/gcalendar` | Do oceny |
| TTS | Piper `pl_PL`; porównanie z Kokoro-PL i usługą chmurową | Test odsłuchowy |
| Wykrywanie hosta | zeroconf, `_iris._tcp.local` | Do napisania |

Nazwa `hermes` w OpenJarvis oznacza źródło umiejętności z projektu Hermes Agent (Nous Research) i nie ma związku z urządzeniem HERMES. Własne narzędzia mają prefiks `hermes_device_`, aby uniknąć pomyłek.

## 9. Narzędzia agenta

| Narzędzie | Działanie |
|---|---|
| `hermes_device_sensors(names)` | Ostatnie odczyty wskazanych czujników z czasem pomiaru i flagą jakości |
| `hermes_device_status()` | Bateria, siła sygnału WiFi, tryb pracy |
| `home_set(device, state, level)` | Zmiana stanu urządzenia; kategoria "wymaga potwierdzenia" zgodnie z IR-08 |
| `home_get(device)` | Stan urządzenia |
| `reminder_add`, `reminder_list`, `reminder_delete` | Przypomnienia; wywołanie przypomnienia wysyła `TTS_CHUNK` do HERMES |
| `note_add`, `note_search` | Notatki |
| `calendar_query` | Wydarzenia z kalendarza |
| `web_search` | Wbudowane w OpenJarvis |

Backend domu (Home Assistant REST API albo bezpośrednio MQTT/Tasmota) jest kwestią otwartą. Home Assistant oddziela agenta od protokołów poszczególnych urządzeń.

## 10. Prywatność i bezpieczeństwo

- Do chmurowego LLM trafia transkrypcja, historia sesji i wyniki wywołanych narzędzi. Audio nie opuszcza hosta.
- Odczyty fizjologiczne (tętno, SpO₂) są danymi o zdrowiu; obowiązuje IR-09.
- Bramka przyjmuje połączenia tylko z sieci lokalnej; parowanie HERMES z hostem kluczem wspólnym zapisanym w obu urządzeniach (wymiana `HELLO` z HMAC, zob. [protocol.md](protocol.md)).
- Narzędzia z efektem fizycznym mają listę dozwolonych urządzeń; agent nie może wywołać akcji na urządzeniu spoza listy.
- Klucz API przechowywany w pliku `.env` poza repozytorium.

## 11. Relacja z dokumentacją HERMES rev B

Dokumentacja HERMES rev B opisuje system monitorowania operatora na mikrokontrolerach Microchip (SAMV71, SAMD21) z niezależnym kanałem bezpieczeństwa. STM32L476 występuje tam jako prototyp koncepcyjny etapu 1, który nie jest bazą wariantów kwalifikowanych. IRIS rozwijany jest na tej linii prototypowej.

Jeśli IRIS miałby działać na sprzęcie rev B:

- funkcje IRIS należą do kanału głównego (misji), nigdy do kanału bezpieczeństwa,
- alarmy fizjologiczne nie mogą zależeć od hosta ani od LLM,
- w wariancie HERMES-S sterowanie głosowe idzie przez tor audio skafandra, a nie przez mikrofon urządzenia,
- ciągła transmisja WiFi musi zmieścić się w budżecie NFR-05 (średnio ≤ 1,5 W dla modułów C i S).

## 12. Kwestie otwarte

1. Backend domu: Home Assistant czy MQTT bezpośrednio.
2. Słowo budzące: wynik testu DF2301Q (IR-01) decyduje, czy potrzebny jest model KWS na MCU.
3. TTS: wybór po teście odsłuchowym Piper, Kokoro-PL i jednej usługi chmurowej.
4. Host minimalny: czy RPi 5 spełnia IR-02 z faster-whisper small.
5. Model LLM i koszt miesięczny przy typowym użyciu.
6. FreeRTOS czy pętla z przerwaniami w firmware.
7. Rozwinięcie nazwy IRIS.

## 13. Plan prac

| Etap | Zakres | Kryterium ukończenia |
|---|---|---|
| M0 | OpenJarvis lokalnie, silnik Claude, czat tekstowy PL | 20 pytań testowych po polsku obsłużonych przez `jarvis ask` |
| M1 | Mikrofon laptopa, webrtcvad, faster-whisper, agent, Piper | Pomiar IR-02 i IR-04 na hoście |
| M2 | `iris-gateway`, protokół v1, symulator HERMES w Pythonie odtwarzający pliki WAV | Rozmowa przez symulator; poprawna reakcja na ramki z błędnym CRC i utratę połączenia |
| M3 | Firmware: SAI + DMA, ADPCM, ESP8266 TCP, odtwarzanie TTS | Rozmowa przez HERMES z przyciskiem zamiast słowa budzącego |
| M4 | DF2301Q, komendy lokalne, heartbeat, mDNS | IR-01, IR-03, IR-05, IR-06, IR-07 |
| M5 | Narzędzia: czujniki, dom, przypomnienia, kalendarz | IR-08, IR-09, IR-10 |
