/* Testy klienta WebSocket (RFC 6455) na atrapie transportu. make test uruchamia je pod
 * ASan/UBSan, make valgrind pod valgrindem. Zgodność z prawdziwym serwerem sprawdza
 * host/tests/test_firmware_posix.py. */
#include <stdio.h>
#include <string.h>

#include "../net/fw_ws.h"

static int checks;
static int failures;

#define CHECK(cond)                                                          \
    do {                                                                     \
        checks++;                                                            \
        if (!(cond)) {                                                       \
            failures++;                                                      \
            fprintf(stderr, "%s:%d: CHECK(%s)\n", __FILE__, __LINE__, #cond); \
        }                                                                    \
    } while (0)

#define CHECK_STR(a, b) CHECK((a) != NULL && strcmp((a), (b)) == 0)

/* ---------- atrapa transportu i strona serwera ---------- */

typedef struct {
    uint8_t out[16384];
    size_t out_len;
    size_t out_read; /* ile ramek klienta już zdekodowano */
    int fail_write;
    uint32_t rnd;
    int opened;
    int closes;
    uint16_t close_code;
    int messages;
    int last_binary;
    uint8_t last[FW_WS_MSG_MAX];
    size_t last_len;
    int oversized;
    int in_callback;
    int close_in_callback;
    int reply_on_message; /* on_message odpowiada ramką, jak rdzeń wysyłający ack */
} wt_t;

static fw_ws_t ws;
static wt_t t;

static int wt_write(void *ctx, const uint8_t *data, size_t len)
{
    wt_t *w = ctx;
    if (w->fail_write) {
        return -1;
    }
    if (w->out_len + len <= sizeof(w->out)) {
        memcpy(w->out + w->out_len, data, len);
        w->out_len += len;
    }
    return 0;
}

static uint32_t wt_random(void *ctx)
{
    wt_t *w = ctx;
    w->rnd = w->rnd * 1664525u + 1013904223u;
    return w->rnd;
}

static void wt_open(void *ctx)
{
    ((wt_t *)ctx)->opened++;
}

static void wt_message(void *ctx, int binary, const uint8_t *data, size_t len)
{
    wt_t *w = ctx;
    w->messages++;
    w->last_binary = binary;
    memcpy(w->last, data, len);
    w->last_len = len;
    if (w->reply_on_message) {
        w->in_callback = 1;
        (void)fw_ws_send(&ws, 0, (const uint8_t *)"ack", 3);
        w->in_callback = 0;
    }
}

static void wt_oversized(void *ctx)
{
    ((wt_t *)ctx)->oversized++;
}

static void wt_close(void *ctx, uint16_t code)
{
    wt_t *w = ctx;
    w->closes++;
    w->close_code = code;
    w->close_in_callback |= w->in_callback;
}

static const fw_ws_io_t io = {&t, wt_write, wt_random, wt_open, wt_message, wt_oversized, wt_close};

/* Ramka serwera (bez maski) z wybraną długością nagłówka: 0 = minimalna, 2 lub 8. */
static size_t srv_frame_ext(uint8_t *out, uint8_t b0, const void *payload, size_t len, int ext)
{
    size_t n = 0;
    unsigned k;
    out[n++] = b0;
    if (ext == 0 && len < 126) {
        out[n++] = (uint8_t)len;
    } else if (ext == 2 || (ext == 0 && len <= 0xFFFF)) {
        out[n++] = 126;
        out[n++] = (uint8_t)(len >> 8);
        out[n++] = (uint8_t)len;
    } else {
        out[n++] = 127;
        for (k = 0; k < 8; k++) {
            out[n++] = (uint8_t)((uint64_t)len >> (56 - 8 * k));
        }
    }
    if (len) {
        memcpy(out + n, payload, len);
    }
    return n + len;
}

static size_t srv_frame(uint8_t *out, uint8_t b0, const void *payload, size_t len)
{
    return srv_frame_ext(out, b0, payload, len, 0);
}

static void feed(const void *data, size_t len, uint32_t now)
{
    fw_ws_feed(&ws, data, len, now);
}

static void feed_frame(uint8_t b0, const void *payload, size_t len)
{
    static uint8_t buf[70000];
    feed(buf, srv_frame(buf, b0, payload, len), 1000);
}

/* Kolejna ramka klienta: sprawdza maskę, odmaskowuje. Zwraca opcode albo -1. */
static int client_frame(uint8_t *payload, size_t *len)
{
    const uint8_t *p = t.out + t.out_read;
    size_t avail = t.out_len - t.out_read;
    size_t n;
    size_t hdr;
    size_t i;
    if (avail < 6 || !(p[0] & 0x80u) || !(p[1] & 0x80u) || (p[0] & 0x70u)) {
        return -1; /* zawsze FIN, zawsze maska, bez RSV */
    }
    n = p[1] & 0x7Fu;
    hdr = 2;
    if (n == 126) {
        n = (size_t)p[2] << 8 | p[3];
        hdr = 4;
        if (n < 126) {
            return -1; /* długość nie minimalna */
        }
    } else if (n == 127) {
        return -1; /* klient nie wysyła ramek > 65535 B */
    }
    if (avail < hdr + 4 + n) {
        return -1;
    }
    for (i = 0; i < n; i++) {
        payload[i] = (uint8_t)(p[hdr + 4 + i] ^ p[hdr + (i & 3u)]);
    }
    *len = n;
    t.out_read += hdr + 4 + n;
    return p[0] & 0x0F;
}

static int client_close_code(void)
{
    uint8_t p[200];
    size_t n;
    if (client_frame(p, &n) != 0x8 || n != 2) {
        return -1;
    }
    return p[0] << 8 | p[1];
}

static int no_more_client_frames(void)
{
    return t.out_read == t.out_len;
}

static const char *find_key(char *key)
{
    const char *k = strstr((const char *)t.out, "Sec-WebSocket-Key: ");
    if (!k) {
        return NULL;
    }
    memcpy(key, k + 19, 24);
    key[24] = '\0';
    return key;
}

static void accept_for(const char *key, char *accept)
{
    char joined[64];
    uint8_t digest[20];
    snprintf(joined, sizeof(joined), "%s258EAFA5-E914-47DA-95CA-C5AB0DC85B11", key);
    fw_sha1((const uint8_t *)joined, strlen(joined), digest);
    fw_base64(digest, 20, accept);
}

static size_t response(char *buf, size_t cap, const char *extra)
{
    char key[25];
    char accept[29];
    accept_for(find_key(key), accept);
    return (size_t)snprintf(buf, cap,
                            "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                            "Sec-WebSocket-Accept: %s\r\n%s\r\n",
                            accept, extra);
}

static void start(void)
{
    memset(&t, 0, sizeof(t));
    t.rnd = 12345;
    CHECK(fw_ws_start(&ws, &io, "127.0.0.1:8000", "/ws", NULL, 1000) == 0);
}

/* Połączenie po handshake, bufor wyjścia pusty. */
static void open_ws(void)
{
    char resp[512];
    start();
    feed(resp, response(resp, sizeof(resp), ""), 1000);
    CHECK(ws.state == FW_WS_OPEN && t.opened == 1);
    t.out_len = t.out_read = 0;
}

/* ---------- SHA-1, base64, UTF-8 ---------- */

static void hex(const uint8_t *d, char *out)
{
    int i;
    for (i = 0; i < 20; i++) {
        snprintf(out + 2 * i, 3, "%02x", d[i]);
    }
}

static void check_sha1(const void *data, size_t len, const char *expected)
{
    uint8_t d[20];
    char h[41];
    fw_sha1(data, len, d);
    hex(d, h);
    CHECK_STR(h, expected);
}

static void test_sha1_vectors(void)
{
    static uint8_t a[1000000];
    /* FIPS 180-2 i hashlib dla długości na granicy bloku */
    check_sha1("", 0, "da39a3ee5e6b4b0d3255bfef95601890afd80709");
    check_sha1("abc", 3, "a9993e364706816aba3e25717850c26c9cd0d89d");
    check_sha1("abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq", 56,
               "84983e441c3bd26ebaae4aa1f95129e5e54670f1");
    memset(a, 'a', sizeof(a));
    check_sha1(a, 55, "c1c8bbdc22796e28c0e15163d20899b65621d65a");
    check_sha1(a, 56, "c2db330f6083854c99d4b5bfb6e8f29f201be699");
    check_sha1(a, 63, "03f09f5b158a7a8cdad920bddc29b81c18a551f5");
    check_sha1(a, 64, "0098ba824b5c16427bd7a1122a5a442a25ec644d");
    check_sha1(a, 65, "11655326c708d70319be2610e8a57d9a5b959d3b");
    check_sha1(a, 119, "ee971065aaa017e0632a8ca6c77bb3bf8b1dfc56");
    check_sha1(a, 120, "f34c1488385346a55709ba056ddd08280dd4c6d6");
    check_sha1(a, sizeof(a), "34aa973cd4c4daa4f61eeb2bdbad27316534016f");
}

static void test_base64_and_rfc_accept(void)
{
    /* RFC 4648, sekcja 10 */
    static const char *const in[] = {"", "f", "fo", "foo", "foob", "fooba", "foobar"};
    static const char *const out[] = {"", "Zg==", "Zm8=", "Zm9v", "Zm9vYg==", "Zm9vYmE=", "Zm9vYmFy"};
    char buf[16];
    char accept[29];
    size_t i;
    for (i = 0; i < 7; i++) {
        CHECK(fw_base64((const uint8_t *)in[i], strlen(in[i]), buf) == strlen(out[i]));
        CHECK_STR(buf, out[i]);
    }
    accept_for("dGhlIHNhbXBsZSBub25jZQ==", accept); /* przykład z RFC 6455, sekcja 1.3 */
    CHECK_STR(accept, "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=");
}

static void test_utf8(void)
{
    static const char *const good[] = {"", "ascii", "zażółć gęślą jaźń", "\xEF\xBF\xBD", "\xF4\x8F\xBF\xBF",
                                       "\xF0\x9F\x98\x80"};
    static const char *const bad[] = {"\xC0\x80", "\xC1\xBF", "\xE0\x80\x80", "\xED\xA0\x80", "\xF0\x80\x80\x80",
                                      "\xF4\x90\x80\x80", "\xF5\x80\x80\x80", "\x80", "\xE2\x82", "a\xFF",
                                      "\xC3\x28"};
    size_t i;
    for (i = 0; i < sizeof(good) / sizeof(good[0]); i++) {
        CHECK(fw_utf8_valid((const uint8_t *)good[i], strlen(good[i])));
    }
    for (i = 0; i < sizeof(bad) / sizeof(bad[0]); i++) {
        CHECK(!fw_utf8_valid((const uint8_t *)bad[i], strlen(bad[i])));
    }
}

/* ---------- handshake ---------- */

static void test_handshake_request(void)
{
    char key[25];
    char expected[256];
    start();
    CHECK(find_key(key) != NULL && strlen(key) == 24 && key[22] == '=' && key[23] == '=');
    snprintf(expected, sizeof(expected),
             "GET /ws HTTP/1.1\r\nHost: 127.0.0.1:8000\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
             "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n",
             key);
    CHECK(t.out_len == strlen(expected) && memcmp(t.out, expected, t.out_len) == 0);
    CHECK(ws.state == FW_WS_HANDSHAKE && t.opened == 0);

    /* klucz to 16 losowych bajtów, inny przy każdym połączeniu */
    t.rnd = 99;
    t.out_len = 0;
    {
        char key2[25];
        CHECK(fw_ws_start(&ws, &io, "h", "/ws", NULL, 0) == 0);
        CHECK(find_key(key2) != NULL && strcmp(key, key2) != 0);
    }

    /* za długi host albo ścieżka bez "/" */
    {
        static char host[FW_WS_TX_MAX];
        memset(host, 'h', sizeof(host) - 1);
        host[sizeof(host) - 1] = '\0';
        CHECK(fw_ws_start(&ws, &io, host, "/ws", NULL, 0) == -1 && ws.state == FW_WS_CLOSED);
        CHECK(fw_ws_start(&ws, &io, "h", "ws", NULL, 0) == -1);
    }
    /* błąd zapisu żądania: bez on_close */
    t.closes = 0;
    t.fail_write = 1;
    CHECK(fw_ws_start(&ws, &io, "h", "/ws", NULL, 0) == -1);
    fw_ws_tick(&ws, 100000);
    CHECK(t.closes == 0 && ws.state == FW_WS_CLOSED);
}

static void test_handshake_byte_by_byte_with_frame_after(void)
{
    char buf[600];
    size_t n;
    size_t i;
    start();
    n = response(buf, sizeof(buf), "Date: Tue, 27 Oct 2026 10:00:00 GMT\r\nServer: uvicorn\r\n");
    n += srv_frame((uint8_t *)buf + n, 0x81, "{\"v\":1}", 7);
    for (i = 0; i < n; i++) {
        feed(buf + i, 1, 1000 + (uint32_t)i);
    }
    CHECK(t.opened == 1 && t.messages == 1 && !t.last_binary);
    CHECK(t.last_len == 7 && memcmp(t.last, "{\"v\":1}", 7) == 0);

    /* odpowiedź i ramka w jednym kawałku */
    start();
    n = response(buf, sizeof(buf), "");
    n += srv_frame((uint8_t *)buf + n, 0x82, "\x02\x00\x01\x00\x00", 5);
    feed(buf, n, 1000);
    CHECK(t.opened == 1 && t.messages == 1 && t.last_binary && t.last_len == 5);
}

static void test_handshake_header_variants_accepted(void)
{
    char key[25];
    char accept[29];
    char buf[600];
    start();
    accept_for(find_key(key), accept);
    snprintf(buf, sizeof(buf),
             "HTTP/1.1 101\r\nupgrade:WebSocket\r\nCONNECTION: keep-alive, Upgrade \r\n"
             "sec-websocket-accept:\t%s\r\n\r\n",
             accept);
    feed(buf, strlen(buf), 1000);
    CHECK(ws.state == FW_WS_OPEN && t.opened == 1);
}

static void reject_case(const char *status, const char *headers, int with_accept, const char *error)
{
    char key[25];
    char accept[29];
    char buf[900];
    start();
    accept_for(find_key(key), accept);
    snprintf(buf, sizeof(buf), "%s\r\n%s%s%s%s\r\n", status, headers, with_accept ? "Sec-WebSocket-Accept: " : "",
             with_accept ? accept : "", with_accept ? "\r\n" : "");
    feed(buf, strlen(buf), 1000);
    CHECK(t.opened == 0 && t.closes == 1 && t.close_code == FW_WS_ABNORMAL);
    CHECK_STR(ws.error, error);
    CHECK(t.out_read == 0 && strstr((const char *)t.out, "\r\n\r\n") + 4 == (const char *)t.out + t.out_len);
}

static void test_handshake_rejections(void)
{
    static const char ok[] = "Upgrade: websocket\r\nConnection: Upgrade\r\n";
    static char big[FW_WS_MSG_MAX + 10];
    char key[25];
    char accept[29];
    char buf[200];

    reject_case("HTTP/1.1 403 Forbidden", ok, 1, "handshake_status");
    reject_case("HTTP/1.1 404 Not Found", ok, 1, "handshake_status");
    reject_case("HTTP/1.1 1010", ok, 1, "handshake_status");
    reject_case("HTTP/1.0 101 Switching Protocols", ok, 1, "handshake_status");
    reject_case("HTTP/1.1 101 Switching Protocols", "Connection: Upgrade\r\n", 1, "handshake_header");
    reject_case("HTTP/1.1 101 Switching Protocols", "Upgrade: websocket\r\n", 1, "handshake_header");
    reject_case("HTTP/1.1 101 Switching Protocols", "Upgrade: websocket\r\nConnection: keep-alive\r\n", 1,
                "handshake_header");
    reject_case("HTTP/1.1 101 Switching Protocols", "Upgrade: h2c\r\nConnection: Upgrade\r\n", 1,
                "handshake_header");
    reject_case("HTTP/1.1 101 Switching Protocols", ok, 0, "handshake_accept");
    reject_case("HTTP/1.1 101 Switching Protocols",
                "Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: AAAAAAAAAAAAAAAAAAAAAAAAAAA=\r\n", 1,
                "handshake_accept");
    reject_case("HTTP/1.1 101 Switching Protocols",
                "Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Extensions: permessage-deflate\r\n", 1,
                "handshake_header");
    reject_case("HTTP/1.1 101 Switching Protocols",
                "Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Protocol: chat\r\n", 1,
                "handshake_header");
    reject_case("HTTP/1.1 101 Switching Protocols", "Upgrade: websocket\r\nConnection: Upgrade\r\nbroken line\r\n",
                1, "handshake_header");

    /* odpowiedź bez końca nagłówków większa niż bufor */
    start();
    memset(big, 'x', sizeof(big));
    feed(big, sizeof(big), 1000);
    CHECK(t.closes == 1 && t.close_code == FW_WS_ABNORMAL);
    CHECK_STR(ws.error, "handshake_size");

    /* brak odpowiedzi w FW_WS_HANDSHAKE_MS */
    start();
    fw_ws_tick(&ws, 1000 + FW_WS_HANDSHAKE_MS - 1);
    CHECK(t.closes == 0);
    fw_ws_tick(&ws, 1000 + FW_WS_HANDSHAKE_MS);
    CHECK(t.closes == 1 && t.close_code == FW_WS_ABNORMAL);
    CHECK_STR(ws.error, "handshake_timeout");

    /* przed handshake nie da się nic wysłać; port może przerwać handshake */
    start();
    CHECK(fw_ws_send(&ws, 0, (const uint8_t *)"x", 1) == -1);
    fw_ws_close(&ws, FW_WS_NORMAL, 1000);
    fw_ws_tick(&ws, 1001);
    CHECK(t.closes == 1 && t.close_code == FW_WS_ABNORMAL && t.opened == 0);

    /* odpowiedź poprawna, ale po zamknięciu nic się nie dzieje */
    accept_for(find_key(key), accept);
    snprintf(buf, sizeof(buf), "HTTP/1.1 101 x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                               "Sec-WebSocket-Accept: %s\r\n\r\n", accept);
    feed(buf, strlen(buf), 1002);
    CHECK(t.opened == 0 && t.closes == 1);
}

static int open_with_protocol(const char *offered, const char *chosen)
{
    char key[25];
    char accept[29];
    char buf[600];
    memset(&t, 0, sizeof(t));
    t.rnd = 5;
    CHECK(fw_ws_start(&ws, &io, "h", "/ws", offered, 0) == 0);
    accept_for(find_key(key), accept);
    snprintf(buf, sizeof(buf),
             "HTTP/1.1 101 x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: %s\r\n"
             "%s%s%s\r\n",
             accept, chosen ? "Sec-WebSocket-Protocol: " : "", chosen ? chosen : "", chosen ? "\r\n" : "");
    feed(buf, strlen(buf), 0);
    return ws.state == FW_WS_OPEN;
}

static void test_subprotocols(void)
{
    static const char offered[] = "iris, token.abcdefghijklmnop";
    CHECK(open_with_protocol(offered, "iris"));
    CHECK(strstr((const char *)t.out, "\r\nSec-WebSocket-Protocol: iris, token.abcdefghijklmnop\r\n\r\n"));
    CHECK(open_with_protocol(offered, NULL)); /* serwer może nie wybrać żadnego */
    CHECK(!open_with_protocol(offered, "chat") && t.closes == 1);
    CHECK_STR(ws.error, "handshake_header");
    CHECK(!open_with_protocol(offered, "token.abcdefghijklmno")); /* nie cały element listy */
    CHECK(!open_with_protocol(offered, "iris, chat"));
    CHECK(!open_with_protocol(NULL, "iris")); /* nie oferowaliśmy */
    CHECK(open_with_protocol(NULL, NULL));
    CHECK(strstr((const char *)t.out, "Sec-WebSocket-Protocol") == NULL);
}

/* ---------- wysyłka ---------- */

static void test_send_masks_frames(void)
{
    static uint8_t data[FW_WS_MSG_MAX];
    static uint8_t got[FW_WS_MSG_MAX];
    static const size_t sizes[] = {0, 1, 125, 126, 200, FW_WS_MSG_MAX};
    uint8_t first_mask[4] = {0};
    size_t n;
    size_t i;
    size_t k;
    open_ws();
    for (i = 0; i < sizeof(data); i++) {
        data[i] = (uint8_t)(i * 7u + 3u);
    }
    for (k = 0; k < sizeof(sizes) / sizeof(sizes[0]); k++) {
        size_t at = t.out_len;
        int binary = (int)(k & 1u);
        CHECK(fw_ws_send(&ws, binary, data, sizes[k]) == 0);
        CHECK(client_frame(got, &n) == (binary ? 0x2 : 0x1) && n == sizes[k] && memcmp(got, data, n) == 0);
        if (k == 0) {
            memcpy(first_mask, t.out + at + 2, 4);
        } else if (k == 1) {
            CHECK(memcmp(first_mask, t.out + at + 2, 4) != 0); /* nowa maska dla każdej ramki */
        }
    }
    CHECK(no_more_client_frames());

    /* za duża wiadomość: błąd programu, połączenie kończy się kodem 1011 */
    CHECK(fw_ws_send(&ws, 1, data, FW_WS_MSG_MAX + 1) == -1);
    CHECK(client_close_code() == FW_WS_INTERNAL_ERROR && t.closes == 0);
    fw_ws_tick(&ws, 1001);
    CHECK(t.closes == 1 && t.close_code == FW_WS_INTERNAL_ERROR);
    CHECK(fw_ws_send(&ws, 0, data, 1) == -1);
}

static void test_write_failure_closes_outside_callbacks(void)
{
    uint8_t got[8];
    size_t n;
    /* błąd zapisu w fw_ws_send wołanym z on_message: on_close po powrocie z callbacku */
    open_ws();
    t.reply_on_message = 1;
    feed_frame(0x81, "a", 1);
    CHECK(t.messages == 1 && client_frame(got, &n) == 0x1 && n == 3 && memcmp(got, "ack", 3) == 0);
    t.fail_write = 1;
    feed_frame(0x81, "b", 1);
    CHECK(t.messages == 2 && t.closes == 1 && !t.close_in_callback && t.close_code == FW_WS_ABNORMAL);
    CHECK_STR(ws.error, "transport");

    /* błąd zapisu poza callbackiem: on_close przy najbliższym fw_ws_tick */
    open_ws();
    t.fail_write = 1;
    CHECK(fw_ws_send(&ws, 0, (const uint8_t *)"x", 1) == -1);
    CHECK(t.closes == 0 && ws.state == FW_WS_CLOSED);
    fw_ws_tick(&ws, 1001);
    fw_ws_tick(&ws, 1002);
    fw_ws_transport_lost(&ws);
    CHECK(t.closes == 1);
}

/* ---------- odbiór ---------- */

static void test_receive_lengths(void)
{
    static uint8_t data[FW_WS_MSG_MAX];
    static uint8_t buf[FW_WS_MSG_MAX + 20];
    static const size_t sizes[] = {0, 125, 126, 1000, FW_WS_MSG_MAX};
    size_t k;
    size_t i;
    open_ws();
    for (i = 0; i < sizeof(data); i++) {
        data[i] = (uint8_t)i;
    }
    for (k = 0; k < sizeof(sizes) / sizeof(sizes[0]); k++) {
        feed_frame(0x82, data, sizes[k]);
        CHECK(t.messages == (int)k + 1 && t.last_binary && t.last_len == sizes[k]);
        CHECK(memcmp(t.last, data, sizes[k]) == 0);
    }
    /* długość zapisana na 2 i 8 bajtach, choć wystarczyłby 1 */
    feed(buf, srv_frame_ext(buf, 0x81, "abc", 3, 2), 1000);
    CHECK(t.messages == 6 && t.last_len == 3);
    feed(buf, srv_frame_ext(buf, 0x81, "abcd", 4, 8), 1000);
    CHECK(t.messages == 7 && t.last_len == 4 && ws.state == FW_WS_OPEN);
    CHECK(no_more_client_frames());
}

static void test_fragments_with_interleaved_control_frames(void)
{
    uint8_t buf[256];
    uint8_t got[200];
    size_t n = 0;
    size_t i;
    open_ws();
    /* "Zażółć" podzielone w środku znaku "ó" */
    n += srv_frame(buf + n, 0x01, "Za\xC5\xBC\xC3", 5);
    n += srv_frame(buf + n, 0x89, "p1", 2);
    n += srv_frame(buf + n, 0x00, "\xB3\xC5\x82", 3);
    n += srv_frame(buf + n, 0x8A, "", 0);
    n += srv_frame(buf + n, 0x80, "\xC4\x87", 2);
    for (i = 0; i < n; i++) {
        feed(buf + i, 1, 1000);
    }
    CHECK(t.messages == 1 && !t.last_binary && t.last_len == 10);
    CHECK(memcmp(t.last, "Za\xC5\xBC\xC3\xB3\xC5\x82\xC4\x87", 10) == 0);
    CHECK(client_frame(got, &n) == 0xA && n == 2 && memcmp(got, "p1", 2) == 0); /* pong z tą samą treścią */
    CHECK(no_more_client_frames());

    /* binarna w trzech częściach, w tym pusta */
    n = 0;
    n += srv_frame(buf + n, 0x02, "\x02\x00", 2);
    n += srv_frame(buf + n, 0x00, "", 0);
    n += srv_frame(buf + n, 0x80, "\x01\x00\x07", 3);
    feed(buf, n, 1000);
    CHECK(t.messages == 2 && t.last_binary && t.last_len == 5 && memcmp(t.last, "\x02\x00\x01\x00\x07", 5) == 0);
    CHECK(ws.state == FW_WS_OPEN);
}

static void test_oversized_message_is_dropped_and_connection_stays(void)
{
    static uint8_t data[FW_WS_MSG_MAX + 1];
    static uint8_t buf[2 * FW_WS_MSG_MAX];
    size_t n;
    open_ws();
    memset(data, 'x', sizeof(data));
    feed_frame(0x82, data, FW_WS_MSG_MAX + 1);
    CHECK(t.messages == 0 && t.oversized == 1 && ws.oversized == 1 && ws.state == FW_WS_OPEN);

    /* fragmenty, które razem przekraczają bufor; tekst niepoprawny w UTF-8 nie jest sprawdzany */
    n = srv_frame(buf, 0x01, data, 1000);
    n += srv_frame(buf + n, 0x89, "", 0);
    n += srv_frame(buf + n, 0x00, data, 281);
    n += srv_frame(buf + n, 0x80, "\xFF", 1);
    feed(buf, n, 1000);
    CHECK(t.messages == 0 && t.oversized == 2 && ws.state == FW_WS_OPEN);
    CHECK(client_frame(buf, &n) == 0xA); /* ping w środku i tak dostał odpowiedź */

    /* następna wiadomość przechodzi */
    feed_frame(0x81, "ok", 2);
    CHECK(t.messages == 1 && t.last_len == 2 && t.closes == 0);

    /* dokładnie FW_WS_MSG_MAX w dwóch fragmentach mieści się */
    n = srv_frame(buf, 0x02, data, 1000);
    n += srv_frame(buf + n, 0x80, data, FW_WS_MSG_MAX - 1000);
    feed(buf, n, 1000);
    CHECK(t.messages == 2 && t.last_len == FW_WS_MSG_MAX && t.oversized == 2);
}

static void protocol_error_case(const uint8_t *frame, size_t len, uint16_t code, const char *error)
{
    open_ws();
    feed(frame, len, 1000);
    CHECK(t.closes == 1 && t.close_code == code && ws.state == FW_WS_CLOSED);
    CHECK_STR(ws.error, error);
    CHECK(client_close_code() == code && no_more_client_frames());
    CHECK(t.messages == 0);
    /* po zamknięciu bajty są pomijane */
    feed("\x81\x01x", 3, 1001);
    fw_ws_tick(&ws, 100000);
    CHECK(t.closes == 1 && t.messages == 0);
}

static void test_protocol_errors(void)
{
    uint8_t buf[300];
    size_t n;
    protocol_error_case((const uint8_t *)"\xC1\x01x", 3, FW_WS_PROTOCOL_ERROR, "protocol");     /* RSV1 */
    protocol_error_case((const uint8_t *)"\x81\x81\x00\x00\x00\x00x", 7, FW_WS_PROTOCOL_ERROR, "protocol"); /* maska */
    protocol_error_case((const uint8_t *)"\x83\x00", 2, FW_WS_PROTOCOL_ERROR, "protocol");     /* opcode 3 */
    protocol_error_case((const uint8_t *)"\x8B\x00", 2, FW_WS_PROTOCOL_ERROR, "protocol");     /* opcode 0xB */
    protocol_error_case((const uint8_t *)"\x09\x00", 2, FW_WS_PROTOCOL_ERROR, "protocol");     /* ping bez FIN */
    protocol_error_case((const uint8_t *)"\x89\x7E\x00\x7E", 4, FW_WS_PROTOCOL_ERROR, "protocol"); /* ping 126 B */
    protocol_error_case((const uint8_t *)"\x80\x01x", 3, FW_WS_PROTOCOL_ERROR, "protocol");    /* ciąg bez początku */
    protocol_error_case((const uint8_t *)"\x01\x01x\x81\x01y", 6, FW_WS_PROTOCOL_ERROR, "protocol"); /* nowa w środku */
    protocol_error_case((const uint8_t *)"\x81\x7F\x80\x00\x00\x00\x00\x00\x00\x01", 10, FW_WS_PROTOCOL_ERROR,
                        "protocol"); /* długość z najstarszym bitem */
    protocol_error_case((const uint8_t *)"\x81\x02\xC3\x28", 4, FW_WS_INVALID_DATA, "utf8");
    protocol_error_case((const uint8_t *)"\x88\x01\x03", 3, FW_WS_PROTOCOL_ERROR, "protocol"); /* close 1 B */
    protocol_error_case((const uint8_t *)"\x88\x02\x03\xED", 4, FW_WS_PROTOCOL_ERROR, "protocol"); /* kod 1005 */
    protocol_error_case((const uint8_t *)"\x88\x02\x03\xE7", 4, FW_WS_PROTOCOL_ERROR, "protocol"); /* kod 999 */
    protocol_error_case((const uint8_t *)"\x88\x02\x13\x88", 4, FW_WS_PROTOCOL_ERROR, "protocol"); /* kod 5000 */
    protocol_error_case((const uint8_t *)"\x88\x03\x03\xE8\xFF", 5, FW_WS_INVALID_DATA, "utf8"); /* powód */

    /* błąd w drugiej z dwóch ramek w jednym kawałku: pierwsza dochodzi */
    open_ws();
    n = srv_frame(buf, 0x81, "ok", 2);
    n += srv_frame(buf + n, 0xC1, "x", 1);
    feed(buf, n, 1000);
    CHECK(t.messages == 1 && t.closes == 1 && t.close_code == FW_WS_PROTOCOL_ERROR);
}

static void test_server_close_is_echoed(void)
{
    uint8_t got[16];
    size_t n;
    open_ws();
    feed_frame(0x88, "\x0F\xA0replaced", 10); /* 4000: host zastąpił połączenie */
    CHECK(t.closes == 1 && t.close_code == 4000 && ws.error == NULL);
    CHECK(client_frame(got, &n) == 0x8 && n == 2 && got[0] == 0x0F && got[1] == 0xA0);
    CHECK(fw_ws_send(&ws, 0, (const uint8_t *)"x", 1) == -1 && no_more_client_frames());

    open_ws();
    feed_frame(0x88, "", 0);
    CHECK(t.closes == 1 && t.close_code == FW_WS_NO_STATUS);
    CHECK(client_frame(got, &n) == 0x8 && n == 0);

    /* transport zerwany po on_close: bez drugiego wywołania */
    fw_ws_transport_lost(&ws);
    CHECK(t.closes == 1);
}

static void test_client_close(void)
{
    open_ws();
    fw_ws_close(&ws, FW_WS_NORMAL, 2000);
    CHECK(ws.state == FW_WS_CLOSING && client_close_code() == FW_WS_NORMAL);
    feed_frame(0x81, "late", 4); /* dane po naszym close są pomijane */
    feed_frame(0x89, "", 0);     /* ping bez odpowiedzi */
    feed_frame(0x81, "\xFF", 1); /* bez drugiej ramki close */
    CHECK(t.messages == 0 && no_more_client_frames() && t.closes == 0);
    CHECK(fw_ws_send(&ws, 0, (const uint8_t *)"x", 1) == -1);
    feed_frame(0x88, "\x03\xE8", 2);
    CHECK(t.closes == 1 && t.close_code == FW_WS_NORMAL && no_more_client_frames());

    /* błąd protokołu po naszym close: koniec bez drugiej ramki close */
    open_ws();
    fw_ws_close(&ws, FW_WS_NORMAL, 2000);
    CHECK(client_close_code() == FW_WS_NORMAL);
    feed("\xC1\x01x", 3, 2001);
    CHECK(t.closes == 1 && t.close_code == FW_WS_PROTOCOL_ERROR && no_more_client_frames());

    /* serwer nie odpowiada na close */
    open_ws();
    fw_ws_close(&ws, FW_WS_NORMAL, 2000);
    fw_ws_tick(&ws, 2000 + FW_WS_CLOSE_MS - 1);
    CHECK(t.closes == 0);
    fw_ws_tick(&ws, 2000 + FW_WS_CLOSE_MS);
    CHECK(t.closes == 1 && t.close_code == FW_WS_ABNORMAL);
    CHECK_STR(ws.error, "close_timeout");

    /* serwer zamyka TCP zamiast odpowiedzieć */
    open_ws();
    fw_ws_close(&ws, FW_WS_NORMAL, 2000);
    fw_ws_transport_lost(&ws);
    CHECK(t.closes == 1 && t.close_code == FW_WS_ABNORMAL);
    CHECK_STR(ws.error, "close_transport");
}

static void test_keepalive(void)
{
    uint8_t got[8];
    size_t n;
    uint8_t frame[2] = {0x8A, 0x00}; /* pong */
    open_ws(); /* ostatni odbiór: 1000 */
    fw_ws_tick(&ws, 1000 + FW_WS_PING_MS - 1);
    CHECK(no_more_client_frames());
    fw_ws_tick(&ws, 1000 + FW_WS_PING_MS);
    CHECK(client_frame(got, &n) == 0x9 && n == 0);
    fw_ws_tick(&ws, 1000 + FW_WS_PING_MS + 1000);
    CHECK(no_more_client_frames()); /* jeden ping na okres ciszy */
    fw_ws_feed(&ws, frame, 2, 8000);
    fw_ws_tick(&ws, 8000 + FW_WS_PING_MS);
    CHECK(client_frame(got, &n) == 0x9);
    fw_ws_tick(&ws, 8000 + FW_WS_DEAD_MS - 1);
    CHECK(t.closes == 0 && ws.state == FW_WS_OPEN);
    fw_ws_tick(&ws, 8000 + FW_WS_DEAD_MS);
    CHECK(t.closes == 1 && t.close_code == FW_WS_ABNORMAL);
    CHECK_STR(ws.error, "timeout");

    /* zegar przekręca się przez 0 */
    memset(&t, 0, sizeof(t));
    {
        char resp[512];
        CHECK(fw_ws_start(&ws, &io, "h", "/ws", NULL, 0xFFFFF000u) == 0);
        feed(resp, response(resp, sizeof(resp), ""), 0xFFFFF000u);
        t.out_len = t.out_read = 0;
        fw_ws_tick(&ws, 0xFFFFF000u + FW_WS_PING_MS);
        CHECK(client_frame(got, &n) == 0x9 && t.closes == 0);
    }
}

/* ---------- losowe dane ---------- */

static uint32_t lcg = 7;

static uint32_t next(void)
{
    lcg = lcg * 1103515245u + 12345u;
    return lcg >> 8;
}

static void test_random_input_never_breaks_invariants(void)
{
    static uint8_t buf[4096];
    static const uint8_t heads[] = {0x81, 0x82, 0x01, 0x00, 0x80, 0x88, 0x89, 0x8A, 0xC1, 0x83};
    int iter;
    for (iter = 0; iter < 3000; iter++) {
        size_t n = 0;
        size_t pos = 0;
        open_ws();
        /* ramki z sensownym początkiem i losowym resztą, czasem czyste śmieci */
        while (n < sizeof(buf) - 300) {
            size_t len = next() % 200u;
            size_t i;
            if (next() % 8u == 0) {
                for (i = 0; i < len; i++) {
                    buf[n++] = (uint8_t)next();
                }
                continue;
            }
            buf[n++] = heads[next() % sizeof(heads)];
            buf[n++] = (uint8_t)(next() % 4u == 0 ? 126u : len % 126u);
            if (buf[n - 1] == 126) {
                buf[n++] = (uint8_t)(next() % 6u);
                buf[n++] = (uint8_t)next();
            }
            for (i = 0; i < len; i++) {
                buf[n++] = (uint8_t)(next() % 3u ? 'a' + next() % 26u : next());
            }
        }
        while (pos < n) {
            size_t chunk = 1 + next() % 300u;
            if (chunk > n - pos) {
                chunk = n - pos;
            }
            fw_ws_feed(&ws, buf + pos, chunk, 1000 + (uint32_t)pos);
            pos += chunk;
            if (next() % 50u == 0) {
                fw_ws_tick(&ws, 1000 + (uint32_t)pos + next() % 20000u);
            }
        }
        fw_ws_transport_lost(&ws);
        CHECK(t.closes == 1 && ws.state == FW_WS_CLOSED && ws.msg_len <= FW_WS_MSG_MAX);
        CHECK(t.out_len <= sizeof(t.out));
    }
}

static void test_ram(void)
{
    printf("sizeof(fw_ws_t) = %zu B\n", sizeof(fw_ws_t));
    CHECK(sizeof(fw_ws_t) <= 2912u); /* zmiana wymaga poprawki budżetu w fw_config.h */
}

int main(void)
{
    test_sha1_vectors();
    test_base64_and_rfc_accept();
    test_utf8();
    test_handshake_request();
    test_handshake_byte_by_byte_with_frame_after();
    test_handshake_header_variants_accepted();
    test_handshake_rejections();
    test_subprotocols();
    test_send_masks_frames();
    test_write_failure_closes_outside_callbacks();
    test_receive_lengths();
    test_fragments_with_interleaved_control_frames();
    test_oversized_message_is_dropped_and_connection_stays();
    test_protocol_errors();
    test_server_close_is_echoed();
    test_client_close();
    test_keepalive();
    test_random_input_never_breaks_invariants();
    test_ram();
    printf("%d sprawdzeń, %d błędów\n", checks, failures);
    return failures ? 1 : 0;
}
