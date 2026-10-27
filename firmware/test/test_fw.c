/* Testy rdzenia firmware. Uruchamiane natywnie pod ASan/UBSan (make test) i valgrindem. */
#include <stdio.h>
#include <string.h>

#include "../core/fw_body.h"
#include "../core/fw_json.h"
#include "fake_hal.h"

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

#define CHECK_STR(a, b) CHECK(strcmp((a), (b)) == 0)

static fw_body_t body; /* ok. 66 KB: statycznie, nie na stosie */
static fake_hal_t fake;
static fw_hal_t hal;
static fw_settings_t settings;

static const char *out_text(int i)
{
    static char buf[FAKE_FRAME_MAX + 1];
    if (i < 0 || i >= fake.out_count || fake.out[i].binary) {
        return "";
    }
    memcpy(buf, fake.out[i].data, fake.out[i].len);
    buf[fake.out[i].len] = '\0';
    return buf;
}

static int out_has(int i, const char *needle)
{
    return strstr(out_text(i), needle) != NULL;
}

static void setup(int link)
{
    fake_hal_init(&fake, &hal);
    memset(&settings, 0, sizeof(settings));
    settings.gpio_pins[0] = 5;
    settings.gpio_pins[1] = 6;
    settings.gpio_count = 2;
    settings.clips[0] = "ready";
    settings.clips[1] = "error";
    settings.clip_count = 2;
    fw_body_init(&body, &hal, &settings, 1000, 0xA1B2C3D4u, "power");
    fw_body_set_battery(&body, 710);
    fw_body_set_rssi(&body, -58);
    if (link) {
        fw_body_set_link(&body, 1);
    }
}

static void action(const char *json)
{
    fw_body_on_text(&body, json, strlen(json), 2000);
}

static int count_type(const char *type)
{
    char needle[64];
    int i;
    int n = 0;
    snprintf(needle, sizeof(needle), "\"type\":\"%s\"", type);
    for (i = 0; i < fake.out_count; i++) {
        n += out_has(i, needle);
    }
    return n;
}

static const char *last_ack(void)
{
    int i;
    for (i = fake.out_count - 1; i >= 0; i--) {
        if (out_has(i, "\"t\":\"ack\"")) {
            return out_text(i);
        }
    }
    return "";
}

/* ---------- zapis JSON ---------- */

static void test_writer(void)
{
    char buf[128];
    fw_jw_t w;
    fw_jw_init(&w, buf, sizeof(buf));
    fw_jw_open(&w);
    fw_jw_key(&w, "s");
    fw_jw_str(&w, "a\"b\\c\n\x01ż");
    fw_jw_key(&w, "n");
    fw_jw_int(&w, -42);
    fw_jw_key(&w, "o");
    fw_jw_open(&w);
    fw_jw_key(&w, "t");
    fw_jw_bool(&w, 1);
    fw_jw_close(&w);
    fw_jw_close(&w);
    CHECK(fw_jw_finish(&w) > 0);
    CHECK_STR(buf, "{\"s\":\"a\\\"b\\\\c\\n\\u0001ż\",\"n\":-42,\"o\":{\"t\":true}}");
}

static const char *fixed(int32_t v, unsigned d)
{
    static char buf[32];
    fw_jw_t w;
    fw_jw_init(&w, buf, sizeof(buf));
    fw_jw_fixed(&w, v, d);
    fw_jw_finish(&w);
    return buf;
}

static void test_writer_fixed_point(void)
{
    CHECK_STR(fixed(710, 1), "71");
    CHECK_STR(fixed(715, 1), "71.5");
    CHECK_STR(fixed(-35, 1), "-3.5");
    CHECK_STR(fixed(-5, 1), "-0.5");
    CHECK_STR(fixed(522297000, 7), "52.2297");
    CHECK_STR(fixed(1, 7), "0.0000001");
    CHECK_STR(fixed(0, 3), "0");
    CHECK_STR(fixed(INT32_MIN, 0), "-2147483648");
}

