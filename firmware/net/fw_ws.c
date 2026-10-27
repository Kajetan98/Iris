#include "fw_ws.h"

#include <string.h>

#define OP_CONT 0x0u
#define OP_TEXT 0x1u
#define OP_BINARY 0x2u
#define OP_CLOSE 0x8u
#define OP_PING 0x9u
#define OP_PONG 0xAu

static const char WS_GUID[] = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";

/* ---------- SHA-1, base64, UTF-8 ---------- */

static uint32_t rol(uint32_t x, unsigned n)
{
    return (x << n) | (x >> (32u - n));
}

static void sha1_block(uint32_t h[5], const uint8_t *p)
{
    uint32_t w[80];
    uint32_t a = h[0], b = h[1], c = h[2], d = h[3], e = h[4];
    unsigned i;
    for (i = 0; i < 16; i++) {
        w[i] = (uint32_t)p[4 * i] << 24 | (uint32_t)p[4 * i + 1] << 16 | (uint32_t)p[4 * i + 2] << 8 |
               (uint32_t)p[4 * i + 3];
    }
    for (i = 16; i < 80; i++) {
        w[i] = rol(w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16], 1);
    }
    for (i = 0; i < 80; i++) {
        uint32_t f;
        uint32_t k;
        uint32_t t;
        if (i < 20) {
            f = (b & c) | (~b & d);
            k = 0x5A827999u;
        } else if (i < 40) {
            f = b ^ c ^ d;
            k = 0x6ED9EBA1u;
        } else if (i < 60) {
            f = (b & c) | (b & d) | (c & d);
            k = 0x8F1BBCDCu;
        } else {
            f = b ^ c ^ d;
            k = 0xCA62C1D6u;
        }
        t = rol(a, 5) + f + e + k + w[i];
        e = d;
        d = c;
        c = rol(b, 30);
        b = a;
        a = t;
    }
    h[0] += a;
    h[1] += b;
    h[2] += c;
    h[3] += d;
    h[4] += e;
}

void fw_sha1(const uint8_t *data, size_t len, uint8_t out[20])
{
    uint32_t h[5] = {0x67452301u, 0xEFCDAB89u, 0x98BADCFEu, 0x10325476u, 0xC3D2E1F0u};
    uint8_t block[64];
    uint64_t bits = (uint64_t)len * 8u;
    size_t i = 0;
    size_t rem;
    unsigned k;
    for (; len - i >= 64; i += 64) {
        sha1_block(h, data + i);
    }
    rem = len - i;
    if (rem) {
        memcpy(block, data + i, rem);
    }
    block[rem++] = 0x80;
    if (rem > 56) {
        memset(block + rem, 0, 64 - rem);
        sha1_block(h, block);
        rem = 0;
    }
    memset(block + rem, 0, 56 - rem);
    for (k = 0; k < 8; k++) {
        block[56 + k] = (uint8_t)(bits >> (56 - 8 * k));
    }
    sha1_block(h, block);
    for (k = 0; k < 20; k++) {
        out[k] = (uint8_t)(h[k / 4] >> (24 - 8 * (k % 4)));
    }
}

