/* Minimalny JSON: zapis do bufora o stałym rozmiarze i ograniczony odczyt ramek akcji.
 * Bez malloc, bez liczb zmiennoprzecinkowych. */
#ifndef FW_JSON_H
#define FW_JSON_H

#include <stddef.h>
#include <stdint.h>

/* --- zapis --- */

typedef struct {
    char *buf;
    size_t cap;
    size_t len;
    int overflow;
    int depth;
    uint8_t first[8]; /* czy następny element na danym poziomie jest pierwszy */
    uint8_t arr[8];   /* poziom jest tablicą: przecinki przed wartościami, nie przed kluczami */
} fw_jw_t;

void fw_jw_init(fw_jw_t *w, char *buf, size_t cap);
void fw_jw_open(fw_jw_t *w);   /* { */
void fw_jw_close(fw_jw_t *w);  /* } */
void fw_jw_arr_open(fw_jw_t *w);   /* [ */
void fw_jw_arr_close(fw_jw_t *w);  /* ] */
void fw_jw_key(fw_jw_t *w, const char *key);
void fw_jw_str(fw_jw_t *w, const char *s);
void fw_jw_int(fw_jw_t *w, int32_t v);
void fw_jw_uint(fw_jw_t *w, uint32_t v);
void fw_jw_bool(fw_jw_t *w, int v);
/* Liczba stałoprzecinkowa: value / 10^decimals, bez zbędnych zer (71 zamiast 71.0). */
void fw_jw_fixed(fw_jw_t *w, int32_t value, unsigned decimals);
/* Zakańcza znakiem NUL; zwraca długość albo 0 przy przepełnieniu. */
size_t fw_jw_finish(fw_jw_t *w);

/* --- odczyt --- */

enum { FW_JT_OBJ = 1, FW_JT_ARR, FW_JT_STR, FW_JT_PRIM };

typedef struct {
    uint8_t type;
    uint16_t start;  /* dla napisu: bez cudzysłowów */
    uint16_t end;
    int16_t parent;  /* klucz: indeks obiektu; wartość: indeks klucza; element tablicy: tablica */
} fw_jtok_t;

/* Liczba tokenów albo -1 (składnia, zbyt wiele tokenów, zbyt głęboko, za długie wejście). */
int fw_json_parse(const char *js, size_t len, fw_jtok_t *tok, int max_tok);
/* Indeks tokenu wartości pod kluczem w obiekcie obj albo -1. */
int fw_json_find(const char *js, const fw_jtok_t *tok, int ntok, int obj, const char *key);
/* Napis po rozwinięciu escape'ów (UTF-8). Długość albo -1 (nie napis, za długi, zły escape). */
int fw_json_str(const char *js, const fw_jtok_t *t, char *out, size_t cap);
/* Liczba całkowita int32 (bez części ułamkowej i wykładnika). 0 = ok, -1 = nie ta wartość. */
int fw_json_int(const char *js, const fw_jtok_t *t, int32_t *out);
/* Liczba w tysięcznych (do 3 miejsc po przecinku, bez wykładnika). 0 = ok, -1 = nie ta wartość. */
int fw_json_milli(const char *js, const fw_jtok_t *t, int32_t *out);
int fw_json_is_bool(const char *js, const fw_jtok_t *t);
int fw_json_is_number(const char *js, const fw_jtok_t *t);

#endif
