#include "fw_json.h"

#include <string.h>

#include "fw_config.h"

/* ---------- zapis ---------- */

static void put(fw_jw_t *w, char c)
{
    if (w->len + 1 < w->cap) {
        w->buf[w->len++] = c;
    } else {
        w->overflow = 1;
    }
}

static void put_s(fw_jw_t *w, const char *s)
{
    while (*s) {
        put(w, *s++);
    }
}

static void put_u(fw_jw_t *w, uint32_t v)
{
    char tmp[10];
    int n = 0;
    do {
        tmp[n++] = (char)('0' + v % 10u);
        v /= 10u;
    } while (v);
    while (n) {
        put(w, tmp[--n]);
    }
}

void fw_jw_init(fw_jw_t *w, char *buf, size_t cap)
{
    memset(w, 0, sizeof(*w));
    w->buf = buf;
    w->cap = cap;
}

void fw_jw_open(fw_jw_t *w)
{
    if (w->depth >= (int)sizeof(w->first)) {
        w->overflow = 1;
        return;
    }
    put(w, '{');
    w->first[w->depth++] = 1;
}

void fw_jw_close(fw_jw_t *w)
{
    if (w->depth <= 0) {
        w->overflow = 1;
        return;
    }
    put(w, '}');
    w->depth--;
}

void fw_jw_str(fw_jw_t *w, const char *s)
{
    static const char hex[] = "0123456789abcdef";
    put(w, '"');
    for (; *s; s++) {
        unsigned char c = (unsigned char)*s;
        if (c == '"' || c == '\\') {
            put(w, '\\');
            put(w, (char)c);
        } else if (c == '\n') {
            put_s(w, "\\n");
        } else if (c == '\r') {
            put_s(w, "\\r");
        } else if (c == '\t') {
            put_s(w, "\\t");
        } else if (c < 0x20) {
            put_s(w, "\\u00");
            put(w, hex[c >> 4]);
            put(w, hex[c & 0xF]);
        } else {
            put(w, (char)c); /* UTF-8 przechodzi bez zmian */
        }
    }
    put(w, '"');
}

void fw_jw_key(fw_jw_t *w, const char *key)
{
    if (w->depth <= 0) {
        w->overflow = 1;
        return;
    }
    if (!w->first[w->depth - 1]) {
        put(w, ',');
    }
    w->first[w->depth - 1] = 0;
    fw_jw_str(w, key);
    put(w, ':');
}

void fw_jw_uint(fw_jw_t *w, uint32_t v)
{
    put_u(w, v);
}

void fw_jw_int(fw_jw_t *w, int32_t v)
{
    if (v < 0) {
        put(w, '-');
        put_u(w, (uint32_t)(-(int64_t)v));
    } else {
        put_u(w, (uint32_t)v);
    }
}

void fw_jw_bool(fw_jw_t *w, int v)
{
    put_s(w, v ? "true" : "false");
}

void fw_jw_fixed(fw_jw_t *w, int32_t value, unsigned decimals)
{
    uint32_t div = 1;
    uint32_t a;
    uint32_t frac;
    char digits[10];
    unsigned i;
    unsigned n;

    if (decimals > 9) {
        w->overflow = 1;
        return;
    }
    for (i = 0; i < decimals; i++) {
        div *= 10u;
    }
    a = value < 0 ? (uint32_t)(-(int64_t)value) : (uint32_t)value;
    frac = a % div;
    if (value < 0) {
        put(w, '-');
    }
    put_u(w, a / div);
    if (frac == 0) {
        return;
    }
    for (i = 0; i < decimals; i++) {
        digits[decimals - 1 - i] = (char)('0' + frac % 10u);
        frac /= 10u;
    }
    n = decimals;
    while (n > 0 && digits[n - 1] == '0') {
        n--;
    }
    put(w, '.');
    for (i = 0; i < n; i++) {
        put(w, digits[i]);
    }
}

size_t fw_jw_finish(fw_jw_t *w)
{
    if (w->cap == 0) {
        return 0;
    }
    w->buf[w->len < w->cap ? w->len : w->cap - 1] = '\0';
    if (w->overflow || w->depth != 0) {
        return 0;
    }
    return w->len;
}

/* ---------- odczyt ---------- */

typedef struct {
    const char *js;
    size_t len;
    size_t pos;
    fw_jtok_t *tok;
    int max;
    int n;
} parser_t;

static int add(parser_t *p, uint8_t type, size_t start, size_t end, int parent)
{
    if (p->n >= p->max) {
        return -1;
    }
    p->tok[p->n].type = type;
    p->tok[p->n].start = (uint16_t)start;
    p->tok[p->n].end = (uint16_t)end;
    p->tok[p->n].parent = (int16_t)parent;
    return p->n++;
}

