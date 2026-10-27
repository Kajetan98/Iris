/* Atrapa HAL dla testów C i biblioteki do testów zgodności (ctypes). */
#ifndef FAKE_HAL_H
#define FAKE_HAL_H

#include "../core/fw_hal.h"

#define FAKE_MAX_OUT 512
#define FAKE_FRAME_MAX 1400

typedef struct {
    int binary;
    size_t len;
    uint8_t data[FAKE_FRAME_MAX];
} fake_frame_t;

typedef struct {
    fake_frame_t out[FAKE_MAX_OUT];
    int out_count;
    int out_lost;    /* ramki ponad FAKE_MAX_OUT */
    int link_ok;     /* 0: send_* zwraca błąd jak zerwane łącze */
    char log[8192];  /* wywołania sprzętu: "gpio 5 1\n", "play 3 50 2400\n" ... */
    size_t log_len;
} fake_hal_t;

void fake_hal_init(fake_hal_t *f, fw_hal_t *hal);
void fake_hal_clear(fake_hal_t *f);

#endif