static void test_writer_overflow_and_balance(void)
{
    char buf[8];
    fw_jw_t w;
    fw_jw_init(&w, buf, sizeof(buf));
    fw_jw_open(&w);
    fw_jw_key(&w, "długi_klucz");
    fw_jw_close(&w);
    CHECK(fw_jw_finish(&w) == 0);
    CHECK(strlen(buf) < sizeof(buf));
    fw_jw_init(&w, buf, sizeof(buf));
    fw_jw_open(&w);
    CHECK(fw_jw_finish(&w) == 0); /* niezamknięty obiekt */
}

/* ---------- odczyt JSON ---------- */

static fw_jtok_t tok[FW_JSON_MAX_TOKENS];

static int parse(const char *js)
{
    return fw_json_parse(js, strlen(js), tok, FW_JSON_MAX_TOKENS);
}

static void test_parser_accepts_valid_json(void)
{
    const char *js = " {\"a\": [1, -2.5e3, true, null], \"b\": {\"c\": \"x\\u00f3\\ud83d\\ude00\"}} ";
    char s[16];
    int n = parse(js);
    int b;
    int c;
    CHECK(n > 0);
    b = fw_json_find(js, tok, n, 0, "b");
    CHECK(b > 0 && tok[b].type == FW_JT_OBJ);
    c = fw_json_find(js, tok, n, b, "c");
    CHECK(c > 0 && fw_json_str(js, &tok[c], s, sizeof(s)) == 7);
    CHECK_STR(s, "x\xc3\xb3\xf0\x9f\x98\x80"); /* xó + emoji jako UTF-8 */
    CHECK(fw_json_find(js, tok, n, 0, "zzz") == -1);
}

static void test_parser_rejects_invalid_json(void)
{
    static const char *bad[] = {
        "", "{", "}", "{\"a\":1,}", "{\"a\" 1}", "{a:1}", "{\"a\":01}", "{\"a\":1.}", "{\"a\":-}",
        "{\"a\":.5}", "{\"a\":1e}", "{\"a\":\"x\\q\"}", "{\"a\":\"x\\u12g4\"}", "{\"a\":\"\x01\"}",
        "{\"a\":tru}", "{\"a\":1} x", "[1,2", "{\"a\":[[[[[1]]]]]}", "\"bez końca",
    };
    size_t i;
    for (i = 0; i < sizeof(bad) / sizeof(bad[0]); i++) {
        if (parse(bad[i]) >= 0) {
            fprintf(stderr, "przyjęto błędny JSON: %s\n", bad[i]);
            CHECK(0);
        }
    }
}

static void test_parser_token_limit(void)
{
    char js[600];
    size_t n = 0;
    int i;
    js[n++] = '[';
    for (i = 0; i < 100; i++) {
        js[n++] = '1';
        js[n++] = ',';
    }
    js[n - 1] = ']';
    js[n] = '\0';
    CHECK(parse(js) == -1);
}

static void test_parser_duplicate_key_takes_last(void)
{
    const char *js = "{\"type\":\"ping\",\"type\":\"haptic\"}";
    char s[16];
    int n = parse(js);
    int t = fw_json_find(js, tok, n, 0, "type");
    CHECK(t > 0 && fw_json_str(js, &tok[t], s, sizeof(s)) > 0);
    CHECK_STR(s, "haptic");
}

static void test_escaped_keys_match(void)
{
    const char *js = "{\"\\u0074ype\":\"ping\"}";
    int n = parse(js);
    CHECK(fw_json_find(js, tok, n, 0, "type") > 0);
}

static void test_string_decoding_limits(void)
{
    const char *js = "[\"\\ud83d\", \"\\u0000\", \"abcdef\"]";
    char s[4];
    int n = parse(js);
    CHECK(n == 4);
    CHECK(fw_json_str(js, &tok[1], s, sizeof(s)) == -1); /* samotny surogat */
    CHECK(fw_json_str(js, &tok[2], s, sizeof(s)) == -1); /* NUL */
    CHECK(fw_json_str(js, &tok[3], s, sizeof(s)) == -1); /* za długi */
    CHECK(fw_json_str(js, &tok[0], s, sizeof(s)) == -1); /* nie napis */
}

