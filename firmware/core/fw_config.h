/* Parametry rdzenia firmware HERMES (docs/spec.md). Wartości liczbowe to wartości początkowe.
 *
 * Budżet RAM (STM32L476: 128 KB): stan fw_body_t to 71 960 B (sprawdza test_ram_budget):
 * bufor odtwarzania 53 248 B (dane 48 KB + tablice seq), bufor zdarzeń 16 512 B
 * (64 x 258 B), reszta ok. 2 KB. Na stos sieciowy, koder Opus i stos zostaje ok. 56 KB.
 */
#ifndef FW_CONFIG_H
#define FW_CONFIG_H

#define FW_VERSION "hermes-0.1.0"
#define FW_PROTOCOL_VERSION 1

#define FW_RX_TEXT_MAX 512u   /* ramka tekstowa od hosta (spec, sekcja 5) */
#define FW_TX_TEXT_MAX 256u   /* największa ramka zdarzenia */
#define FW_JSON_MAX_TOKENS 48 /* ramka akcji ma kilkanaście tokenów */
#define FW_JSON_MAX_DEPTH 4
#define FW_ID_MAX 32u         /* id koperty od hosta */
#define FW_TYPE_MAX 32u       /* typ akcji */
#define FW_TEXT_MAX 48u       /* kody usterek, komendy lokalne, nazwy klipów */

#define FW_OUTBOX_LEN 64u     /* zdarzenia buforowane bez łącza (spec, sekcja 13) */

#define FW_PLAYBACK_BYTES (48u * 1024u)  /* ok. 15 s przy 24 kbit/s (spec, sekcja 6) */
#define FW_PLAYBACK_MAX_PACKETS 1024u    /* 20 s w pakietach po 20 ms */

#define FW_HEARTBEAT_MS 1000u
#define FW_SENSOR_PERIOD_MS 30000u
#define FW_TEMP_DELTA_X10 5   /* zmiana temperatury o 0,5 C wysyła odczyt */

#define FW_GPIO_MAX 8u
#define FW_CLIPS_MAX 8u

#endif
