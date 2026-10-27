#include "fw_body.h"

#include <string.h>

#if FW_PLAYBACK_BYTES > 65535u
#error "pb_offset jest 16-bitowy: FW_PLAYBACK_BYTES musi być mniejsze niż 64 KB"
#endif

#define AUDIO_HEADER 5u
#define KIND_MIC 1u
#define KIND_PLAYBACK 2u

static const char *const SENSOR_NAMES[FW_SENSOR_COUNT] = {"mic", "imu", "temp", "presence", "gps"};
static const char *const HAPTIC_PATTERNS[] = {"attention", "warn", "critical"};

const char *fw_sensor_name(fw_sensor_t sensor)
{
    return (unsigned)sensor < FW_SENSOR_COUNT ? SENSOR_NAMES[sensor] : "?";
}

/* ---------- wysyłka i bufor zdarzeń ---------- */

static void outbox_push(fw_body_t *b, const char *frame, size_t len)
{
    fw_slot_t *slot;
    if (b->outbox_count == FW_OUTBOX_LEN) {
        /* pełny bufor: giną najstarsze (spec, sekcja 13) */
        b->outbox_head = (uint16_t)((b->outbox_head + 1u) % FW_OUTBOX_LEN);
        b->outbox_count--;
        b->dropped_events++;
    }
    slot = &b->outbox[(b->outbox_head + b->outbox_count) % FW_OUTBOX_LEN];
    memcpy(slot->data, frame, len);
    slot->len = (uint16_t)len;
    b->outbox_count++;
}

static int send_text(fw_body_t *b, const char *frame, size_t len)
{
    if (!b->link_up) {
        return -1;
    }
    if (b->hal.send_text(b->hal.ctx, frame, len) != 0) {
        b->link_up = 0; /* do ponownego zestawienia, które zgłosi port */
        return -1;
    }
    return 0;
}

/* Zdarzenia trafiają do bufora przy braku łącza; heartbeat i ack przepadają (spec, sekcja 11). */
static void emit(fw_body_t *b, size_t len, int bufferable)
{
    if (len == 0) {
        b->dropped_events++; /* ramka nie zmieściła się w FW_TX_TEXT_MAX */
        return;
    }
    if (send_text(b, b->tx, len) == 0) {
        return;
    }
    if (bufferable) {
        outbox_push(b, b->tx, len);
    }
}

void fw_body_set_link(fw_body_t *b, int up)
{
    b->link_up = up ? 1 : 0;
    while (b->link_up && b->outbox_count) {
        fw_slot_t *slot = &b->outbox[b->outbox_head];
        if (send_text(b, slot->data, slot->len) != 0) {
            return; /* zostaje w buforze, kolejność bez zmian */
        }
        b->outbox_head = (uint16_t)((b->outbox_head + 1u) % FW_OUTBOX_LEN);
        b->outbox_count--;
    }
}

/* ---------- koperta ---------- */

static void write_id(fw_body_t *b, fw_jw_t *w)
{
    char id[4 + 1 + 10 + 1];
    uint32_t n = ++b->next_id;
    char digits[10];
    int k = 0;
    int i = 4;
    memcpy(id, b->boot_id, 4);
    id[i++] = '.';
    do {
        digits[k++] = (char)('0' + n % 10u);
        n /= 10u;
    } while (n);
    while (k) {
        id[i++] = digits[--k];
    }
    id[i] = '\0';
    fw_jw_str(w, id);
}

static void begin(fw_body_t *b, fw_jw_t *w, const char *t, const char *type, uint32_t now_ms)
{
    fw_jw_init(w, b->tx, sizeof(b->tx));
    fw_jw_open(w);
    fw_jw_key(w, "v");
    fw_jw_int(w, FW_PROTOCOL_VERSION);
    fw_jw_key(w, "t");
    fw_jw_str(w, t);
    fw_jw_key(w, "id");
    write_id(b, w);
    fw_jw_key(w, "ts");
    fw_jw_uint(w, now_ms - b->boot_ms); /* ms od startu, z przepełnieniem co ok. 49 dni */
    fw_jw_key(w, "type");
    fw_jw_str(w, type);
    fw_jw_key(w, "body");
    fw_jw_open(w);
}