static void test_numbers(void)
{
    const char *js = "[7, -2147483648, 2147483648, 1.5, 1e2, \"7\", true, 38.5, -0.001, 0.0001, 1e20, 12.3456]";
    int32_t v;
    int n = parse(js);
    CHECK(n == 13);
    CHECK(fw_json_int(js, &tok[1], &v) == 0 && v == 7);
    CHECK(fw_json_int(js, &tok[2], &v) == 0 && v == INT32_MIN);
    CHECK(fw_json_int(js, &tok[3], &v) == -1);
    CHECK(fw_json_int(js, &tok[4], &v) == -1);
    CHECK(fw_json_int(js, &tok[5], &v) == -1);
    CHECK(fw_json_int(js, &tok[6], &v) == -1);
    CHECK(fw_json_int(js, &tok[7], &v) == -1);
    CHECK(fw_json_milli(js, &tok[5], &v) == 0 && v == 100000);
    CHECK(fw_json_milli(js, &tok[8], &v) == 0 && v == 38500);
    CHECK(fw_json_milli(js, &tok[9], &v) == 0 && v == -1);
    CHECK(fw_json_milli(js, &tok[10], &v) == 0 && v == 0);
    CHECK(fw_json_milli(js, &tok[11], &v) == -1);
    CHECK(fw_json_milli(js, &tok[12], &v) == 0 && v == 12345); /* obcięcie w stronę zera */
    CHECK(fw_json_is_number(js, &tok[1]) && !fw_json_is_number(js, &tok[6]) && !fw_json_is_number(js, &tok[7]));
}

/* ---------- ciało: start, łącze, bufor ---------- */

static void test_boot_waits_in_outbox_until_link(void)
{
    setup(0);
    CHECK(fake.out_count == 0 && body.outbox_count == 1);
    fw_body_set_link(&body, 1);
    CHECK(fake.out_count == 1 && body.outbox_count == 0);
    CHECK_STR(out_text(0), "{\"v\":1,\"t\":\"event\",\"id\":\"a1b2.1\",\"ts\":0,\"type\":\"system.boot\","
                           "\"body\":{\"boot_id\":\"a1b2c3d4\",\"fw\":\"" FW_VERSION "\",\"reset_reason\":\"power\"}}");
}

static void test_heartbeat_cadence(void)
{
    setup(1);
    fake_hal_clear(&fake);
    fw_body_tick(&body, 1000);
    CHECK(fake.out_count == 1 && out_has(0, "\"type\":\"body.heartbeat\""));
    CHECK(out_has(0, "\"body\":{\"batt_pct\":71,\"link_rssi\":-58,\"frame_errors\":0}"));
    fw_body_tick(&body, 1999);
    CHECK(fake.out_count == 1);
    fw_body_tick(&body, 2000);
    CHECK(fake.out_count == 2 && out_has(1, "\"ts\":1000"));
}

static void test_heartbeat_skipped_without_link_and_resumes(void)
{
    setup(1);
    fw_body_set_link(&body, 0);
    fake_hal_clear(&fake);
    fw_body_tick(&body, 5000);
    CHECK(fake.out_count == 0 && body.outbox_count == 0); /* heartbeat nie jest buforowany */
    fw_body_set_link(&body, 1);
    fw_body_tick(&body, 5001);
    CHECK(fake.out_count == 1 && out_has(0, "body.heartbeat"));
}

