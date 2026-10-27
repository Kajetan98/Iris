/* Ciało HERMES: zdarzenia do hosta, akcje od hosta, bufory (docs/spec.md, sekcje 3, 5, 6, 11).
 *
 * Cały stan siedzi w fw_body_t (bez malloc). Funkcje woła jedna pętla główna; przerwania
 * powinny tylko ustawiać flagi albo kolejkować dane dla tej pętli.
 */
#ifndef FW_BODY_H
#define FW_BODY_H

#include <stddef.h>
#include <stdint.h>

#include "fw_config.h"
#include "fw_hal.h"
#include "fw_json.h"

typedef enum {
    FW_SENSOR_MIC,
    FW_SENSOR_IMU,
    FW_SENSOR_TEMP,
    FW_SENSOR_PRESENCE,
    FW_SENSOR_GPS,
    FW_SENSOR_COUNT
} fw_sensor_t;

typedef struct {
    uint8_t gpio_pins[FW_GPIO_MAX]; /* biała lista dla gpio.set */
    uint8_t gpio_count;
    const char *clips[FW_CLIPS_MAX]; /* klipy zapisane w ciele (speak.clip) */
    uint8_t clip_count;
} fw_settings_t;

typedef struct {
    uint16_t len;
    char data[FW_TX_TEXT_MAX];
} fw_slot_t;

typedef struct {
    fw_hal_t hal;
    fw_settings_t settings;

    uint32_t boot_ms;
    char boot_id[9];
    uint32_t next_id;
    int link_up;
    uint32_t next_heartbeat_ms;

    int32_t batt_x10;
    int16_t rssi;
    int32_t temp_x10;
    int temp_valid;
    int32_t sent_temp_x10;
    int temp_sent;
    uint32_t last_sensor_ms;
    int presence;
    int presence_known;
    uint8_t faults; /* bit = fw_sensor_t */

    int32_t volume_milli;
    int32_t batt_warn_milli;
    int32_t temp_warn_milli;

    uint32_t frame_errors;   /* ramki od hosta niezgodne ze spec (heartbeat.frame_errors) */
    uint32_t dropped_events; /* zdarzenia utracone przy pełnym buforze */
    uint32_t busy_drops;     /* ramki odtwarzania odrzucone, bo coś grało */

    uint32_t utt_id;
    uint16_t mic_seq;
    int utt_active;

    fw_slot_t outbox[FW_OUTBOX_LEN];
    uint16_t outbox_head;
    uint16_t outbox_count;

    uint8_t pb_pool[FW_PLAYBACK_BYTES];
    uint16_t pb_offset[FW_PLAYBACK_MAX_PACKETS];
    uint16_t pb_length[FW_PLAYBACK_MAX_PACKETS];
    uint32_t pb_used;
    uint16_t pb_packets;
    uint16_t pb_id; /* 0 = pusty */
    int pb_playing;
    int pb_truncated;

    char tx[FW_TX_TEXT_MAX];
    uint8_t txbin[5 + 1275]; /* nagłówek + największy pakiet Opus */
    fw_jtok_t tok[FW_JSON_MAX_TOKENS];
} fw_body_t;

void fw_body_init(fw_body_t *b, const fw_hal_t *hal, const fw_settings_t *settings,
                  uint32_t now_ms, uint32_t random32, const char *reset_reason);
void fw_body_tick(fw_body_t *b, uint32_t now_ms);
/* Port zgłasza zestawienie (1) i zerwanie (0) połączenia; po zestawieniu idą zaległe zdarzenia. */
void fw_body_set_link(fw_body_t *b, int up);

void fw_body_on_text(fw_body_t *b, const char *frame, size_t len, uint32_t now_ms);
void fw_body_on_binary(fw_body_t *b, const uint8_t *frame, size_t len);
void fw_body_playback_done(fw_body_t *b);
/* Port zgłasza wiadomość od hosta odrzuconą przed rdzeniem (za duża dla bufora łącza). */
void fw_body_frame_error(fw_body_t *b);

void fw_body_set_battery(fw_body_t *b, int32_t pct_x10);
void fw_body_set_rssi(fw_body_t *b, int16_t rssi);
void fw_body_set_temp(fw_body_t *b, int32_t temp_x10);
void fw_body_set_presence(fw_body_t *b, int present, uint32_t now_ms);
void fw_body_gps(fw_body_t *b, int32_t lat_e7, int32_t lon_e7, uint32_t now_ms);
void fw_body_fault(fw_body_t *b, fw_sensor_t sensor, const char *code, uint32_t now_ms);
void fw_body_fault_clear(fw_body_t *b, fw_sensor_t sensor, uint32_t now_ms);
void fw_body_button(fw_body_t *b, const char *button_id, int long_press, uint32_t now_ms);
void fw_body_local_command(fw_body_t *b, const char *cmd, const char *result, uint32_t now_ms);

/* Wypowiedź: zwraca utt_id albo 0, gdy mikrofon ma usterkę. Pakiety Opus koduje port. */
uint32_t fw_body_utterance_start(fw_body_t *b, uint32_t now_ms);
void fw_body_mic_packet(fw_body_t *b, const uint8_t *opus, size_t len);
void fw_body_utterance_end(fw_body_t *b, uint32_t dur_ms, uint32_t now_ms);

const char *fw_sensor_name(fw_sensor_t sensor);

#endif