static void skip_ws(parser_t *p)
{
    while (p->pos < p->len) {
        char c = p->js[p->pos];
        if (c != ' ' && c != '\t' && c != '\n' && c != '\r') {
            break;
        }
        p->pos++;
    }
}

static int is_hex(char c)
{
    return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');
}

static int is_digit(char c)
{
    return c >= '0' && c <= '9';
}

static int parse_string(parser_t *p, int parent)
{
    size_t start = ++p->pos; /* za cudzysłowem */
    while (p->pos < p->len) {
        unsigned char c = (unsigned char)p->js[p->pos];
        if (c == '"') {
            int idx = add(p, FW_JT_STR, start, p->pos, parent);
            p->pos++;
            return idx;
        }
        if (c < 0x20) {
            return -1;
        }
        if (c == '\\') {
            char e;
            if (p->pos + 1 >= p->len) {
                return -1;
            }
            e = p->js[p->pos + 1];
            if (e == 'u') {
                size_t k;
                if (p->pos + 5 >= p->len) {
                    return -1;
                }
                for (k = 2; k < 6; k++) {
                    if (!is_hex(p->js[p->pos + k])) {
                        return -1;
                    }
                }
                p->pos += 6;
                continue;
            }
            if (!strchr("\"\\/bfnrt", e) || e == '\0') {
                return -1;
            }
            p->pos += 2;
            continue;
        }
        p->pos++;
    }
    return -1;
}

static int parse_number(parser_t *p, int parent)
{
    size_t start = p->pos;
    const char *s = p->js;
    if (p->pos < p->len && s[p->pos] == '-') {
        p->pos++;
    }
    if (p->pos >= p->len) {
        return -1;
    }
    if (s[p->pos] == '0') {
        p->pos++;
    } else if (is_digit(s[p->pos])) {
        while (p->pos < p->len && is_digit(s[p->pos])) {
            p->pos++;
        }
    } else {
        return -1;
    }
    if (p->pos < p->len && s[p->pos] == '.') {
        p->pos++;
        if (p->pos >= p->len || !is_digit(s[p->pos])) {
            return -1;
        }
        while (p->pos < p->len && is_digit(s[p->pos])) {
            p->pos++;
        }
    }
    if (p->pos < p->len && (s[p->pos] == 'e' || s[p->pos] == 'E')) {
        p->pos++;
        if (p->pos < p->len && (s[p->pos] == '+' || s[p->pos] == '-')) {
            p->pos++;
        }
        if (p->pos >= p->len || !is_digit(s[p->pos])) {
            return -1;
        }
        while (p->pos < p->len && is_digit(s[p->pos])) {
            p->pos++;
        }
    }
    return add(p, FW_JT_PRIM, start, p->pos, parent);
}

static int parse_literal(parser_t *p, int parent, const char *word)
{
    size_t n = strlen(word);
    size_t start = p->pos;
    if (p->len - p->pos < n || memcmp(p->js + p->pos, word, n) != 0) {
        return -1;
    }
    p->pos += n;
    return add(p, FW_JT_PRIM, start, p->pos, parent);
}

static int parse_value(parser_t *p, int parent, int depth);

static int parse_container(parser_t *p, int parent, int depth, char open, char close)
{
    int idx;
    if (depth > FW_JSON_MAX_DEPTH) {
        return -1;
    }
    idx = add(p, open == '{' ? FW_JT_OBJ : FW_JT_ARR, p->pos, p->pos, parent);
    if (idx < 0) {
        return -1;
    }
    p->pos++;
    skip_ws(p);
    if (p->pos < p->len && p->js[p->pos] == close) {
        p->pos++;
        p->tok[idx].end = (uint16_t)p->pos;
        return idx;
    }
    for (;;) {
        skip_ws(p);
        if (open == '{') {
            int key;
            if (p->pos >= p->len || p->js[p->pos] != '"') {
                return -1;
            }
            key = parse_string(p, idx);
            if (key < 0) {
                return -1;
            }
            skip_ws(p);
            if (p->pos >= p->len || p->js[p->pos] != ':') {
                return -1;
            }
            p->pos++;
            skip_ws(p);
            if (parse_value(p, key, depth + 1) < 0) {
                return -1;
            }
        } else if (parse_value(p, idx, depth + 1) < 0) {
            return -1;
        }
        skip_ws(p);
        if (p->pos >= p->len) {
            return -1;
        }
        if (p->js[p->pos] == ',') {
            p->pos++;
            continue;
        }
        if (p->js[p->pos] == close) {
            p->pos++;
            p->tok[idx].end = (uint16_t)p->pos;
            return idx;
        }
        return -1;
    }
}