size_t fw_base64(const uint8_t *data, size_t len, char *out)
{
    static const char abc[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    size_t i;
    size_t o = 0;
    for (i = 0; i < len; i += 3) {
        uint32_t v = (uint32_t)data[i] << 16;
        size_t n = len - i;
        if (n > 1) {
            v |= (uint32_t)data[i + 1] << 8;
        }
        if (n > 2) {
            v |= data[i + 2];
        }
        out[o++] = abc[(v >> 18) & 63u];
        out[o++] = abc[(v >> 12) & 63u];
        out[o++] = n > 1 ? abc[(v >> 6) & 63u] : '=';
        out[o++] = n > 2 ? abc[v & 63u] : '=';
    }
    out[o] = '\0';
    return o;
}

int fw_utf8_valid(const uint8_t *s, size_t len)
{
    size_t i = 0;
    while (i < len) {
        uint8_t c = s[i];
        size_t need;
        size_t k;
        uint32_t cp;
        uint32_t min;
        if (c < 0x80u) {
            i++;
            continue;
        }
        if (c >= 0xC2u && c <= 0xDFu) {
            need = 1;
            cp = c & 0x1Fu;
            min = 0x80u;
        } else if ((c & 0xF0u) == 0xE0u) {
            need = 2;
            cp = c & 0x0Fu;
            min = 0x800u;
        } else if (c >= 0xF0u && c <= 0xF4u) {
            need = 3;
            cp = c & 0x07u;
            min = 0x10000u;
        } else {
            return 0;
        }
        if (len - i - 1 < need) {
            return 0;
        }
        for (k = 1; k <= need; k++) {
            if ((s[i + k] & 0xC0u) != 0x80u) {
                return 0;
            }
            cp = (cp << 6) | (s[i + k] & 0x3Fu);
        }
        if (cp < min || cp > 0x10FFFFu || (cp >= 0xD800u && cp <= 0xDFFFu)) {
            return 0; /* forma nadmiarowa, poza zakresem albo surogat */
        }
        i += need + 1;
    }
    return 1;
}

/* ---------- zamykanie ---------- */

static void deliver_close(fw_ws_t *ws)
{
    if (ws->close_pending) {
        ws->close_pending = 0;
        if (ws->io.on_close) {
            ws->io.on_close(ws->io.ctx, ws->close_code);
        }
    }
}

/* Koniec połączenia; on_close przychodzi przy najbliższym deliver_close(). */
static void finish(fw_ws_t *ws, uint16_t code, const char *error)
{
    if (ws->state == FW_WS_CLOSED) {
        return;
    }
    ws->state = FW_WS_CLOSED;
    ws->close_code = code;
    ws->error = error;
    ws->close_pending = 1;
}

/* ---------- wysyłka ramek ---------- */

static int send_frame(fw_ws_t *ws, uint8_t opcode, const uint8_t *data, size_t len)
{
    uint32_t mask = ws->io.random32(ws->io.ctx);
    uint8_t m[4];
    size_t n = 0;
    size_t i;
    if (len > FW_WS_MSG_MAX) {
        return -1;
    }
    m[0] = (uint8_t)(mask >> 24);
    m[1] = (uint8_t)(mask >> 16);
    m[2] = (uint8_t)(mask >> 8);
    m[3] = (uint8_t)mask;
    ws->tx[n++] = (uint8_t)(0x80u | opcode);
    if (len < 126) {
        ws->tx[n++] = (uint8_t)(0x80u | len);
    } else {
        ws->tx[n++] = 0x80u | 126u;
        ws->tx[n++] = (uint8_t)(len >> 8);
        ws->tx[n++] = (uint8_t)len;
    }
    memcpy(ws->tx + n, m, 4);
    n += 4;
    for (i = 0; i < len; i++) {
        ws->tx[n + i] = (uint8_t)(data[i] ^ m[i & 3u]);
    }
    return ws->io.write(ws->io.ctx, ws->tx, n + len);
}

static int send_close(fw_ws_t *ws, uint16_t code)
{
    uint8_t p[2];
    p[0] = (uint8_t)(code >> 8);
    p[1] = (uint8_t)code;
    return send_frame(ws, OP_CLOSE, p, 2);
}

/* Zerwanie połączenia przez nas z powodu błędu (RFC 6455, sekcja 7.1.7). Po wysłanej
 * wcześniej ramce close nie wysyłamy drugiej. */
static void fail(fw_ws_t *ws, uint16_t code, const char *error)
{
    if (ws->state == FW_WS_OPEN) {
        (void)send_close(ws, code); /* przy błędzie zapisu i tak kończymy */
    }
    finish(ws, code, error);
}

int fw_ws_send(fw_ws_t *ws, int binary, const uint8_t *data, size_t len)
{
    if (ws->state != FW_WS_OPEN) {
        return -1;
    }
    if (len > FW_WS_MSG_MAX) {
        fail(ws, FW_WS_INTERNAL_ERROR, "tx_size"); /* rdzeń nie tworzy takich ramek */
        return -1;
    }
    if (send_frame(ws, binary ? OP_BINARY : OP_TEXT, data, len) != 0) {
        finish(ws, FW_WS_ABNORMAL, "transport");
        return -1;
    }
    return 0;
}

void fw_ws_close(fw_ws_t *ws, uint16_t code, uint32_t now_ms)
{
    if (ws->state == FW_WS_OPEN) {
        if (send_close(ws, code) != 0) {
            finish(ws, FW_WS_ABNORMAL, "transport");
            return;
        }
        ws->state = FW_WS_CLOSING;
        ws->state_ms = now_ms;
    } else if (ws->state == FW_WS_HANDSHAKE) {
        finish(ws, FW_WS_ABNORMAL, "local");
    }
}

void fw_ws_transport_lost(fw_ws_t *ws)
{
    finish(ws, FW_WS_ABNORMAL, ws->state == FW_WS_CLOSING ? "close_transport" : "transport");
    deliver_close(ws);
}

/* ---------- handshake ---------- */

static int append(fw_ws_t *ws, size_t *pos, const char *s)
{
    size_t n = strlen(s);
    if (*pos + n > sizeof(ws->tx)) {
        return -1;
    }
    memcpy(ws->tx + *pos, s, n);
    *pos += n;
    return 0;
}

int fw_ws_start(fw_ws_t *ws, const fw_ws_io_t *io, const char *host, const char *path, uint32_t now_ms)
{
    uint8_t nonce[16];
    char key[25];
    uint8_t digest[20];
    uint8_t joined[24 + sizeof(WS_GUID) - 1];
    size_t pos = 0;
    unsigned i;

    memset(ws, 0, sizeof(*ws));
    ws->io = *io;
    ws->hdr_need = 2;
    for (i = 0; i < 4; i++) {
        uint32_t r = io->random32(io->ctx);
        nonce[4 * i] = (uint8_t)(r >> 24);
        nonce[4 * i + 1] = (uint8_t)(r >> 16);
        nonce[4 * i + 2] = (uint8_t)(r >> 8);
        nonce[4 * i + 3] = (uint8_t)r;
    }
    (void)fw_base64(nonce, sizeof(nonce), key);
    memcpy(joined, key, 24);
    memcpy(joined + 24, WS_GUID, sizeof(WS_GUID) - 1);
    fw_sha1(joined, sizeof(joined), digest);
    (void)fw_base64(digest, sizeof(digest), ws->accept);

    if (path[0] != '/' || append(ws, &pos, "GET ") || append(ws, &pos, path) ||
        append(ws, &pos, " HTTP/1.1\r\nHost: ") || append(ws, &pos, host) ||
        append(ws, &pos, "\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: ") ||
        append(ws, &pos, key) || append(ws, &pos, "\r\nSec-WebSocket-Version: 13\r\n\r\n")) {
        ws->error = "request_size";
        return -1;
    }
    if (io->write(io->ctx, ws->tx, pos) != 0) {
        ws->error = "transport";
        return -1;
    }
    ws->state = FW_WS_HANDSHAKE;
    ws->state_ms = now_ms;
    ws->last_rx_ms = now_ms;
    return 0;
}

static int lower(int c)
{
    return (c >= 'A' && c <= 'Z') ? c - 'A' + 'a' : c;
}

static int ieq(const char *s, size_t n, const char *lit)
{
    size_t i;
    for (i = 0; i < n; i++) {
        if (lit[i] == '\0' || lower((unsigned char)s[i]) != lit[i]) {
            return 0;
        }
    }
    return lit[n] == '\0';
}

static int is_ows(char c)
{
    return c == ' ' || c == '\t';
}

/* Lista tokenów rozdzielonych przecinkami, np. "keep-alive, Upgrade". */
static int has_token(const char *v, size_t n, const char *token)
{
    size_t i = 0;
    while (i < n) {
        size_t start;
        size_t end;
        while (i < n && (is_ows(v[i]) || v[i] == ',')) {
            i++;
        }
        start = i;
        while (i < n && v[i] != ',') {
            i++;
        }
        end = i;
        while (end > start && is_ows(v[end - 1])) {
            end--;
        }
        if (end > start && ieq(v + start, end - start, token)) {
            return 1;
        }
    }
    return 0;
}

/* Odpowiedź serwera bez końcowego CRLF CRLF; NULL = poprawna (RFC 6455, sekcja 4.1). */
static const char *check_response(const fw_ws_t *ws, size_t len)
{
    const char *s = (const char *)ws->rx;
    const char *line_end = memchr(s, '\r', len);
    size_t pos;
    int upgrade = 0, connection = 0, accept_ok = 0, accept_bad = 0;
    size_t status_len = line_end ? (size_t)(line_end - s) : len;
    if (status_len < 12 || memcmp(s, "HTTP/1.1 101", 12) != 0 || (status_len > 12 && s[12] != ' ')) {
        return "handshake_status";
    }
    pos = status_len;
    while (pos < len) {
        const char *line;
        const char *colon;
        const char *value;
        size_t line_len;
        size_t name_len;
        size_t value_len;
        if (len - pos < 2 || s[pos] != '\r' || s[pos + 1] != '\n') {
            return "handshake_header";
        }
        pos += 2;
        line = s + pos;
        line_end = memchr(line, '\r', len - pos);
        line_len = line_end ? (size_t)(line_end - line) : len - pos;
        pos += line_len;
        colon = memchr(line, ':', line_len);
        if (!colon || colon == line) {
            return "handshake_header";
        }
        name_len = (size_t)(colon - line);
        value = colon + 1;
        value_len = line_len - name_len - 1;
        while (value_len && is_ows(*value)) {
            value++;
            value_len--;
        }
        while (value_len && is_ows(value[value_len - 1])) {
            value_len--;
        }
        if (ieq(line, name_len, "upgrade")) {
            upgrade |= has_token(value, value_len, "websocket");
        } else if (ieq(line, name_len, "connection")) {
            connection |= has_token(value, value_len, "upgrade");
        } else if (ieq(line, name_len, "sec-websocket-accept")) {
            if (value_len == 28 && memcmp(value, ws->accept, 28) == 0) {
                accept_ok = 1;
            } else {
                accept_bad = 1;
            }
        } else if (ieq(line, name_len, "sec-websocket-extensions") ||
                   ieq(line, name_len, "sec-websocket-protocol")) {
            return "handshake_header"; /* nie prosiliśmy o żadne */
        }
    }
    if (!upgrade || !connection) {
        return "handshake_header";
    }
    return (accept_ok && !accept_bad) ? NULL : "handshake_accept";
}

static void feed_frames(fw_ws_t *ws, const uint8_t *data, size_t len);

static void feed_handshake(fw_ws_t *ws, const uint8_t *data, size_t len, uint32_t now_ms)
{
    size_t i;
    for (i = 0; i < len; i++) {
        const char *err;
        if (ws->hs_len == sizeof(ws->rx)) {
            finish(ws, FW_WS_ABNORMAL, "handshake_size");
            return;
        }
        ws->rx[ws->hs_len++] = data[i];
        if (ws->hs_len < 4 || memcmp(ws->rx + ws->hs_len - 4, "\r\n\r\n", 4) != 0) {
            continue;
        }
        err = check_response(ws, ws->hs_len - 4);
        if (err) {
            finish(ws, FW_WS_ABNORMAL, err);
            return;
        }
        ws->state = FW_WS_OPEN;
        ws->last_rx_ms = now_ms;
        if (ws->io.on_open) {
            ws->io.on_open(ws->io.ctx);
        }
        feed_frames(ws, data + i + 1, len - i - 1); /* ramki tuż za odpowiedzią */
        return;
    }
}

/* ---------- odbiór ramek ---------- */

static int valid_close_code(uint16_t c)
{
    return (c >= 1000 && c <= 1003) || (c >= 1007 && c <= 1014) || (c >= 3000 && c <= 4999);
}

static void on_close_frame(fw_ws_t *ws, size_t len)
{
    uint16_t code = FW_WS_NO_STATUS;
    if (len == 1) {
        fail(ws, FW_WS_PROTOCOL_ERROR, "protocol");
        return;
    }
    if (len >= 2) {
        code = (uint16_t)(ws->ctrl[0] << 8 | ws->ctrl[1]);
        if (!valid_close_code(code)) {
            fail(ws, FW_WS_PROTOCOL_ERROR, "protocol");
            return;
        }
        if (!fw_utf8_valid(ws->ctrl + 2, len - 2)) {
            fail(ws, FW_WS_INVALID_DATA, "utf8");
            return;
        }
    }
    if (ws->state == FW_WS_OPEN) {
        /* odpowiedź tym samym kodem (RFC 6455, sekcja 5.5.1) */
        (void)send_frame(ws, OP_CLOSE, ws->ctrl, len >= 2 ? 2 : 0);
    }
    finish(ws, code, NULL);
}

static void end_frame(fw_ws_t *ws)
{
    size_t len = (size_t)ws->payload_len;
    uint8_t opcode = ws->opcode;
    ws->hdr_len = 0;
    ws->hdr_need = 2;
    ws->in_payload = 0;

    if (opcode == OP_PING) {
        if (ws->state == FW_WS_OPEN && send_frame(ws, OP_PONG, ws->ctrl, len) != 0) {
            finish(ws, FW_WS_ABNORMAL, "transport");
        }
        return;
    }
    if (opcode == OP_PONG) {
        return;
    }
    if (opcode == OP_CLOSE) {
        on_close_frame(ws, len);
        return;
    }
    if (!ws->fin) {
        return;
    }
    ws->msg_active = 0;
    if (ws->state != FW_WS_OPEN) {
        return; /* po naszej ramce close dane są pomijane */
    }
    if (ws->msg_drop) {
        ws->oversized++;
        if (ws->io.on_oversized) {
            ws->io.on_oversized(ws->io.ctx);
        }
        return;
    }
    if (!ws->msg_binary && !fw_utf8_valid(ws->rx, ws->msg_len)) {
        fail(ws, FW_WS_INVALID_DATA, "utf8");
        return;
    }
    if (ws->io.on_message) {
        ws->io.on_message(ws->io.ctx, ws->msg_binary, ws->rx, ws->msg_len);
    }
}

/* Nagłówek kompletny: długość i początek ramki. 0 = dalej, -1 = połączenie zakończone. */
static int start_frame(fw_ws_t *ws)
{
    uint8_t len7 = ws->hdr[1] & 0x7Fu;
    uint8_t opcode = ws->opcode;
    if (len7 == 126) {
        ws->payload_len = (uint64_t)ws->hdr[2] << 8 | ws->hdr[3];
    } else if (len7 == 127) {
        unsigned k;
        if (ws->hdr[2] & 0x80u) {
            fail(ws, FW_WS_PROTOCOL_ERROR, "protocol");
            return -1;
        }
        ws->payload_len = 0;
        for (k = 2; k < 10; k++) {
            ws->payload_len = ws->payload_len << 8 | ws->hdr[k];
        }
    } else {
        ws->payload_len = len7;
    }
    ws->payload_got = 0;
    if (opcode == OP_CONT) {
        if (!ws->msg_active) {
            fail(ws, FW_WS_PROTOCOL_ERROR, "protocol");
            return -1;
        }
    } else if (opcode == OP_TEXT || opcode == OP_BINARY) {
        if (ws->msg_active) {
            fail(ws, FW_WS_PROTOCOL_ERROR, "protocol"); /* nowa wiadomość w środku fragmentów */
            return -1;
        }
        ws->msg_active = 1;
        ws->msg_binary = opcode == OP_BINARY;
        ws->msg_drop = 0;
        ws->msg_len = 0;
    }
    if (!(opcode & 0x8u) && ws->payload_len > FW_WS_MSG_MAX - ws->msg_len) {
        ws->msg_drop = 1;
    }
    ws->in_payload = 1;
    if (ws->payload_len == 0) {
        end_frame(ws);
    }
    return 0;
}

/* Pierwsze dwa bajty nagłówka. 0 = dalej, -1 = połączenie zakończone. */
static int parse_head(fw_ws_t *ws)
{
    uint8_t b0 = ws->hdr[0];
    uint8_t b1 = ws->hdr[1];
    uint8_t opcode = b0 & 0x0Fu;
    uint8_t len7 = b1 & 0x7Fu;
    int control = (opcode & 0x8u) != 0;
    if ((b0 & 0x70u) || (b1 & 0x80u)) {
        fail(ws, FW_WS_PROTOCOL_ERROR, "protocol"); /* bity RSV albo maska od serwera */
        return -1;
    }
    if (opcode != OP_CONT && opcode != OP_TEXT && opcode != OP_BINARY && opcode != OP_CLOSE &&
        opcode != OP_PING && opcode != OP_PONG) {
        fail(ws, FW_WS_PROTOCOL_ERROR, "protocol");
        return -1;
    }
    if (control && (!(b0 & 0x80u) || len7 > 125)) {
        fail(ws, FW_WS_PROTOCOL_ERROR, "protocol");
        return -1;
    }
    ws->opcode = opcode;
    ws->fin = (b0 & 0x80u) != 0;
    ws->hdr_need = (uint8_t)(2u + (len7 == 126 ? 2u : len7 == 127 ? 8u : 0u));
    return 0;
}

static void feed_frames(fw_ws_t *ws, const uint8_t *data, size_t len)
{
    size_t i = 0;
    while (i < len && (ws->state == FW_WS_OPEN || ws->state == FW_WS_CLOSING)) {
        size_t n;
        if (!ws->in_payload) {
            ws->hdr[ws->hdr_len++] = data[i++];
            if (ws->hdr_len == 2 && parse_head(ws) != 0) {
                return;
            }
            if (ws->hdr_len == ws->hdr_need && start_frame(ws) != 0) {
                return;
            }
            continue;
        }
        n = len - i;
        if ((uint64_t)n > ws->payload_len - ws->payload_got) {
            n = (size_t)(ws->payload_len - ws->payload_got);
        }
        if (ws->opcode & 0x8u) {
            memcpy(ws->ctrl + ws->payload_got, data + i, n);
        } else if (!ws->msg_drop) {
            memcpy(ws->rx + ws->msg_len, data + i, n);
            ws->msg_len += n;
        }
        ws->payload_got += n;
        i += n;
        if (ws->payload_got == ws->payload_len) {
            end_frame(ws);
        }
    }
}

void fw_ws_feed(fw_ws_t *ws, const uint8_t *data, size_t len, uint32_t now_ms)
{
    if (len && ws->state != FW_WS_CLOSED) {
        ws->last_rx_ms = now_ms;
        ws->ping_sent = 0;
        if (ws->state == FW_WS_HANDSHAKE) {
            feed_handshake(ws, data, len, now_ms);
        } else {
            feed_frames(ws, data, len);
        }
    }
    deliver_close(ws);
}

void fw_ws_tick(fw_ws_t *ws, uint32_t now_ms)
{
    uint32_t idle = now_ms - ws->last_rx_ms;
    if (ws->state == FW_WS_HANDSHAKE && now_ms - ws->state_ms >= FW_WS_HANDSHAKE_MS) {
        finish(ws, FW_WS_ABNORMAL, "handshake_timeout");
    } else if (ws->state == FW_WS_OPEN && idle >= FW_WS_DEAD_MS) {
        finish(ws, FW_WS_ABNORMAL, "timeout");
    } else if (ws->state == FW_WS_OPEN && idle >= FW_WS_PING_MS && !ws->ping_sent) {
        ws->ping_sent = 1;
        if (send_frame(ws, OP_PING, NULL, 0) != 0) {
            finish(ws, FW_WS_ABNORMAL, "transport");
        }
    } else if (ws->state == FW_WS_CLOSING && now_ms - ws->state_ms >= FW_WS_CLOSE_MS) {
        finish(ws, FW_WS_ABNORMAL, "close_timeout");
    }
    deliver_close(ws);
}
