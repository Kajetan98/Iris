/* Płaskie API rdzenia dla testów zgodności z hostem (pytest + ctypes). Nie trafia na STM32. */
#include <string.h>

#include "../core/fw_body.h"
#include "../test/fake_hal.h"

static fw_body_t body;
static fake_hal_t fake;
static fw_hal_t hal;
static fw_settings_t settings;

void ffi_init(uint32_t now_ms, uint32_t random32, const char *reset_reason, const uint8_t *pins, int npins);
void ffi_init(uint32_t now_ms, uint32_t random32, const char *reset_reason, const uint8_t *pins, int npins)
{
    static const char *const clips[] = {"ready", "error", "low_battery", "link_lost"};
    int i;
    fake_hal_init(&fake, &hal);
    memset(&settings, 0, sizeof(settings));
    for (i = 0; i < npins && i < (int)FW_GPIO_MAX; i++) {
        settings.gpio_pins[i] = pins[i];
    }
    settings.gpio_count = (uint8_t)i;
    for (i = 0; i < 4; i++) {
        settings.clips[i] = clips[i];
    }
    settings.clip_count = 4;
    fw_body_init(&body, &hal, &settings, now_ms, random32, reset_reason);
}

void ffi_link(int up);
void ffi_link(int up) { fw_body_set_link(&body, up); }
void ffi_transport_ok(int ok);
void ffi_transport_ok(int ok) { fake.link_ok = ok; }
void ffi_tick(uint32_t now_ms);
void ffi_tick(uint32_t now_ms) { fw_body_tick(&body, now_ms); }
void ffi_on_text(const char *frame, size_t len, uint32_t now_ms);
void ffi_on_text(const char *frame, size_t len, uint32_t now_ms) { fw_body_on_text(&body, frame, len, now_ms); }
void ffi_on_binary(const uint8_t *frame, size_t len);
void ffi_on_binary(const uint8_t *frame, size_t len) { fw_body_on_binary(&body, frame, len); }
void ffi_playback_done(void);
void ffi_playback_done(void) { fw_body_playback_done(&body); }

void ffi_set_battery(int32_t pct_x10);
void ffi_set_battery(int32_t pct_x10) { fw_body_set_battery(&body, pct_x10); }
void ffi_set_rssi(int16_t rssi);
void ffi_set_rssi(int16_t rssi) { fw_body_set_rssi(&body, rssi); }
void ffi_set_temp(int32_t temp_x10);
void ffi_set_temp(int32_t temp_x10) { fw_body_set_temp(&body, temp_x10); }
void ffi_set_presence(int present, uint32_t now_ms);
void ffi_set_presence(int present, uint32_t now_ms) { fw_body_set_presence(&body, present, now_ms); }
void ffi_gps(int32_t lat_e7, int32_t lon_e7, uint32_t now_ms);
void ffi_gps(int32_t lat_e7, int32_t lon_e7, uint32_t now_ms) { fw_body_gps(&body, lat_e7, lon_e7, now_ms); }
void ffi_fault(int sensor, const char *code, uint32_t now_ms);
void ffi_fault(int sensor, const char *code, uint32_t now_ms) { fw_body_fault(&body, (fw_sensor_t)sensor, code, now_ms); }
void ffi_fault_clear(int sensor, uint32_t now_ms);
void ffi_fault_clear(int sensor, uint32_t now_ms) { fw_body_fault_clear(&body, (fw_sensor_t)sensor, now_ms); }
void ffi_button(const char *button_id, int long_press, uint32_t now_ms);
void ffi_button(const char *button_id, int long_press, uint32_t now_ms) { fw_body_button(&body, button_id, long_press, now_ms); }
void ffi_local_command(const char *cmd, const char *result, uint32_t now_ms);
void ffi_local_command(const char *cmd, const char *result, uint32_t now_ms) { fw_body_local_command(&body, cmd, result, now_ms); }
void ffi_request_active(uint32_t now_ms);
void ffi_request_active(uint32_t now_ms) { fw_body_request_active(&body, now_ms); }
uint32_t ffi_utterance_start(uint32_t now_ms);
uint32_t ffi_utterance_start(uint32_t now_ms) { return fw_body_utterance_start(&body, now_ms); }
void ffi_mic_packet(const uint8_t *opus, size_t len);
void ffi_mic_packet(const uint8_t *opus, size_t len) { fw_body_mic_packet(&body, opus, len); }
void ffi_utterance_end(uint32_t dur_ms, uint32_t now_ms);
void ffi_utterance_end(uint32_t dur_ms, uint32_t now_ms) { fw_body_utterance_end(&body, dur_ms, now_ms); }

/* --- wyjście --- */

int ffi_out_count(void);
int ffi_out_count(void) { return fake.out_count; }
int ffi_out_binary(int i);
int ffi_out_binary(int i) { return fake.out[i].binary; }
size_t ffi_out_len(int i);
size_t ffi_out_len(int i) { return fake.out[i].len; }
const uint8_t *ffi_out_data(int i);
const uint8_t *ffi_out_data(int i) { return fake.out[i].data; }
void ffi_out_clear(void);
void ffi_out_clear(void) { fake_hal_clear(&fake); }
const char *ffi_hal_log(void);
const char *ffi_hal_log(void) { return fake.log; }

uint32_t ffi_counter(const char *name);
uint32_t ffi_counter(const char *name)
{
    if (strcmp(name, "frame_errors") == 0) return body.frame_errors;
    if (strcmp(name, "dropped_events") == 0) return body.dropped_events;
    if (strcmp(name, "busy_drops") == 0) return body.busy_drops;
    if (strcmp(name, "outbox") == 0) return body.outbox_count;
    if (strcmp(name, "link_up") == 0) return (uint32_t)body.link_up;
    if (strcmp(name, "pb_packets") == 0) return body.pb_packets;
    if (strcmp(name, "pb_used") == 0) return body.pb_used;
    if (strcmp(name, "active") == 0) return (uint32_t)body.active;
    return 0xFFFFFFFFu;
}

int32_t ffi_setting(const char *key);
int32_t ffi_setting(const char *key)
{
    if (strcmp(key, "volume") == 0) return body.volume_milli;
    if (strcmp(key, "batt_warn_pct") == 0) return body.batt_warn_milli;
    if (strcmp(key, "temp_warn_c") == 0) return body.temp_warn_milli;
    return INT32_MIN;
}

/* Pakiet z bufora odtwarzania (do porównania z tym, co wysłał host). */
size_t ffi_playback_packet(uint16_t seq, const uint8_t **data);
size_t ffi_playback_packet(uint16_t seq, const uint8_t **data)
{
    if (seq >= body.pb_packets) {
        return 0;
    }
    *data = body.pb_pool + body.pb_offset[seq];
    return body.pb_length[seq];
}

size_t ffi_sizeof_body(void);
size_t ffi_sizeof_body(void) { return sizeof(fw_body_t); }