static int parse_value(parser_t *p, int parent, int depth)
{
    char c;
    skip_ws(p);
    if (p->pos >= p->len) {
        return -1;
    }
    c = p->js[p->pos];
    switch (c) {
    case '{':
        return parse_container(p, parent, depth, '{', '}');
    case '[':
        return parse_container(p, parent, depth, '[', ']');
    case '"':
        return parse_string(p, parent);
    case 't':
        return parse_literal(p, parent, "true");
    case 'f':
        return parse_literal(p, parent, "false");
    case 'n':
        return parse_literal(p, parent, "null");
    default:
        return parse_number(p, parent);
    }
}

int fw_json_parse(const char *js, size_t len, fw_jtok_t *tok, int max_tok)
{
    parser_t p;
    if (len > 0xFFFFu || max_tok <= 0) {
        return -1;
    }
    p.js = js;
    p.len = len;
    p.pos = 0;
    p.tok = tok;
    p.max = max_tok;
    p.n = 0;
    if (parse_value(&p, -1, 1) < 0) {
        return -1;
    }
    skip_ws(&p);
    if (p.pos != len) {
        return -1; /* śmieci za wartością */
    }
    return p.n;
}

static int hex4(const char *s, uint32_t *out)
{
    uint32_t v = 0;
    int i;
    for (i = 0; i < 4; i++) {
        char c = s[i];
        v <<= 4;
        if (c >= '0' && c <= '9') {
            v |= (uint32_t)(c - '0');
        } else if (c >= 'a' && c <= 'f') {
            v |= (uint32_t)(c - 'a' + 10);
        } else if (c >= 'A' && c <= 'F') {
            v |= (uint32_t)(c - 'A' + 10);
        } else {
            return -1;
        }
    }
    *out = v;
    return 0;
}

static int put_utf8(char *out, size_t cap, size_t *n, uint32_t cp)
{
    uint8_t b[4];
    size_t k;
    size_t len;
    if (cp == 0) {
        return -1; /* NUL w napisie C */
    }
    if (cp < 0x80) {
        b[0] = (uint8_t)cp;
        len = 1;
    } else if (cp < 0x800) {
        b[0] = (uint8_t)(0xC0 | (cp >> 6));
        b[1] = (uint8_t)(0x80 | (cp & 0x3F));
        len = 2;
    } else if (cp < 0x10000) {
        b[0] = (uint8_t)(0xE0 | (cp >> 12));
        b[1] = (uint8_t)(0x80 | ((cp >> 6) & 0x3F));
        b[2] = (uint8_t)(0x80 | (cp & 0x3F));
        len = 3;
    } else {
        b[0] = (uint8_t)(0xF0 | (cp >> 18));
        b[1] = (uint8_t)(0x80 | ((cp >> 12) & 0x3F));
        b[2] = (uint8_t)(0x80 | ((cp >> 6) & 0x3F));
        b[3] = (uint8_t)(0x80 | (cp & 0x3F));
        len = 4;
    }
    if (*n + len >= cap) {
        return -1;
    }
    for (k = 0; k < len; k++) {
        out[(*n)++] = (char)b[k];
    }
    return 0;
}

int fw_json_str(const char *js, const fw_jtok_t *t, char *out, size_t cap)
{
    size_t i = t->start;
    size_t n = 0;
    if (t->type != FW_JT_STR || cap == 0) {
        return -1;
    }
    while (i < t->end) {
        char c = js[i];
        if (c != '\\') {
            if (n + 1 >= cap) {
                return -1;
            }
            out[n++] = c;
            i++;
            continue;
        }
        c = js[i + 1];
        if (c == 'u') {
            uint32_t cp;
            if (hex4(js + i + 2, &cp) < 0) {
                return -1;
            }
            i += 6;
            if (cp >= 0xD800 && cp <= 0xDBFF) {
                uint32_t lo;
                if (i + 6 > t->end || js[i] != '\\' || js[i + 1] != 'u' || hex4(js + i + 2, &lo) < 0 ||
                    lo < 0xDC00 || lo > 0xDFFF) {
                    return -1;
                }
                cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                i += 6;
            } else if (cp >= 0xDC00 && cp <= 0xDFFF) {
                return -1;
            }
            if (put_utf8(out, cap, &n, cp) < 0) {
                return -1;
            }
            continue;
        }
        if (n + 1 >= cap) {
            return -1;
        }
        switch (c) {
        case 'n': out[n++] = '\n'; break;
        case 'r': out[n++] = '\r'; break;
        case 't': out[n++] = '\t'; break;
        case 'b': out[n++] = '\b'; break;
        case 'f': out[n++] = '\f'; break;
        default: out[n++] = c; break; /* \" \\ \/ */
        }
        i += 2;
    }
    out[n] = '\0';
    return (int)n;
}