static void test_events_are_buffered_in_order_and_flushed(void)
{
    setup(1);
    fw_body_set_link(&body, 0);
    fake_hal_clear(&fake);
    fw_body_button(&body, "b1", 0, 1100);
    fw_body_local_command(&body, "volume_up", "ok", 1200);
    CHECK(fake.out_count == 0 && body.outbox_count == 2);
    fw_body_set_link(&body, 1);
    CHECK(fake.out_count == 2 && out_has(0, "\"type\":\"button\"") && out_has(1, "\"type\":\"local.command\""));
    CHECK(out_has(0, "\"ts\":100") && out_has(1, "\"ts\":200")); /* czas powstania, nie wysłania */
}

static void test_outbox_drops_oldest_when_full(void)
{
    unsigned i;
    setup(1);
    fw_body_set_link(&body, 0);
    fake_hal_clear(&fake);
    for (i = 0; i < FW_OUTBOX_LEN + 3; i++) {
        char cmd[16];
        snprintf(cmd, sizeof(cmd), "c%u", i);
        fw_body_local_command(&body, cmd, "ok", 1000 + i);
    }
    CHECK(body.outbox_count == FW_OUTBOX_LEN && body.dropped_events == 3);
    fw_body_set_link(&body, 1);
    CHECK(fake.out_count == (int)FW_OUTBOX_LEN && out_has(0, "\"cmd\":\"c3\""));
}

static void test_send_failure_marks_link_down_and_buffers(void)
{
    setup(1);
    fake_hal_clear(&fake);
    fake.link_ok = 0;
    fw_body_button(&body, "b1", 1, 1500);
    CHECK(!body.link_up && body.outbox_count == 1);
    fake.link_ok = 1;
    fw_body_set_link(&body, 1);
    CHECK(fake.out_count == 1 && out_has(0, "\"kind\":\"long\""));
}

static void test_flush_stops_at_first_failure_keeping_order(void)
{
    setup(1);
    fw_body_set_link(&body, 0);
    fw_body_button(&body, "b1", 0, 1100);
    fw_body_button(&body, "b2", 0, 1200);
    fake_hal_clear(&fake);
    fake.link_ok = 0;
    fw_body_set_link(&body, 1);
    CHECK(body.outbox_count == 2 && !body.link_up);
    fake.link_ok = 1;
    fw_body_set_link(&body, 1);
    CHECK(fake.out_count == 2 && out_has(0, "\"id\":\"b1\"") && out_has(1, "\"id\":\"b2\""));
}

static void test_oversized_event_is_counted_not_sent(void)
{
    char cmd[300];
    setup(1);
    fake_hal_clear(&fake);
    memset(cmd, 'x', sizeof(cmd) - 1);
    cmd[sizeof(cmd) - 1] = '\0';
    fw_body_local_command(&body, cmd, "ok", 1100);
    CHECK(fake.out_count == 0 && body.dropped_events == 1);
}

/* ---------- ciało: odczyty i usterki ---------- */

static void test_temperature_on_change_and_period(void)
{
    setup(1);
    fake_hal_clear(&fake);
    fw_body_set_temp(&body, 240);
    fw_body_tick(&body, 1000);
    CHECK(fake.out_count == 2 && out_has(1, "\"body\":{\"temp_c\":24}"));
    fw_body_set_temp(&body, 243);
    fw_body_tick(&body, 1500);
    fw_body_set_temp(&body, 246);
    fw_body_tick(&body, 1600);
    CHECK(fake.out_count == 3 && out_has(2, "\"temp_c\":24.6"));
    fw_body_tick(&body, 1600 + FW_SENSOR_PERIOD_MS - 1);
    CHECK(count_type("body.sensor") == 2); /* przed upływem okresu bez nowego odczytu (heartbeaty mogą być) */
    fake_hal_clear(&fake);
    fw_body_tick(&body, 1600 + FW_SENSOR_PERIOD_MS);
    CHECK(out_has(fake.out_count - 1, "\"temp_c\":24.6"));
}

