/* Granica sprzętu: rdzeń woła te funkcje, port (STM32, test, symulacja) je dostarcza. */
#ifndef FW_HAL_H
#define FW_HAL_H

#include <stddef.h>
#include <stdint.h>

typedef struct {
    const uint8_t *pool;      /* dane pakietów Opus */
    const uint16_t *offset;   /* początek pakietu seq w pool */
    const uint16_t *length;   /* 0 = brakujący pakiet (cisza) */
    uint16_t packets;         /* liczba pozycji seq */
    uint16_t audio_id;
} fw_playback_t;

typedef struct {
    void *ctx;
    /* 0 = wysłano, inna wartość = brak łącza */
    int (*send_text)(void *ctx, const char *frame, size_t len);
    int (*send_binary)(void *ctx, const uint8_t *frame, size_t len);
    void (*gpio_write)(void *ctx, uint8_t pin, uint8_t value);
    void (*haptic)(void *ctx, const char *pattern);
    void (*play_clip)(void *ctx, const char *clip);
    /* Odtwarzanie asynchroniczne; po zakończeniu port woła fw_body_playback_done(). */
    void (*play_audio)(void *ctx, const fw_playback_t *playback);
    void (*config_changed)(void *ctx, const char *key, int32_t value_milli);
    /* Czy urządzenie jest aktywne (spec, sekcja 1). Opcjonalne, może być NULL. */
    void (*active_changed)(void *ctx, int active);
} fw_hal_t;

#endif
