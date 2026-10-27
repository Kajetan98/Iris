/* Parametry rdzenia firmware HERMES (docs/spec.md). Wartości liczbowe to wartości początkowe.
 *
 * Budżet RAM (STM32L476: 128 KB): stan fw_body_t to 71 968 B (sprawdza test_ram_budget):
 * bufor odtwarzania 53 248 B (dane 48 KB + tablice seq), bufor zdarzeń 16 512 B
 * (64 x 258 B), reszta ok. 2 KB. Klient WebSocket fw_ws_t to 2 904 B (test_ram w test_ws.c):
 * odbiór i wysyłka po FW_WS_MSG_MAX. Na sterownik modułu sieciowego, koder Opus i stos
 * zostaje 56 200 B (ok. 55 KB). RAM L476 to SRAM1 (96 KB) i SRAM2 (32 KB), więc fw_body_t
 * musi trafić do SRAM1.
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

/* Łącze WebSocket (spec, sekcja 1) */
#define FW_WS_MSG_MAX 1280u           /* największa wiadomość: 5 B nagłówka audio + 1275 B Opus */
#define FW_WS_HANDSHAKE_MS 5000u      /* połączenie TCP i odpowiedź na handshake */
#define FW_WS_PING_MS 5000u           /* ping po tylu ms bez danych od hosta */
#define FW_WS_DEAD_MS 10000u          /* zerwanie po tylu ms bez danych */
#define FW_WS_CLOSE_MS 1000u          /* czekanie na odpowiedź na naszą ramkę close */
#define FW_RECONNECT_MIN_MS 500u      /* ponowne połączenie, opóźnienie podwajane */
#define FW_RECONNECT_MAX_MS 5000u

#define FW_GPIO_MAX 8u
#define FW_CLIPS_MAX 8u

#endif