static void test_temp_fault_stops_readings_and_clear_resumes(void)
{
    setup(1);
    fw_body_set_temp(&body, 240);
    fw_body_tick(&body, 1000);
    fake_hal_clear(&fake);
    fw_body_fault(&body, FW_SENSOR_TEMP, "stuck", 1100);
    CHECK(out_has(0, "\"type\":\"body.fault\"") && out_has(0, "\"body\":{\"sensor\":\"temp\",\"code\":\"stuck\"}"));
    fw_body_set_temp(&body, 300);
    fw_body_tick(&body, 1200);
    CHECK(fake.out_count == 1);
    fw_body_fault_clear(&body, FW_SENSOR_TEMP, 1300);
    fw_body_tick(&body, 1300);
    CHECK(out_has(1, "body.fault.clear") && out_has(2, "\"temp_c\":30"));
    fake_hal_clear(&fake);
    fw_body_fault_clear(&body, FW_SENSOR_TEMP, 1400); /* bez usterki: nic */
    CHECK(fake.out_count == 0);
}

static void test_presence_and_gps(void)
{
    setup(1);
    fake_hal_clear(&fake);
    fw_body_set_presence(&body, 1, 1100);
    fw_body_set_presence(&body, 1, 1200); /* bez zmiany */
    fw_body_set_presence(&body, 0, 1300);
    fw_body_gps(&body, 522297000, 210122000, 1400);
    CHECK(fake.out_count == 3);
    CHECK(out_has(0, "\"presence\":true") && out_has(1, "\"presence\":false"));
    CHECK(out_has(2, "\"gps\":{\"lat\":52.2297,\"lon\":21.0122}"));
    fw_body_fault(&body, FW_SENSOR_GPS, "no_fix", 1500);
    fw_body_gps(&body, 1, 1, 1600);
    CHECK(fake.out_count == 4);
}

/* ---------- ciało: akcje i kody błędów ---------- */

static void test_ping_ack_echoes_type_and_ref(void)
{
    setup(1);
    fake_hal_clear(&fake);
    action("{\"v\":1,\"t\":\"action\",\"id\":\"h7\",\"ts\":5,\"type\":\"ping\",\"body\":{}}");
    CHECK_STR(last_ack(), "{\"v\":1,\"t\":\"ack\",\"id\":\"a1b2.2\",\"ts\":1000,\"type\":\"ping\",\"body\":{\"ref\":\"h7\",\"ok\":true}}");
}

static void test_body_may_be_absent(void)
{
    setup(1);
    action("{\"v\":1,\"t\":\"action\",\"id\":\"h1\",\"type\":\"ping\"}");
    CHECK(strstr(last_ack(), "\"ok\":true") != NULL);
}

struct case_ {
    const char *type;
    const char *body;
    const char *err; /* NULL = ok */
    const char *hal;  /* wpis w logu atrapy albo NULL */
};

