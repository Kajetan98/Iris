/* Port POSIX ciała HERMES: rdzeń fw_body i klient fw_ws na gnieździe TCP.
 *
 *   build/hermes --url ws://127.0.0.1:8000/ws [--device ID] [--audio-out KATALOG]
 *
 * Token hosta (IRIS_TOKEN) czytany ze zmiennej środowiskowej, nie z argumentów, bo te widać w ps.
 * Do sprawdzania firmware z prawdziwym hostem bez sprzętu. Odczyty czujników ustawiają
 * komendy ze stdin (help). Wywołania sprzętu (gpio, haptyka, klipy, odtwarzanie) i stan
 * łącza idą na stdout, po jednym w linii; odpowiedzi na komendy i błędy na stderr.
 */
#define _POSIX_C_SOURCE 200809L

#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <netdb.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#include "../../core/fw_body.h"
#include "../../net/fw_ws.h"

#define FRAME_MS 20u /* pakiet Opus (spec, sekcja 6) */
#define MAX_UTTERANCE_PACKETS 3000u /* 60 s, jak w symulatorze */
#define POLL_MS 20

typedef struct {
    char host[256]; /* dla getaddrinfo, bez nawiasów IPv6 */
    char port[6];
    char host_header[270];
    char path[512];
} url_t;

static fw_body_t body;
static fw_ws_t ws;
static fw_hal_t hal;
static fw_settings_t settings;
static url_t url;
static char protocols[160]; /* "iris, token.<IRIS_TOKEN>" albo pusty bez tokenu */
static const char *url_text = "ws://localhost:8000/ws";

static int sock = -1;
static struct addrinfo *addrs;
static struct addrinfo *addr_cur;
static int connecting; /* connect() w toku */
static int ws_up;      /* fw_ws_start() się udał, on_close jeszcze nie przyszło */
static uint32_t connect_ms;
static uint32_t retry_at;
static uint32_t backoff = FW_RECONNECT_MIN_MS;
static int offline; /* komenda drop */
static uint32_t offline_until;

static int playing;
static uint16_t play_id;
static uint32_t play_end_ms;
static const char *audio_out;
static unsigned plays;

static FILE *urandom;
static uint32_t lcg;
static int stdin_open = 1;
static volatile sig_atomic_t stop_requested;
static int quitting;
static uint32_t quit_deadline;

static uint32_t now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint32_t)((uint64_t)ts.tv_sec * 1000u + (uint64_t)ts.tv_nsec / 1000000u);
}

static int due(uint32_t now, uint32_t at)
{
    return (int32_t)(now - at) >= 0;
}

static uint32_t random32(void *ctx)
{
    uint32_t v;
    (void)ctx;
    if (urandom && fread(&v, sizeof(v), 1, urandom) == 1) {
        return v;
    }
    lcg = (lcg * 1664525u + 1013904223u) ^ now_ms(); /* bez /dev/urandom maski są przewidywalne */
    return lcg;
}

/* ---------- gniazdo ---------- */

static void close_socket(void)
{
    if (sock >= 0) {
        close(sock);
        sock = -1;
    }
    connecting = 0;
    if (addrs) {
        freeaddrinfo(addrs);
        addrs = NULL;
        addr_cur = NULL;
    }
}

static void schedule_retry(uint32_t now)
{
    retry_at = now + backoff;
    backoff = backoff * 2 > FW_RECONNECT_MAX_MS ? FW_RECONNECT_MAX_MS : backoff * 2;
}

static int sock_write(void *ctx, const uint8_t *data, size_t len)
{
    size_t done = 0;
    (void)ctx;
    while (done < len) {
        ssize_t n = send(sock, data + done, len - done, 0);
        if (n > 0) {
            done += (size_t)n;
            continue;
        }
        if (n < 0 && errno == EINTR) {
            continue;
        }
        if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            struct pollfd p;
            p.fd = sock;
            p.events = POLLOUT;
            p.revents = 0;
            if (poll(&p, 1, 1000) > 0) {
                continue;
            }
        }
        return -1;
    }
    return 0;
}