static size_t finish(fw_jw_t *w)
{
    fw_jw_close(w);
    fw_jw_close(w);
    return fw_jw_finish(w);
}

/* ---------- start i upływ czasu ---------- */

static void playback_reset(fw_body_t *b)
{
    b->pb_used = 0;
    b->pb_packets = 0;
    b->pb_id = 0;
    b->pb_playing = 0;
    b->pb_truncated = 0;
    memset(b->pb_length, 0, sizeof(b->pb_length));
}

void fw_body_init(fw_body_t *b, const fw_hal_t *hal, const fw_settings_t *settings,
                  uint32_t now_ms, uint32_t random32, const char *reset_reason)
{
    static const char hex[] = "0123456789abcdef";
    fw_jw_t w;
    int i;

    memset(b, 0, sizeof(*b));
    b->hal = *hal;
    b->settings = *settings;
    b->boot_ms = now_ms;
    for (i = 0; i < 8; i++) {
        b->boot_id[i] = hex[(random32 >> (28 - 4 * i)) & 0xFu];
    }
    b->boot_id[8] = '\0';
    b->next_heartbeat_ms = now_ms;
    b->last_sensor_ms = now_ms;
    b->volume_milli = 50000;
    b->batt_warn_milli = 20000;
    b->temp_warn_milli = 40000;
    playback_reset(b);

    begin(b, &w, "event", "system.boot", now_ms);
    fw_jw_key(&w, "boot_id");
    fw_jw_str(&w, b->boot_id);
    fw_jw_key(&w, "fw");
    fw_jw_str(&w, FW_VERSION);
    fw_jw_key(&w, "reset_reason");
    fw_jw_str(&w, reset_reason);
    emit(b, finish(&w), 1);
}

static void send_heartbeat(fw_body_t *b, uint32_t now_ms)
{
    fw_jw_t w;
    begin(b, &w, "event", "body.heartbeat", now_ms);
    fw_jw_key(&w, "batt_pct");
    fw_jw_fixed(&w, b->batt_x10, 1);
    fw_jw_key(&w, "link_rssi");
    fw_jw_int(&w, b->rssi);
    fw_jw_key(&w, "frame_errors");
    fw_jw_uint(&w, b->frame_errors);
    emit(b, finish(&w), 0);
}

static int faulted(const fw_body_t *b, fw_sensor_t s)
{
    return (b->faults >> s) & 1u;
}

static void maybe_send_temperature(fw_body_t *b, uint32_t now_ms)
{
    int32_t diff;
    fw_jw_t w;
    if (!b->temp_valid || faulted(b, FW_SENSOR_TEMP)) {
        return;
    }
    diff = b->temp_x10 - b->sent_temp_x10;
    if (diff < 0) {
        diff = -diff;
    }
    if (b->temp_sent && diff < FW_TEMP_DELTA_X10 && now_ms - b->last_sensor_ms < FW_SENSOR_PERIOD_MS) {
        return;
    }
    begin(b, &w, "event", "body.sensor", now_ms);
    fw_jw_key(&w, "temp_c");
    fw_jw_fixed(&w, b->temp_x10, 1);
    b->sent_temp_x10 = b->temp_x10;
    b->temp_sent = 1;
    b->last_sensor_ms = now_ms;
    emit(b, finish(&w), 1);
}

void fw_body_tick(fw_body_t *b, uint32_t now_ms)
{
    /* Heartbeat tylko przy łączu: nie jest buforowany, a po zestawieniu wychodzi od razu. */
    if (b->link_up && (int32_t)(now_ms - b->next_heartbeat_ms) >= 0) {
        b->next_heartbeat_ms = now_ms + FW_HEARTBEAT_MS;
        send_heartbeat(b, now_ms);
    }
    maybe_send_temperature(b, now_ms);
}

/* ---------- odczyty i zdarzenia lokalne ---------- */

void fw_body_set_battery(fw_body_t *b, int32_t pct_x10)
{
    b->batt_x10 = pct_x10 < 0 ? 0 : (pct_x10 > 1000 ? 1000 : pct_x10);
}