static void test_action_error_codes_follow_spec(void)
{
    static const struct case_ cases[] = {
        {"haptic", "{\"pattern\":\"warn\"}", NULL, "haptic warn\n"},
        {"haptic", "{\"pattern\":\"loud\"}", "bad_value", NULL},
        {"haptic", "{\"pattern\":1}", "bad_body", NULL},
        {"haptic", "{}", "bad_body", NULL},
        {"gpio.set", "{\"pin\":5,\"value\":1}", NULL, "gpio 5 1\n"},
        {"gpio.set", "{\"pin\":9,\"value\":1}", "pin_not_allowed", NULL},
        {"gpio.set", "{\"pin\":5,\"value\":2}", "bad_value", NULL},
        {"gpio.set", "{\"pin\":\"5\",\"value\":1}", "bad_body", NULL},
        {"gpio.set", "{\"pin\":true,\"value\":1}", "bad_body", NULL},
        {"gpio.set", "{\"pin\":5.0,\"value\":1}", "bad_body", NULL},
        {"speak.clip", "{\"clip\":\"ready\"}", NULL, "clip ready\n"},
        {"speak.clip", "{\"clip\":\"nope\"}", "unknown_clip", NULL},
        {"speak.clip", "{\"clip\":3}", "bad_body", NULL},
        {"config.set", "{\"key\":\"volume\",\"value\":80}", NULL, "config volume 80000\n"},
        {"config.set", "{\"key\":\"temp_warn_c\",\"value\":38.5}", NULL, "config temp_warn_c 38500\n"},
        {"config.set", "{\"key\":\"volume\",\"value\":101}", "bad_value", NULL},
        {"config.set", "{\"key\":\"volume\",\"value\":\"x\"}", "bad_body", NULL},
        {"config.set", "{\"key\":\"color\",\"value\":1}", "unknown_key", NULL},
        {"config.set", "{\"key\":1,\"value\":1}", "bad_body", NULL},
        {"speak", "{\"audio_id\":9}", "no_audio", NULL},
        {"speak", "{}", "bad_body", NULL},
        {"speak", "{\"audio_id\":true}", "bad_body", NULL},
        {"self.destruct", "{}", "unknown_action", NULL},
    };
    size_t i;
    for (i = 0; i < sizeof(cases) / sizeof(cases[0]); i++) {
        char frame[256];
        char expect[64];
        const char *ack;
        setup(1);
        fake_hal_clear(&fake);
        snprintf(frame, sizeof(frame), "{\"v\":1,\"t\":\"action\",\"id\":\"h1\",\"ts\":0,\"type\":\"%s\",\"body\":%s}",
                 cases[i].type, cases[i].body);
        action(frame);
        ack = last_ack();
        if (cases[i].err) {
            snprintf(expect, sizeof(expect), "\"ok\":false,\"err\":\"%s\"}", cases[i].err);
        } else {
            snprintf(expect, sizeof(expect), "\"ok\":true}");
        }
        if (!strstr(ack, expect)) {
            fprintf(stderr, "przypadek %zu (%s %s): %s\n", i, cases[i].type, cases[i].body, ack);
            CHECK(0);
        }
        CHECK(cases[i].hal ? strcmp(fake.log, cases[i].hal) == 0 : fake.log_len == 0);
    }
}

static void test_invalid_frames_count_as_frame_errors_without_ack(void)
{
    static const char *bad[] = {
        "nie json",
        "[1,2]",
        "{\"v\":2,\"t\":\"action\",\"id\":\"h\",\"type\":\"ping\"}",
        "{\"v\":1,\"t\":\"event\",\"id\":\"h\",\"type\":\"ping\"}",
        "{\"v\":1,\"t\":\"action\",\"type\":\"ping\"}",
        "{\"v\":1,\"t\":\"action\",\"id\":7,\"type\":\"ping\"}",
        "{\"v\":1,\"t\":\"action\",\"id\":\"h\",\"type\":\"ping\",\"body\":[1]}",
        "{\"v\":\"1\",\"t\":\"action\",\"id\":\"h\",\"type\":\"ping\"}",
    };
    size_t i;
    setup(1);
    fake_hal_clear(&fake);
    for (i = 0; i < sizeof(bad) / sizeof(bad[0]); i++) {
        action(bad[i]);
    }
    CHECK(fake.out_count == 0 && body.frame_errors == sizeof(bad) / sizeof(bad[0]));
    fw_body_tick(&body, 3000);
    CHECK(out_has(0, "\"frame_errors\":8"));
}

static void test_oversized_frame_is_rejected(void)
{
    char frame[FW_RX_TEXT_MAX + 64];
    int n;
    setup(1);
    fake_hal_clear(&fake);
    n = snprintf(frame, sizeof(frame), "{\"v\":1,\"t\":\"action\",\"id\":\"h\",\"type\":\"ping\",\"body\":{\"pad\":\"");
    memset(frame + n, 'x', FW_RX_TEXT_MAX);
    strcpy(frame + FW_RX_TEXT_MAX - 2, "\"}}");
    fw_body_on_text(&body, frame, strlen(frame), 2000);
    CHECK(fake.out_count == 0 && body.frame_errors == 1);
    fw_body_frame_error(&body); /* wiadomość odrzucona już przez fw_ws */
    fw_body_tick(&body, 2000);
    CHECK(fake.out_count == 1 && out_has(0, "\"frame_errors\":2"));
}

