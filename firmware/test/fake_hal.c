#include "fake_hal.h"

#include <stdio.h>
#include <string.h>

static void logf_(fake_hal_t *f, const char *line)
{
    size_t n = strlen(line);
    if (f->log_len + n + 1 < sizeof(f->log)) {
        memcpy(f->log + f->log_len, line, n);
        f->log_len += n;
        f->log[f->log_len] = '\0';
    }
}

static int store(fake_hal_t *f, int binary, const void *data, size_t len)
{
    fake_frame_t *fr;
    if (!f->link_ok) {
        return -1;
    }
    if (f->out_count >= FAKE_MAX_OUT || len > FAKE_FRAME_MAX) {
        f->out_lost++;
        return 0;
    }
    fr = &f->out[f->out_count++];
    fr->binary = binary;
    fr->len = len;
    memcpy(fr->data, data, len);
    return 0;
}

static int send_text(void *ctx, const char *frame, size_t len)
{
    return store(ctx, 0, frame, len);
}

static int send_binary(void *ctx, const uint8_t *frame, size_t len)
{
    return store(ctx, 1, frame, len);
}

static void gpio_write(void *ctx, uint8_t pin, uint8_t value)
{
    char line[32];
    snprintf(line, sizeof(line), "gpio %u %u\n", pin, value);
    logf_(ctx, line);
}

static void haptic(void *ctx, const char *pattern)
{
    char line[48];
    snprintf(line, sizeof(line), "haptic %s\n", pattern);
    logf_(ctx, line);
}

static void play_clip(void *ctx, const char *clip)
{
    char line[80];
    snprintf(line, sizeof(line), "clip %s\n", clip);
    logf_(ctx, line);
}

static void play_audio(void *ctx, const fw_playback_t *pb)
{
    char line[64];
    unsigned long bytes = 0;
    unsigned present = 0;
    uint16_t i;
    for (i = 0; i < pb->packets; i++) {
        if (pb->length[i]) {
            present++;
            bytes += pb->length[i];
        }
    }
    snprintf(line, sizeof(line), "play %u %u %u %lu\n", pb->audio_id, pb->packets, present, bytes);
    logf_(ctx, line);
}

static void config_changed(void *ctx, const char *key, int32_t value_milli)
{
    char line[64];
    snprintf(line, sizeof(line), "config %s %ld\n", key, (long)value_milli);
    logf_(ctx, line);
}

void fake_hal_clear(fake_hal_t *f)
{
    f->out_count = 0;
    f->out_lost = 0;
    f->log_len = 0;
    f->log[0] = '\0';
}

void fake_hal_init(fake_hal_t *f, fw_hal_t *hal)
{
    memset(f, 0, sizeof(*f));
    f->link_ok = 1;
    hal->ctx = f;
    hal->send_text = send_text;
    hal->send_binary = send_binary;
    hal->gpio_write = gpio_write;
    hal->haptic = haptic;
    hal->play_clip = play_clip;
    hal->play_audio = play_audio;
    hal->config_changed = config_changed;
}