void fw_body_set_rssi(fw_body_t *b, int16_t rssi)
{
    b->rssi = rssi;
}

void fw_body_set_temp(fw_body_t *b, int32_t temp_x10)
{
    b->temp_x10 = temp_x10;
    b->temp_valid = 1;
}

void fw_body_set_presence(fw_body_t *b, int present, uint32_t now_ms)
{
    fw_jw_t w;
    present = present ? 1 : 0;
    if (faulted(b, FW_SENSOR_PRESENCE) || (b->presence_known && b->presence == present)) {
        return;
    }
    b->presence = present;
    b->presence_known = 1;
    begin(b, &w, "event", "body.sensor", now_ms);
    fw_jw_key(&w, "presence");
    fw_jw_bool(&w, present);
    emit(b, finish(&w), 1);
}

void fw_body_gps(fw_body_t *b, int32_t lat_e7, int32_t lon_e7, uint32_t now_ms)
{
    fw_jw_t w;
    if (faulted(b, FW_SENSOR_GPS)) {
        return;
    }
    begin(b, &w, "event", "body.sensor", now_ms);
    fw_jw_key(&w, "gps");
    fw_jw_open(&w);
    fw_jw_key(&w, "lat");
    fw_jw_fixed(&w, lat_e7, 7);
    fw_jw_key(&w, "lon");
    fw_jw_fixed(&w, lon_e7, 7);
    fw_jw_close(&w);
    emit(b, finish(&w), 1);
}

void fw_body_fault(fw_body_t *b, fw_sensor_t sensor, const char *code, uint32_t now_ms)
{
    fw_jw_t w;
    if ((unsigned)sensor >= FW_SENSOR_COUNT) {
        return;
    }
    b->faults = (uint8_t)(b->faults | (1u << sensor));
    begin(b, &w, "event", "body.fault", now_ms);
    fw_jw_key(&w, "sensor");
    fw_jw_str(&w, SENSOR_NAMES[sensor]);
    fw_jw_key(&w, "code");
    fw_jw_str(&w, code);
    emit(b, finish(&w), 1);
}

void fw_body_fault_clear(fw_body_t *b, fw_sensor_t sensor, uint32_t now_ms)
{
    fw_jw_t w;
    if ((unsigned)sensor >= FW_SENSOR_COUNT || !faulted(b, sensor)) {
        return;
    }
    b->faults = (uint8_t)(b->faults & ~(1u << sensor));
    if (sensor == FW_SENSOR_TEMP) {
        b->temp_sent = 0; /* po usterce odczyt idzie od razu */
    }
    begin(b, &w, "event", "body.fault.clear", now_ms);
    fw_jw_key(&w, "sensor");
    fw_jw_str(&w, SENSOR_NAMES[sensor]);
    emit(b, finish(&w), 1);
}

void fw_body_button(fw_body_t *b, const char *button_id, int long_press, uint32_t now_ms)
{
    fw_jw_t w;
    begin(b, &w, "event", "button", now_ms);
    fw_jw_key(&w, "id");
    fw_jw_str(&w, button_id);
    fw_jw_key(&w, "kind");
    fw_jw_str(&w, long_press ? "long" : "press");
    emit(b, finish(&w), 1);
}

void fw_body_local_command(fw_body_t *b, const char *cmd, const char *result, uint32_t now_ms)
{
    fw_jw_t w;
    begin(b, &w, "event", "local.command", now_ms);
    fw_jw_key(&w, "cmd");
    fw_jw_str(&w, cmd);
    fw_jw_key(&w, "result");
    fw_jw_str(&w, result);
    emit(b, finish(&w), 1);
}

/* ---------- mikrofon ---------- */

uint32_t fw_body_utterance_start(fw_body_t *b, uint32_t now_ms)
{
    fw_jw_t w;
    if (faulted(b, FW_SENSOR_MIC)) {
        return 0;
    }
    b->utt_id++;
    if (b->utt_id == 0) {
        b->utt_id = 1;
    }
    b->mic_seq = 0;
    b->utt_active = 1;
    begin(b, &w, "event", "audio.utterance.start", now_ms);
    fw_jw_key(&w, "utt_id");
    fw_jw_uint(&w, b->utt_id);
    emit(b, finish(&w), 1);
    return b->utt_id;
}