/* ---------- ciało: odtwarzanie ---------- */

static void playback_frame(uint16_t id, uint16_t seq, const char *data)
{
    uint8_t f[64];
    size_t len = strlen(data);
    f[0] = 2;
    f[1] = (uint8_t)(id >> 8);
    f[2] = (uint8_t)id;
    f[3] = (uint8_t)(seq >> 8);
    f[4] = (uint8_t)seq;
    memcpy(f + 5, data, len);
    fw_body_on_binary(&body, f, 5 + len);
}

static void speak(int id)
{
    char frame[128];
    snprintf(frame, sizeof(frame), "{\"v\":1,\"t\":\"action\",\"id\":\"s%d\",\"type\":\"speak\",\"body\":{\"audio_id\":%d}}", id, id);
    action(frame);
}

static void test_playback_after_frames_and_speak(void)
{
    setup(1);
    fake_hal_clear(&fake);
    playback_frame(3, 0, "abcd");
    playback_frame(3, 2, "ef");     /* seq 1 brakuje */
    playback_frame(3, 0, "XXXX");   /* powtórzony seq */
    speak(3);
    CHECK(strstr(last_ack(), "\"ok\":true") != NULL);
    CHECK_STR(fake.log, "play 3 3 2 6\n");
    CHECK(memcmp(body.pb_pool + body.pb_offset[0], "abcd", 4) == 0);
}

static void test_playback_single_slot_rules(void)
{
    setup(1);
    playback_frame(4, 0, "old");
    playback_frame(5, 0, "new");  /* nowe audio_id zastępuje niezapowiedziane ramki */
    speak(4);
    CHECK(strstr(last_ack(), "no_audio") != NULL);
    speak(5);
    CHECK(strstr(last_ack(), "\"ok\":true") != NULL);
    playback_frame(6, 0, "next"); /* trwa odtwarzanie 5 */
    CHECK(body.busy_drops == 1);
    speak(6);
    CHECK(strstr(last_ack(), "busy") != NULL);
    speak(5);
    CHECK(strstr(last_ack(), "busy") != NULL);
    fw_body_playback_done(&body);
    playback_frame(6, 0, "next");
    speak(6);
    CHECK(strstr(last_ack(), "\"ok\":true") != NULL);
}

static void test_playback_limits(void)
{
    static uint8_t big[5 + 1200];
    unsigned seq;
    setup(1);
    playback_frame(7, FW_PLAYBACK_MAX_PACKETS, "x"); /* seq poza tablicą */
    CHECK(body.pb_truncated && body.pb_used == 0);
    big[0] = 2;
    big[2] = 8;
    for (seq = 0; seq < 60; seq++) {
        big[3] = (uint8_t)(seq >> 8);
        big[4] = (uint8_t)seq;
        fw_body_on_binary(&body, big, sizeof(big));
    }
    CHECK(body.pb_truncated && body.pb_used <= FW_PLAYBACK_BYTES && body.pb_used > FW_PLAYBACK_BYTES - 1200);
}

static void test_bad_playback_frames(void)
{
    static const uint8_t short_frame[3] = {2, 0, 1};
    static const uint8_t mic_frame[6] = {1, 0, 1, 0, 0, 9};
    static const uint8_t zero_id[6] = {2, 0, 0, 0, 0, 9};
    setup(1);
    fw_body_on_binary(&body, short_frame, sizeof(short_frame));
    fw_body_on_binary(&body, mic_frame, sizeof(mic_frame));
    fw_body_on_binary(&body, zero_id, sizeof(zero_id));
    CHECK(body.frame_errors == 3 && body.pb_used == 0);
}

