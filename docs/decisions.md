# Rejestr decyzji architektonicznych

Każda decyzja ma status: przyjęta, do weryfikacji albo zastąpiona.

## D-001. OpenJarvis jako podstawa IRIS Core

Status: przyjęta.

Framework ma moduły STT, LLM (chmura i lokalne), agenta z narzędziami, pamięć, harmonogram i konektory kalendarza. Licencja Apache 2.0. Ryzyko: duży zakres frameworka i szybkie zmiany API; IRIS korzysta z niego przez Python SDK w jednym miejscu (`iris-gateway`), aby ograniczyć zależność.

## D-002. Surowy TCP z własnymi ramkami zamiast WebSocket na MCU

Status: przyjęta. Zastępuje ustalenie z wersji 1 specyfikacji.

ESP8266 sterowany komendami AT udostępnia gniazdo TCP w trybie transparentnym. Klient WebSocket wymagałby na STM32 handshake HTTP, maskowania ramek i obsługi ramek kontrolnych. Bramka na hoście tłumaczy ramki na wywołania OpenJarvis.

## D-003. CRC-16 w ramce mimo TCP

Status: przyjęta.

TCP chroni odcinek ESP8266-host. Odcinek UART STM32-ESP8266 nie ma kontroli błędów.

## D-004. Piper jako bazowy silnik TTS

Status: do weryfikacji (test odsłuchowy w M1).

Oficjalny Kokoro nie obsługuje polskiego. Piper ma głosy `pl_PL` i działa lokalnie na CPU. Porównanie: Piper, społecznościowy Kokoro-PL, jedna usługa chmurowa.

## D-005. Kodek IMA-ADPCM, Opus odłożony

Status: przyjęta dla M3.

ADPCM: stopień kompresji 4:1, koszt obliczeniowy pomijalny na Cortex-M4. Opus daje mniejszy strumień, ale zajmuje istotną część 128 KB SRAM. Powrót do tematu, jeśli przepustowość UART lub WiFi okaże się ograniczeniem.

## D-006. Dane z czujników jako narzędzie agenta

Status: przyjęta.

Agent pobiera odczyty narzędziem `hermes_device_sensors` tylko wtedy, gdy są potrzebne. Ogranicza to liczbę tokenów i przesyłanie danych fizjologicznych do chmury (IR-09).

## D-007. Słowo budzące przez DF2301Q w pierwszym etapie

Status: do weryfikacji (IR-01 w M4).

Moduł jest już w projekcie HERMES i obsługuje komendy wyuczone głosem. Jeżeli nie spełni IR-01, kolejnym krokiem jest model KWS uruchamiany na STM32.

## D-008. Jeden aktywny host z bazą SQLite

Status: przyjęta. Zastępuje synchronizację przez Syncthing.

Synchronizacja otwartej bazy SQLite między urządzeniami grozi jej uszkodzeniem. Przy zmianie hosta przenoszona jest kopia bazy wykonana poleceniem `sqlite3 .backup`.

## D-009. IRIS poza ścieżką bezpieczeństwa HERMES

Status: przyjęta.

Funkcje IRIS nie mogą wpływać na alarmy fizjologiczne ani na kanał bezpieczeństwa opisany w dokumentacji HERMES rev B.
