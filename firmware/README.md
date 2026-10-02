# Firmware HERMES dla IRIS

Docelowo: STM32L476, C, STM32 HAL/LL, FreeRTOS (decyzja otwarta).

Zadania: nagrywanie INMP441 przez SAI, kodowanie IMA-ADPCM, ramkowanie wg [../docs/protocol.md](../docs/protocol.md), obsługa ESP8266 (AT, tryb transparentny TCP), odtwarzanie przez MAX98357A, komendy lokalne, odczyt czujników.

Kod powstaje w etapie M3 (zob. [../docs/spec.md](../docs/spec.md)).