/* ---------- WebSocket ---------- */

static void on_open(void *ctx)
{
    (void)ctx;
    backoff = FW_RECONNECT_MIN_MS; /* udane połączenie zeruje opóźnienie (spec, sekcja 1) */
    printf("link up\n");
    fw_body_set_link(&body, 1, now_ms());
}

static void on_message(void *ctx, int binary, const uint8_t *data, size_t len)
{
    (void)ctx;
    if (binary) {
        fw_body_on_binary(&body, data, len);
    } else {
        fw_body_on_text(&body, (const char *)data, len, now_ms());
    }
}

static void on_oversized(void *ctx)
{
    (void)ctx;
    fw_body_frame_error(&body);
}

static void on_close(void *ctx, uint16_t code)
{
    (void)ctx;
    printf("link down %u %s\n", code, ws.error ? ws.error : "-");
    fw_body_set_link(&body, 0, now_ms());
    close_socket();
    ws_up = 0;
    schedule_retry(now_ms());
}

static const fw_ws_io_t ws_io = {NULL, sock_write, random32, on_open, on_message, on_oversized, on_close};

static void connected(uint32_t now)
{
    int one = 1;
    (void)setsockopt(sock, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
    freeaddrinfo(addrs);
    addrs = NULL;
    addr_cur = NULL;
    connecting = 0;
    if (fw_ws_start(&ws, &ws_io, url.host_header, url.path, protocols[0] ? protocols : NULL, now) != 0) {
        fprintf(stderr, "handshake: %s\n", ws.error);
        close_socket();
        schedule_retry(now);
        return;
    }
    ws_up = 1;
}

/* Kolejny adres z getaddrinfo (np. ::1, potem 127.0.0.1 dla localhost). */
static void try_next_address(uint32_t now)
{
    while (addr_cur) {
        struct addrinfo *a = addr_cur;
        addr_cur = a->ai_next;
        sock = socket(a->ai_family, a->ai_socktype, a->ai_protocol);
        if (sock < 0) {
            continue;
        }
        (void)fcntl(sock, F_SETFL, fcntl(sock, F_GETFL) | O_NONBLOCK);
        if (connect(sock, a->ai_addr, a->ai_addrlen) == 0) {
            connected(now);
            return;
        }
        if (errno == EINPROGRESS) {
            connecting = 1;
            connect_ms = now;
            return;
        }
        close(sock);
        sock = -1;
    }
    fprintf(stderr, "brak połączenia z %s, ponawiam za %u ms\n", url_text, (unsigned)backoff);
    close_socket();
    schedule_retry(now);
}

static void start_connect(uint32_t now)
{
    struct addrinfo hints;
    int rc;
    memset(&hints, 0, sizeof(hints));
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;
    rc = getaddrinfo(url.host, url.port, &hints, &addrs);
    if (rc != 0) {
        fprintf(stderr, "adres %s: %s\n", url.host, gai_strerror(rc));
        addrs = NULL;
        schedule_retry(now);
        return;
    }
    addr_cur = addrs;
    try_next_address(now);
}

static void connect_progress(uint32_t now)
{
    int err = 0;
    socklen_t len = sizeof(err);
    if (getsockopt(sock, SOL_SOCKET, SO_ERROR, &err, &len) != 0) {
        err = errno;
    }
    if (err == 0) {
        connected(now);
        return;
    }
    close(sock);
    sock = -1;
    connecting = 0;
    try_next_address(now);
}

static void sock_read(uint32_t now)
{
    uint8_t buf[2048];
    ssize_t n = recv(sock, buf, sizeof(buf), 0);
    if (n > 0) {
        fw_ws_feed(&ws, buf, (size_t)n, now);
    } else if (n == 0 || (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR)) {
        fw_ws_transport_lost(&ws);
    }
}

/* ---------- sprzęt ---------- */

static int hal_send_text(void *ctx, const char *frame, size_t len)
{
    (void)ctx;
    return fw_ws_send(&ws, 0, (const uint8_t *)frame, len);
}

static int hal_send_binary(void *ctx, const uint8_t *frame, size_t len)
{
    (void)ctx;
    return fw_ws_send(&ws, 1, frame, len);
}

static void hal_gpio(void *ctx, uint8_t pin, uint8_t value)
{
    (void)ctx;
    printf("gpio %u %u\n", pin, value);
}

static void hal_haptic(void *ctx, const char *pattern)
{
    (void)ctx;
    printf("haptic %s\n", pattern);
}

static void hal_clip(void *ctx, const char *clip)
{
    (void)ctx;
    printf("clip %s\n", clip);
}

static void hal_active(void *ctx, int active)
{
    (void)ctx;
    printf("active %d\n", active);
}

static void hal_config(void *ctx, const char *key, int32_t value_milli)
{
    (void)ctx;
    printf("config %s %ld\n", key, (long)value_milli);
}

/* Pakiety odtwarzania do pliku: dla każdego seq 2 B długości (big-endian, 0 = brak) i dane. */
static void save_playback(const fw_playback_t *pb, char *path, size_t cap)
{
    FILE *f;
    uint16_t i;
    snprintf(path, cap, "%s/play-%u.bin", audio_out, plays);
    f = fopen(path, "wb");
    if (!f) {
        fprintf(stderr, "zapis %s: %s\n", path, strerror(errno));
        path[0] = '\0';
        return;
    }
    for (i = 0; i < pb->packets; i++) {
        uint8_t len[2];
        len[0] = (uint8_t)(pb->length[i] >> 8);
        len[1] = (uint8_t)pb->length[i];
        fwrite(len, 1, 2, f);
        fwrite(pb->pool + pb->offset[i], 1, pb->length[i], f);
    }
    fclose(f);
}

static void hal_play(void *ctx, const fw_playback_t *pb)
{
    char path[600] = "";
    unsigned present = 0;
    unsigned long bytes = 0;
    uint16_t i;
    (void)ctx;
    for (i = 0; i < pb->packets; i++) {
        if (pb->length[i]) {
            present++;
            bytes += pb->length[i];
        }
    }
    plays++;
    if (audio_out) {
        save_playback(pb, path, sizeof(path));
    }
    printf("play %u %u %u %lu%s%s\n", pb->audio_id, pb->packets, present, bytes, path[0] ? " " : "", path);
    playing = 1;
    play_id = pb->audio_id;
    play_end_ms = now_ms() + pb->packets * FRAME_MS;
}

/* ---------- komendy ---------- */

static const char HELP[] =
    "komendy:\n"
    "  batt <0-100>            ustaw baterię\n"
    "  temp <C>                ustaw temperaturę\n"
    "  rssi <dBm>              siła sygnału\n"
    "  fault <sensor> [kod]    usterka sensora (mic imu temp presence gps)\n"
    "  clear <sensor>          koniec usterki\n"
    "  press [long]            przycisk\n"
    "  activate                przełącz IRIS na to urządzenie (device.activate)\n"
    "  sayopus <plik>          wypowiedź z pakietów Opus (2 B długości big-endian + dane)\n"
    "  local <cmd...>          komenda obsłużona lokalnie\n"
    "  presence on|off         obecność\n"
    "  gps <lat> <lon>         pozycja\n"
    "  drop <s>                zerwij połączenie TCP na s sekund\n"
    "  status                  stan łącza i liczniki\n"
    "  quit                    zamknij połączenie i zakończ\n";

static int parse_double(const char *s, double *out)
{
    char *end;
    if (!s) {
        return -1;
    }
    errno = 0;
    *out = strtod(s, &end);
    return (errno || end == s || *end != '\0' || !isfinite(*out)) ? -1 : 0;
}

static int sensor_of(const char *name)
{
    int i;
    for (i = 0; name && i < FW_SENSOR_COUNT; i++) {
        if (strcmp(name, fw_sensor_name((fw_sensor_t)i)) == 0) {
            return i;
        }
    }
    return -1;
}

static void cmd_sayopus(const char *path, uint32_t now)
{
    static uint8_t packet[1275];
    FILE *f = path ? fopen(path, "rb") : NULL;
    unsigned packets = 0;
    uint8_t len[2];
    uint32_t utt;
    if (!f) {
        fprintf(stderr, "błąd: sayopus <plik>%s%s\n", path ? ": " : "", path ? strerror(errno) : "");
        return;
    }
    utt = fw_body_utterance_start(&body, now);
    if (utt == 0) {
        fprintf(stderr, "%s, brak wypowiedzi\n", body.active ? "mikrofon ma usterkę" : "urządzenie nieaktywne");
        fclose(f);
        return;
    }
    while (packets < MAX_UTTERANCE_PACKETS && fread(len, 1, 2, f) == 2) {
        size_t n = (size_t)len[0] << 8 | len[1];
        if (n > sizeof(packet) || fread(packet, 1, n, f) != n) {
            fprintf(stderr, "błąd: uszkodzony plik %s\n", path);
            break;
        }
        fw_body_mic_packet(&body, packet, n);
        packets++;
    }
    fclose(f);
    fw_body_utterance_end(&body, packets * FRAME_MS, now);
    printf("utterance %lu %u\n", (unsigned long)utt, packets);
}

static void command(char *line, uint32_t now)
{
    char rest[512];
    char *name;
    char *a1;
    char *a2;
    double v;
    double w;
    int s;
    snprintf(rest, sizeof(rest), "%s", line);
    name = strtok(line, " \t\r");
    a1 = strtok(NULL, " \t\r");
    a2 = strtok(NULL, " \t\r");
    if (!name) {
        return;
    }
    if (strcmp(name, "help") == 0) {
        fputs(HELP, stderr);
    } else if (strcmp(name, "batt") == 0 && parse_double(a1, &v) == 0 && v >= 0 && v <= 100) {
        fw_body_set_battery(&body, (int32_t)lround(v * 10));
    } else if (strcmp(name, "temp") == 0 && parse_double(a1, &v) == 0 && fabs(v) < 200) {
        fw_body_set_temp(&body, (int32_t)lround(v * 10));
    } else if (strcmp(name, "rssi") == 0 && parse_double(a1, &v) == 0 && v >= -150 && v <= 0) {
        fw_body_set_rssi(&body, (int16_t)lround(v));
    } else if (strcmp(name, "fault") == 0 && (s = sensor_of(a1)) >= 0) {
        fw_body_fault(&body, (fw_sensor_t)s, a2 ? a2 : "fault", now);
    } else if (strcmp(name, "clear") == 0 && (s = sensor_of(a1)) >= 0) {
        fw_body_fault_clear(&body, (fw_sensor_t)s, now);
    } else if (strcmp(name, "activate") == 0) {
        fw_body_request_active(&body, now);
    } else if (strcmp(name, "press") == 0) {
        fw_body_button(&body, "b1", a1 && strcmp(a1, "long") == 0, now);
    } else if (strcmp(name, "sayopus") == 0) {
        cmd_sayopus(a1, now);
    } else if (strcmp(name, "local") == 0 && a1) {
        char *cmd = strstr(rest, "local") + 5; /* reszta linii, jak w symulatorze */
        size_t n;
        cmd += strspn(cmd, " \t");
        n = strcspn(cmd, "\r");
        cmd[n] = '\0';
        fw_body_local_command(&body, cmd, "ok", now);
    } else if (strcmp(name, "presence") == 0 && a1 && (strcmp(a1, "on") == 0 || strcmp(a1, "off") == 0)) {
        fw_body_set_presence(&body, strcmp(a1, "on") == 0, now);
    } else if (strcmp(name, "gps") == 0 && parse_double(a1, &v) == 0 && parse_double(a2, &w) == 0 &&
               fabs(v) <= 90 && fabs(w) <= 180) {
        fw_body_gps(&body, (int32_t)lround(v * 1e7), (int32_t)lround(w * 1e7), now);
    } else if (strcmp(name, "drop") == 0 && parse_double(a1, &v) == 0 && v >= 0 && v <= 3600) {
        offline = 1;
        offline_until = now + (uint32_t)lround(v * 1000);
        if (ws_up) {
            fw_ws_transport_lost(&ws); /* bez ramki close, jak przy utracie zasięgu */
        } else {
            close_socket();
        }
    } else if (strcmp(name, "status") == 0) {
        printf("status link=%d ws=%d active=%d outbox=%u frame_errors=%lu dropped=%lu busy=%lu oversized=%lu\n",
               body.link_up, (int)ws.state, body.active, body.outbox_count, (unsigned long)body.frame_errors,
               (unsigned long)body.dropped_events, (unsigned long)body.busy_drops,
               (unsigned long)ws.oversized);
    } else if (strcmp(name, "quit") == 0) {
        stop_requested = 1;
    } else {
        fprintf(stderr, "błąd: %s (help)\n", name);
    }
}

static void read_stdin(uint32_t now)
{
    static char line[512];
    static size_t used;
    static int overflow;
    char buf[256];
    ssize_t n = read(STDIN_FILENO, buf, sizeof(buf));
    ssize_t i;
    if (n <= 0) {
        if (n == 0 || (errno != EAGAIN && errno != EINTR)) {
            stdin_open = 0; /* koniec wejścia: ciało działa dalej */
        }
        return;
    }
    for (i = 0; i < n; i++) {
        if (buf[i] != '\n') {
            if (used + 1 < sizeof(line)) {
                line[used++] = buf[i];
            } else {
                overflow = 1;
            }
            continue;
        }
        line[used] = '\0';
        if (overflow) {
            fprintf(stderr, "błąd: za długa linia\n");
        } else {
            command(line, now);
        }
        used = 0;
        overflow = 0;
    }
}

/* ---------- start ---------- */

static int parse_url(const char *s, url_t *u)
{
    const char *auth;
    const char *path;
    const char *host;
    const char *host_end;
    const char *port = NULL;
    size_t auth_len;
    size_t host_len;
    if (strncmp(s, "wss://", 6) == 0) {
        fprintf(stderr, "wss:// (TLS) nie jest obsługiwany (docs/spec.md, sekcja 13)\n");
        return -1;
    }
    if (strncmp(s, "ws://", 5) != 0) {
        return -1;
    }
    auth = s + 5;
    path = strchr(auth, '/');
    auth_len = path ? (size_t)(path - auth) : strlen(auth);
    if (auth_len == 0 || memchr(auth, '@', auth_len)) {
        return -1;
    }
    if (auth[0] == '[') {
        host = auth + 1;
        host_end = memchr(auth, ']', auth_len);
        if (!host_end) {
            return -1;
        }
        if (host_end + 1 < auth + auth_len) {
            if (host_end[1] != ':') {
                return -1;
            }
            port = host_end + 2;
        }
    } else {
        host = auth;
        host_end = memchr(auth, ':', auth_len);
        if (host_end) {
            port = host_end + 1;
        } else {
            host_end = auth + auth_len;
        }
    }
    host_len = (size_t)(host_end - host);
    if (host_len == 0 || host_len >= sizeof(u->host) || auth_len >= sizeof(u->host_header) ||
        strlen(path ? path : "/") >= sizeof(u->path)) {
        return -1;
    }
    memcpy(u->host, host, host_len);
    u->host[host_len] = '\0';
    memcpy(u->host_header, auth, auth_len);
    u->host_header[auth_len] = '\0';
    strcpy(u->path, path ? path : "/");
    if (port) {
        size_t port_len = (size_t)(auth + auth_len - port);
        char *end;
        long p;
        if (port_len == 0 || port_len >= sizeof(u->port)) {
            return -1;
        }
        memcpy(u->port, port, port_len);
        u->port[port_len] = '\0';
        p = strtol(u->port, &end, 10);
        if (*end != '\0' || p < 1 || p > 65535) {
            return -1;
        }
    } else {
        strcpy(u->port, "80");
    }
    return 0;
}

/* Parametr device w adresie /ws (spec, sekcja 1). Host i tak sprawdza identyfikator. */
static int add_device(url_t *u, const char *device)
{
    size_t n = strlen(device);
    size_t i;
    if (n == 0 || n > 32 || device[0] == '-') {
        return -1;
    }
    for (i = 0; i < n; i++) {
        char c = device[i];
        if (!((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-')) {
            return -1;
        }
    }
    if (strlen(u->path) + n + 8 >= sizeof(u->path)) {
        return -1;
    }
    strcat(u->path, strchr(u->path, '?') ? "&device=" : "?device=");
    strcat(u->path, device);
    return 0;
}

/* Token jak na hoście: 16-128 znaków A-Z, a-z, 0-9, _ i - (spec, sekcja 1). */
static int load_token(void)
{
    const char *token = getenv("IRIS_TOKEN");
    size_t n;
    size_t i;
    if (!token || !*token) {
        return 0;
    }
    n = strlen(token);
    if (n < 16 || n > 128) {
        return -1;
    }
    for (i = 0; i < n; i++) {
        char c = token[i];
        if (!((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '_' ||
              c == '-')) {
            return -1;
        }
    }
    snprintf(protocols, sizeof(protocols), "iris, token.%s", token);
    return 0;
}

static int parse_pins(const char *s)
{
    while (*s) {
        char *end;
        long p = strtol(s, &end, 10);
        if (end == s || p < 0 || p > 255 || settings.gpio_count >= FW_GPIO_MAX) {
            return -1;
        }
        settings.gpio_pins[settings.gpio_count++] = (uint8_t)p;
        s = *end == ',' ? end + 1 : end;
        if (*end != ',' && *end != '\0') {
            return -1;
        }
    }
    return 0;
}

static void on_signal(int sig)
{
    (void)sig;
    stop_requested = 1;
}

static void usage(void)
{
    fprintf(stderr, "użycie: hermes [--url ws://host:port/ws] [--device ID] [--batt %%] [--temp C]\n"
                    "               [--rssi dBm] [--gpio-pins 5,6,7] [--audio-out KATALOG]\n%s",
            HELP);
}

int main(int argc, char **argv)
{
    static const char *const clips[] = {"ready", "error", "low_battery", "link_lost"};
    double batt = 71, temp = 24, rssi = -58;
    const char *pins = "5,6,7";
    const char *device = NULL;
    struct sigaction sa;
    uint32_t t0;
    int i;

    for (i = 1; i < argc; i++) {
        const char *val = i + 1 < argc ? argv[i + 1] : NULL;
        int bad = 0;
        if (strcmp(argv[i], "--help") == 0 || strcmp(argv[i], "-h") == 0) {
            usage();
            return 0;
        }
        if (!val) {
            bad = 1;
        } else if (strcmp(argv[i], "--url") == 0) {
            url_text = val;
        } else if (strcmp(argv[i], "--batt") == 0) {
            bad = parse_double(val, &batt) || batt < 0 || batt > 100;
        } else if (strcmp(argv[i], "--temp") == 0) {
            bad = parse_double(val, &temp);
        } else if (strcmp(argv[i], "--rssi") == 0) {
            bad = parse_double(val, &rssi);
        } else if (strcmp(argv[i], "--gpio-pins") == 0) {
            pins = val;
        } else if (strcmp(argv[i], "--audio-out") == 0) {
            audio_out = val;
        } else if (strcmp(argv[i], "--device") == 0) {
            device = val;
        } else {
            bad = 1;
        }
        if (bad) {
            usage();
            return 2;
        }
        i++;
    }
    if (parse_url(url_text, &url) != 0) {
        fprintf(stderr, "niepoprawny adres: %s\n", url_text);
        return 2;
    }
    if (device && add_device(&url, device) != 0) {
        fprintf(stderr, "niepoprawne --device: %s (a-z, 0-9, -, do 32 znaków)\n", device);
        return 2;
    }
    if (load_token() != 0) {
        fprintf(stderr, "niepoprawny IRIS_TOKEN: 16-128 znaków A-Z, a-z, 0-9, _ i -\n");
        return 2;
    }
    if (parse_pins(pins) != 0) {
        fprintf(stderr, "niepoprawne --gpio-pins: %s\n", pins);
        return 2;
    }
    for (i = 0; i < 4; i++) {
        settings.clips[i] = clips[i];
    }
    settings.clip_count = 4;
    settings.has = FW_HAS_ALL;

    setvbuf(stdout, NULL, _IOLBF, 0);
    signal(SIGPIPE, SIG_IGN);
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = on_signal;
    sigemptyset(&sa.sa_mask);
    sigaction(SIGINT, &sa, NULL);
    sigaction(SIGTERM, &sa, NULL);
    urandom = fopen("/dev/urandom", "rb");
    if (!urandom) {
        fprintf(stderr, "brak /dev/urandom: maski ramek będą przewidywalne\n");
    }

    t0 = now_ms();
    lcg = t0;
    hal.send_text = hal_send_text;
    hal.send_binary = hal_send_binary;
    hal.gpio_write = hal_gpio;
    hal.haptic = hal_haptic;
    hal.play_clip = hal_clip;
    hal.play_audio = hal_play;
    hal.config_changed = hal_config;
    hal.active_changed = hal_active;
    fw_body_init(&body, &hal, &settings, t0, random32(NULL), "power");
    fw_body_set_battery(&body, (int32_t)lround(batt * 10));
    fw_body_set_rssi(&body, (int16_t)lround(rssi));
    fw_body_set_temp(&body, (int32_t)lround(temp * 10));
    retry_at = t0;
    fprintf(stderr, "HERMES %s, boot_id %s, host ws://%s%s, token: %s\n", FW_VERSION, body.boot_id,
            url.host_header, url.path, protocols[0] ? "tak" : "nie");

    for (;;) {
        struct pollfd fds[2];
        nfds_t nfds = 0;
        int si = -1;
        int ii = -1;
        uint32_t now = now_ms();

        if (stop_requested && !quitting) {
            quitting = 1;
            quit_deadline = now + FW_WS_CLOSE_MS + 200u;
            if (ws_up) {
                fw_ws_close(&ws, FW_WS_NORMAL, now);
            } else {
                close_socket();
            }
        }
        if (quitting && (!ws_up || due(now, quit_deadline))) {
            break;
        }
        if (offline && due(now, offline_until)) {
            offline = 0;
            retry_at = now;
        }
        if (!quitting && !offline && sock < 0 && due(now, retry_at)) {
            start_connect(now);
        }
        if (connecting && now - connect_ms >= FW_WS_HANDSHAKE_MS) {
            close(sock);
            sock = -1;
            connecting = 0;
            try_next_address(now);
        }

        if (stdin_open) {
            fds[nfds].fd = STDIN_FILENO;
            fds[nfds].events = POLLIN;
            ii = (int)nfds++;
        }
        if (sock >= 0) {
            fds[nfds].fd = sock;
            fds[nfds].events = connecting ? POLLOUT : POLLIN;
            si = (int)nfds++;
        }
        if (poll(fds, nfds, POLL_MS) > 0) {
            now = now_ms();
            if (si >= 0 && fds[si].revents) {
                if (connecting) {
                    connect_progress(now);
                } else {
                    sock_read(now);
                }
            }
            if (ii >= 0 && fds[ii].revents) {
                read_stdin(now);
            }
        }
        now = now_ms();
        if (ws_up) {
            fw_ws_tick(&ws, now);
        }
        fw_body_tick(&body, now);
        if (playing && due(now, play_end_ms)) {
            playing = 0;
            printf("played %u\n", play_id);
            fw_body_playback_done(&body);
        }
    }
    close_socket();
    if (urandom) {
        fclose(urandom);
    }
    fprintf(stderr, "koniec\n");
    return 0;
}