/* ---------- ciało: mikrofon ---------- */

static void test_utterance_stream(void)
{
    static const uint8_t pkt[3] = {0xAA, 0xBB, 0xCC};
    uint32_t utt;
    setup(1);
    fake_hal_clear(&fake);
    utt = fw_body_utterance_start(&body, 1100);
    fw_body_mic_packet(&body, pkt, sizeof(pkt));
    fw_body_mic_packet(&body, pkt, sizeof(pkt));
    fw_body_utterance_end(&body, 40, 1150);
    CHECK(utt == 1 && fake.out_count == 4);
    CHECK(out_has(0, "\"type\":\"audio.utterance.start\"") && out_has(0, "\"body\":{\"utt_id\":1}"));
    CHECK(fake.out[1].binary && fake.out[1].len == 8);
    CHECK(memcmp(fake.out[1].data, "\x01\x00\x01\x00\x00\xAA\xBB\xCC", 8) == 0);
    CHECK(memcmp(fake.out[2].data, "\x01\x00\x01\x00\x01", 5) == 0);
    CHECK(out_has(3, "\"body\":{\"utt_id\":1,\"dur_ms\":40}"));
    fw_body_mic_packet(&body, pkt, sizeof(pkt)); /* po końcu: ignorowane */
    CHECK(fake.out_count == 4);
}

static void test_mic_fault_blocks_utterances_and_link_drop_loses_audio(void)
{
    static const uint8_t pkt[1] = {1};
    setup(1);
    fw_body_fault(&body, FW_SENSOR_MIC, "dead", 1100);
    CHECK(fw_body_utterance_start(&body, 1200) == 0);
    fw_body_fault_clear(&body, FW_SENSOR_MIC, 1300);
    fw_body_set_link(&body, 0);
    fake_hal_clear(&fake);
    CHECK(fw_body_utterance_start(&body, 1400) == 1);
    fw_body_mic_packet(&body, pkt, sizeof(pkt));
    fw_body_utterance_end(&body, 20, 1420);
    CHECK(fake.out_count == 0 && body.outbox_count == 2); /* start i end czekają, audio przepadło */
}

static void test_ram_budget(void)
{
    printf("sizeof(fw_body_t) = %zu B\n", sizeof(fw_body_t));
    CHECK(sizeof(fw_body_t) <= 72000u); /* zmiana rozmiaru buforów wymaga poprawki budżetu w fw_config.h */
}

int main(void)
{
    test_writer();
    test_writer_fixed_point();
    test_writer_overflow_and_balance();
    test_parser_accepts_valid_json();
    test_parser_rejects_invalid_json();
    test_parser_token_limit();
    test_parser_duplicate_key_takes_last();
    test_escaped_keys_match();
    test_string_decoding_limits();
    test_numbers();
    test_boot_waits_in_outbox_until_link();
    test_heartbeat_cadence();
    test_heartbeat_skipped_without_link_and_resumes();
    test_events_are_buffered_in_order_and_flushed();
    test_outbox_drops_oldest_when_full();
    test_send_failure_marks_link_down_and_buffers();
    test_flush_stops_at_first_failure_keeping_order();
    test_oversized_event_is_counted_not_sent();
    test_temperature_on_change_and_period();
    test_temp_fault_stops_readings_and_clear_resumes();
    test_presence_and_gps();
    test_ping_ack_echoes_type_and_ref();
    test_body_may_be_absent();
    test_action_error_codes_follow_spec();
    test_invalid_frames_count_as_frame_errors_without_ack();
    test_oversized_frame_is_rejected();
    test_playback_after_frames_and_speak();
    test_playback_single_slot_rules();
    test_playback_limits();
    test_bad_playback_frames();
    test_utterance_stream();
    test_mic_fault_blocks_utterances_and_link_drop_loses_audio();
    test_ram_budget();
    printf("%d sprawdzeń, %d błędów\n", checks, failures);
    return failures ? 1 : 0;
}