void fw_body_mic_packet(fw_body_t *b, const uint8_t *opus, size_t len)
{
    uint16_t id;
    if (!b->utt_active || len > sizeof(b->txbin) - AUDIO_HEADER) {
        return;
    }
    id = (uint16_t)(b->utt_id & 0xFFFFu);
    b->txbin[0] = KIND_MIC;
    b->txbin[1] = (uint8_t)(id >> 8);
    b->txbin[2] = (uint8_t)(id & 0xFFu);
    b->txbin[3] = (uint8_t)(b->mic_seq >> 8);
    b->txbin[4] = (uint8_t)(b->mic_seq & 0xFFu);
    memcpy(b->txbin + AUDIO_HEADER, opus, len);
    b->mic_seq++;
    /* Audio nie jest buforowane: bez łącza pakiet przepada. */
    if (b->link_up && b->hal.send_binary(b->hal.ctx, b->txbin, AUDIO_HEADER + len) != 0) {
        b->link_up = 0;
    }
}

void fw_body_utterance_end(fw_body_t *b, uint32_t dur_ms, uint32_t now_ms)
{
    fw_jw_t w;
    if (!b->utt_active) {
        return;
    }
    b->utt_active = 0;
    begin(b, &w, "event", "audio.utterance.end", now_ms);
    fw_jw_key(&w, "utt_id");
    fw_jw_uint(&w, b->utt_id);
    fw_jw_key(&w, "dur_ms");
    fw_jw_uint(&w, dur_ms);
    emit(b, finish(&w), 1);
}

/* ---------- odtwarzanie (ramki kind=2) ---------- */

void fw_body_on_binary(fw_body_t *b, const uint8_t *frame, size_t len)
{
    uint16_t id;
    uint16_t seq;
    size_t data_len;
    if (len < AUDIO_HEADER || frame[0] != KIND_PLAYBACK) {
        b->frame_errors++;
        return;
    }
    id = (uint16_t)((frame[1] << 8) | frame[2]);
    seq = (uint16_t)((frame[3] << 8) | frame[4]);
    data_len = len - AUDIO_HEADER;
    if (id == 0) {
        b->frame_errors++; /* audio_id jest z zakresu 1-65535 */
        return;
    }
    if (b->pb_playing) {
        b->busy_drops++; /* jeden bufor: w trakcie odtwarzania nic nie przyjmujemy */
        return;
    }
    if (id != b->pb_id) {
        playback_reset(b); /* nowe audio_id zastępuje niezapowiedziane ramki */
        b->pb_id = id;
    }
    if (seq >= FW_PLAYBACK_MAX_PACKETS || b->pb_used + data_len > FW_PLAYBACK_BYTES) {
        b->pb_truncated = 1;
        return;
    }
    if (b->pb_length[seq] != 0 || data_len == 0) {
        return; /* powtórzony albo pusty pakiet */
    }
    memcpy(b->pb_pool + b->pb_used, frame + AUDIO_HEADER, data_len);
    b->pb_offset[seq] = (uint16_t)b->pb_used;
    b->pb_length[seq] = (uint16_t)data_len;
    b->pb_used += (uint32_t)data_len;
    if (seq + 1u > b->pb_packets) {
        b->pb_packets = (uint16_t)(seq + 1u);
    }
}

void fw_body_playback_done(fw_body_t *b)
{
    playback_reset(b);
}

void fw_body_frame_error(fw_body_t *b)
{
    b->frame_errors++;
}

/* ---------- akcje od hosta ---------- */

static void send_ack(fw_body_t *b, const char *type, const char *ref, const char *err, uint32_t now_ms)
{
    fw_jw_t w;
    begin(b, &w, "ack", type, now_ms);
    fw_jw_key(&w, "ref");
    fw_jw_str(&w, ref);
    fw_jw_key(&w, "ok");
    fw_jw_bool(&w, err == NULL);
    if (err) {
        fw_jw_key(&w, "err");
        fw_jw_str(&w, err);
    }
    emit(b, finish(&w), 0);
}