int fw_json_find(const char *js, const fw_jtok_t *tok, int ntok, int obj, const char *key)
{
    char name[FW_TYPE_MAX + 1];
    int found = -1;
    int i;
    if (obj < 0 || obj >= ntok || tok[obj].type != FW_JT_OBJ) {
        return -1;
    }
    for (i = obj + 1; i + 1 < ntok && tok[i].start < tok[obj].end; i++) {
        if (tok[i].parent != obj) {
            continue;
        }
        /* Jak json.loads po stronie hosta: przy powtórzonym kluczu wygrywa ostatni. */
        if (fw_json_str(js, &tok[i], name, sizeof(name)) >= 0 && strcmp(name, key) == 0) {
            found = i + 1;
        }
    }
    return found;
}

static int is_number_token(const char *js, const fw_jtok_t *t)
{
    char c;
    if (t->type != FW_JT_PRIM || t->end <= t->start) {
        return 0;
    }
    c = js[t->start];
    return c == '-' || is_digit(c);
}

int fw_json_int(const char *js, const fw_jtok_t *t, int32_t *out)
{
    size_t i = t->start;
    int neg = 0;
    int64_t v = 0;
    if (!is_number_token(js, t)) {
        return -1;
    }
    if (js[i] == '-') {
        neg = 1;
        i++;
    }
    for (; i < t->end; i++) {
        if (!is_digit(js[i])) {
            return -1; /* ułamek lub wykładnik */
        }
        v = v * 10 + (js[i] - '0');
        if (v > 2147483648LL) {
            return -1;
        }
    }
    if (neg) {
        v = -v;
    }
    if (v > INT32_MAX || v < INT32_MIN) {
        return -1;
    }
    *out = (int32_t)v;
    return 0;
}

int fw_json_milli(const char *js, const fw_jtok_t *t, int32_t *out)
{
    size_t i = t->start;
    int neg = 0;
    int64_t mant = 0;
    int scale = 0;      /* liczba cyfr po przecinku w mantysie */
    int exp = 0;
    int exp_neg = 0;
    int digits = 0;
    if (!is_number_token(js, t)) {
        return -1;
    }
    if (js[i] == '-') {
        neg = 1;
        i++;
    }
    for (; i < t->end && is_digit(js[i]); i++) {
        if (digits < 15) {
            mant = mant * 10 + (js[i] - '0');
            if (mant != 0) {
                digits++;
            }
        } else {
            scale--; /* zbyt wiele cyfr całości: dalsze cyfry tylko zwiększają rząd wielkości */
        }
    }
    if (i < t->end && js[i] == '.') {
        for (i++; i < t->end && is_digit(js[i]); i++) {
            if (digits < 15) {
                mant = mant * 10 + (js[i] - '0');
                scale++;
                if (mant != 0) {
                    digits++;
                }
            }
        }
    }
    if (i < t->end && (js[i] == 'e' || js[i] == 'E')) {
        i++;
        if (js[i] == '+' || js[i] == '-') {
            exp_neg = js[i] == '-';
            i++;
        }
        for (; i < t->end; i++) {
            if (exp < 1000) {
                exp = exp * 10 + (js[i] - '0');
            }
        }
    }
    /* wartość = mant * 10^(exp_signed - scale); w tysięcznych: * 10^3 */
    {
        int shift = (exp_neg ? -exp : exp) - scale + 3;
        while (shift > 0 && mant != 0) {
            if (mant > 2147483647LL) {
                return -1;
            }
            mant *= 10;
            shift--;
        }
        while (shift < 0 && mant != 0) {
            mant /= 10; /* obcięcie w stronę zera */
            shift++;
        }
    }
    if (mant > 2147483647LL) {
        return -1;
    }
    *out = (int32_t)(neg ? -mant : mant);
    return 0;
}

int fw_json_is_bool(const char *js, const fw_jtok_t *t)
{
    return t->type == FW_JT_PRIM && (js[t->start] == 't' || js[t->start] == 'f');
}

int fw_json_is_number(const char *js, const fw_jtok_t *t)
{
    return is_number_token(js, t);
}
