/* Klient WebSocket (RFC 6455) dla łącza ciało-host (docs/spec.md, sekcja 1).
 *
 * Bez gniazd i bez malloc. Port dostarcza transport bajtów (gniazdo TCP, moduł Wi-Fi
 * z komendami AT, W5500), woła fw_ws_feed() z odebranymi bajtami i fw_ws_tick() w pętli
 * głównej. Każde udane fw_ws_start() kończy dokładnie jedno wywołanie on_close; potem port
 * zamyka transport. Rozszerzenia (permessage-deflate) i podprotokoły nie są obsługiwane.
 */
#ifndef FW_WS_H
#define FW_WS_H

#include <stddef.h>
#include <stdint.h>

#include "../core/fw_config.h"

/* Kody zamknięcia (RFC 6455, sekcja 7.4.1) */
#define FW_WS_NORMAL 1000u
#define FW_WS_PROTOCOL_ERROR 1002u
#define FW_WS_NO_STATUS 1005u /* ramka close bez kodu; nie trafia do sieci */
#define FW_WS_ABNORMAL 1006u  /* koniec bez ramki close; nie trafia do sieci */
#define FW_WS_INVALID_DATA 1007u
#define FW_WS_INTERNAL_ERROR 1011u

#define FW_WS_FRAME_HEADER_MAX 8u /* ramka klienta do 65535 B: 2 + 2 (długość) + 4 (maska) */
#define FW_WS_TX_MAX (FW_WS_FRAME_HEADER_MAX + FW_WS_MSG_MAX)

typedef struct {
    void *ctx;
    /* Zapisuje wszystkie bajty: 0 = zapisano, inna wartość = transport zerwany. */
    int (*write)(void *ctx, const uint8_t *data, size_t len);
    uint32_t (*random32)(void *ctx); /* klucz handshake i maski ramek (RFC 6455, sekcja 10.3) */
    void (*on_open)(void *ctx);
    void (*on_message)(void *ctx, int binary, const uint8_t *data, size_t len);
    void (*on_oversized)(void *ctx); /* wiadomość większa niż FW_WS_MSG_MAX, odrzucona */
    void (*on_close)(void *ctx, uint16_t code);
} fw_ws_io_t;

typedef enum {
    FW_WS_CLOSED,
    FW_WS_HANDSHAKE,
    FW_WS_OPEN,
    FW_WS_CLOSING /* wysłaliśmy close, czekamy na odpowiedź */
} fw_ws_state_t;

typedef struct {
    fw_ws_io_t io;
    fw_ws_state_t state;
    const char *error; /* przyczyna ostatniego zamknięcia, do logów; NULL przy czystym */
    uint16_t close_code;
    int close_pending; /* on_close czeka na fw_ws_tick() albo koniec fw_ws_feed() */
    char accept[29];   /* oczekiwany Sec-WebSocket-Accept */
    uint32_t state_ms; /* start handshake albo wysłanie close */
    uint32_t last_rx_ms;
    int ping_sent;
    uint32_t oversized; /* licznik odrzuconych wiadomości */

    /* parser ramek */
    uint8_t hdr[14];
    uint8_t hdr_len;
    uint8_t hdr_need;
    int in_payload;
    uint8_t opcode;
    int fin;
    uint64_t payload_len;
    uint64_t payload_got;
    int msg_active; /* trwa wiadomość (także pofragmentowana) */
    int msg_binary;
    int msg_drop;   /* wiadomość za duża: reszta jest pomijana */
    size_t msg_len;
    size_t hs_len;  /* bajty odpowiedzi na handshake w rx */
    uint8_t ctrl[125];
    uint8_t rx[FW_WS_MSG_MAX];
    uint8_t tx[FW_WS_TX_MAX];
} fw_ws_t;

/* Wysyła żądanie handshake. host to wartość nagłówka Host (z portem, gdy inny niż 80).
 * Zwraca 0 albo -1 (za długie host/path, błąd zapisu); przy -1 on_close nie będzie wołane. */
int fw_ws_start(fw_ws_t *ws, const fw_ws_io_t *io, const char *host, const char *path, uint32_t now_ms);
void fw_ws_feed(fw_ws_t *ws, const uint8_t *data, size_t len, uint32_t now_ms);
void fw_ws_tick(fw_ws_t *ws, uint32_t now_ms);
/* Wiadomość w jednej ramce. 0 albo -1, gdy połączenie nie jest otwarte. Błąd zapisu zamyka
 * połączenie, a on_close przychodzi z fw_ws_tick(), nie z wnętrza fw_ws_send(). */
int fw_ws_send(fw_ws_t *ws, int binary, const uint8_t *data, size_t len);
/* Zamknięcie z naszej strony: ramka close i czekanie na odpowiedź do FW_WS_CLOSE_MS. */
void fw_ws_close(fw_ws_t *ws, uint16_t code, uint32_t now_ms);
/* Port zgłasza zerwanie transportu (TCP zamknięte, błąd odczytu). */
void fw_ws_transport_lost(fw_ws_t *ws);

/* Pomocnicze, także dla testów. */
void fw_sha1(const uint8_t *data, size_t len, uint8_t out[20]);
size_t fw_base64(const uint8_t *data, size_t len, char *out); /* out: 4 * ceil(len / 3) + 1 */
int fw_utf8_valid(const uint8_t *s, size_t len);

#endif