typedef struct {
    const char *js;
    int ntok;
    int body; /* indeks obiektu body albo -1 */
} action_t;

static int field(fw_body_t *b, const action_t *a, const char *key)
{
    return a->body < 0 ? -1 : fw_json_find(a->js, b->tok, a->ntok, a->body, key);
}

static int get_int(fw_body_t *b, const action_t *a, const char *key, int32_t *out)
{
    int t = field(b, a, key);
    return t < 0 ? -1 : fw_json_int(a->js, &b->tok[t], out);
}

/* -1 = brak pola lub nie napis; -2 = napis, ale za długi dla bufora */
static int get_str(fw_body_t *b, const action_t *a, const char *key, char *out, size_t cap)
{
    int t = field(b, a, key);
    if (t < 0 || b->tok[t].type != FW_JT_STR) {
        return -1;
    }
    return fw_json_str(a->js, &b->tok[t], out, cap) < 0 ? -2 : 0;
}

static const char *act_haptic(fw_body_t *b, const action_t *a)
{
    char pattern[16];
    size_t i;
    int r = get_str(b, a, "pattern", pattern, sizeof(pattern));
    if (r == -1) {
        return "bad_body";
    }
    for (i = 0; r == 0 && i < sizeof(HAPTIC_PATTERNS) / sizeof(HAPTIC_PATTERNS[0]); i++) {
        if (strcmp(pattern, HAPTIC_PATTERNS[i]) == 0) {
            b->hal.haptic(b->hal.ctx, HAPTIC_PATTERNS[i]);
            return NULL;
        }
    }
    return "bad_value";
}

static int audio_id_of(fw_body_t *b, const action_t *a, int32_t *out)
{
    char text[12];
    int t = field(b, a, "audio_id");
    int r;
    size_t i;
    int32_t v = 0;
    if (t < 0) {
        return -1;
    }
    if (b->tok[t].type != FW_JT_STR) {
        return fw_json_int(a->js, &b->tok[t], out);
    }
    /* Spec dopuszcza audio_id jako napis z liczbą. */
    r = fw_json_str(a->js, &b->tok[t], text, sizeof(text));
    if (r <= 0 || r > 9) {
        return -1;
    }
    for (i = 0; i < (size_t)r; i++) {
        if (text[i] < '0' || text[i] > '9') {
            return -1;
        }
        v = v * 10 + (text[i] - '0');
    }
    *out = v;
    return 0;
}

static const char *act_speak(fw_body_t *b, const action_t *a)
{
    int32_t id;
    fw_playback_t pb;
    if (audio_id_of(b, a, &id) < 0) {
        return "bad_body";
    }
    if (b->pb_playing) {
        return "busy";
    }
    if (id <= 0 || id > 0xFFFF || b->pb_id != (uint16_t)id || b->pb_used == 0) {
        return "no_audio";
    }
    b->pb_playing = 1;
    pb.pool = b->pb_pool;
    pb.offset = b->pb_offset;
    pb.length = b->pb_length;
    pb.packets = b->pb_packets;
    pb.audio_id = b->pb_id;
    b->hal.play_audio(b->hal.ctx, &pb);
    return NULL;
}

static const char *act_clip(fw_body_t *b, const action_t *a)
{
    char clip[FW_TEXT_MAX];
    uint8_t i;
    int r = get_str(b, a, "clip", clip, sizeof(clip));
    if (r == -1) {
        return "bad_body";
    }
    for (i = 0; r == 0 && i < b->settings.clip_count; i++) {
        if (strcmp(clip, b->settings.clips[i]) == 0) {
            b->hal.play_clip(b->hal.ctx, b->settings.clips[i]);
            return NULL;
        }
    }
    return "unknown_clip";
}

static const char *act_gpio(fw_body_t *b, const action_t *a)
{
    int32_t pin;
    int32_t value;
    uint8_t i;
    if (get_int(b, a, "pin", &pin) < 0 || get_int(b, a, "value", &value) < 0) {
        return "bad_body";
    }
    for (i = 0; i < b->settings.gpio_count; i++) {
        if (pin == b->settings.gpio_pins[i]) {
            break;
        }
    }
    if (i == b->settings.gpio_count) {
        return "pin_not_allowed";
    }
    if (value != 0 && value != 1) {
        return "bad_value";
    }
    b->hal.gpio_write(b->hal.ctx, (uint8_t)pin, (uint8_t)value);
    return NULL;
}

static const char *act_config(fw_body_t *b, const action_t *a)
{
    char key[16];
    int32_t value;
    int32_t *target;
    int ranged;
    int t;
    int r = get_str(b, a, "key", key, sizeof(key));
    if (r == -1) {
        return "bad_body";
    }
    if (r == 0 && strcmp(key, "volume") == 0) {
        target = &b->volume_milli;
        ranged = 1;
    } else if (r == 0 && strcmp(key, "batt_warn_pct") == 0) {
        target = &b->batt_warn_milli;
        ranged = 1;
    } else if (r == 0 && strcmp(key, "temp_warn_c") == 0) {
        target = &b->temp_warn_milli;
        ranged = 0;
    } else {
        return "unknown_key";
    }
    t = field(b, a, "value");
    if (t < 0 || !fw_json_is_number(a->js, &b->tok[t])) {
        return "bad_body";
    }
    if (fw_json_milli(a->js, &b->tok[t], &value) < 0) {
        return "bad_value"; /* liczba poza zakresem int32 w tysięcznych */
    }
    if (ranged && (value < 0 || value > 100000)) {
        return "bad_value";
    }
    *target = value;
    b->hal.config_changed(b->hal.ctx, key, value);
    return NULL;
}

static int envelope_str(fw_body_t *b, const char *js, int ntok, const char *key, char *out, size_t cap)
{
    int t = fw_json_find(js, b->tok, ntok, 0, key);
    return (t < 0 || b->tok[t].type != FW_JT_STR) ? -1 : fw_json_str(js, &b->tok[t], out, cap);
}

void fw_body_on_text(fw_body_t *b, const char *frame, size_t len, uint32_t now_ms)
{
    char t_field[8];
    char id[FW_ID_MAX + 1];
    char type[FW_TYPE_MAX + 1];
    const char *err;
    action_t a;
    int32_t v;
    int vt;
    int ntok;

    if (len > FW_RX_TEXT_MAX) {
        b->frame_errors++;
        return;
    }
    ntok = fw_json_parse(frame, len, b->tok, FW_JSON_MAX_TOKENS);
    if (ntok <= 0 || b->tok[0].type != FW_JT_OBJ) {
        b->frame_errors++;
        return;
    }
    vt = fw_json_find(frame, b->tok, ntok, 0, "v");
    if (vt < 0 || fw_json_int(frame, &b->tok[vt], &v) < 0 || v != FW_PROTOCOL_VERSION ||
        envelope_str(b, frame, ntok, "t", t_field, sizeof(t_field)) < 0 || strcmp(t_field, "action") != 0 ||
        envelope_str(b, frame, ntok, "id", id, sizeof(id)) < 0 ||
        envelope_str(b, frame, ntok, "type", type, sizeof(type)) < 0) {
        b->frame_errors++;
        return;
    }
    a.js = frame;
    a.ntok = ntok;
    a.body = fw_json_find(frame, b->tok, ntok, 0, "body");
    if (a.body >= 0 && b->tok[a.body].type != FW_JT_OBJ) {
        b->frame_errors++;
        return;
    }

    if (strcmp(type, "ping") == 0) {
        err = NULL;
    } else if (strcmp(type, "haptic") == 0) {
        err = act_haptic(b, &a);
    } else if (strcmp(type, "speak") == 0) {
        err = act_speak(b, &a);
    } else if (strcmp(type, "speak.clip") == 0) {
        err = act_clip(b, &a);
    } else if (strcmp(type, "gpio.set") == 0) {
        err = act_gpio(b, &a);
    } else if (strcmp(type, "config.set") == 0) {
        err = act_config(b, &a);
    } else {
        err = "unknown_action";
    }
    send_ack(b, type, id, err, now_ms);
}
