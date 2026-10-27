// Protokół ciała w przeglądarce (docs/spec.md, sekcje 1-6): koperta, zdarzenia, akcje, bufor
// odtwarzania. Bez DOM i audio, więc te same reguły da się sprawdzić obok symulatora i firmware.

export const PROTOCOL_VERSION = 1;
export const MAX_HOST_FRAME_BYTES = 4096; // urządzenie z `display` (spec, sekcja 5)
export const OUTBOX_LIMIT = 64;
export const KIND_MIC = 1;
export const KIND_PLAYBACK = 2;
export const PLAYBACK_BYTES = 1024 * 1024;
export const PLAYBACK_PACKETS = 4096;
export const HAPTIC_PATTERNS = {
  attention: [80],
  warn: [120, 80, 120],
  critical: [200, 100, 200, 100, 200],
};
const CONFIG_KEYS = ["volume", "batt_warn_pct", "temp_warn_c"];
const DISPLAY_KINDS = ["heard", "reply", "nothing", "failed"];
const DEVICE_ID = /^[a-z0-9][a-z0-9-]{0,31}$/;

export function validDeviceId(id) {
  return DEVICE_ID.test(id);
}

function isInt(x) {
  return typeof x === "number" && Number.isInteger(x);
}

function isNumber(x) {
  return typeof x === "number" && Number.isFinite(x);
}

// audio_id: liczba albo napis z samymi cyframi (do 9), jak w firmware.
function audioIdOf(x) {
  if (isInt(x)) return x;
  if (typeof x === "string" && /^[0-9]{1,9}$/.test(x)) return Number(x);
  return null;
}

export class Body {
  // io: send(text), sendBinary(Uint8Array), play({audioId, packets}) -> czas trwania w s,
  //     vibrate(pattern) albo null, setVolume(0..1), onActive(active), onDisplay(body)
  constructor({ io, now = () => performance.now(), random = Math.random, has = ["mic", "speaker"] }) {
    this.io = io;
    this.now = now;
    this.has = has;
    this.bootId = Array.from({ length: 8 }, () => Math.floor(random() * 16).toString(16)).join("");
    this.bootT = now();
    this.n = 0;
    this.connected = false;
    this.active = true;
    this.outbox = [];
    this.dropped = 0;
    this.frameErrors = 0;
    this.busyDrops = 0;
    this.battery = null; // 0..100 albo null
    this.settings = { volume: 50, batt_warn_pct: 20, temp_warn_c: 40 };
    this.utt = 0;
    this.uttActive = false;
    this.micSeq = 0;
    this.playback = new Map(); // seq -> Uint8Array dla jednego audio_id
    this.playbackId = 0;
    this.playbackBytes = 0;
    this.playing = false;
    this.emit("system.boot", { boot_id: this.bootId, fw: "web-0.1", reset_reason: "power" });
  }

  frame(t, type, body) {
    this.n += 1;
    return JSON.stringify({
      v: PROTOCOL_VERSION,
      t,
      id: `${this.bootId.slice(0, 4)}.${this.n}`,
      ts: Math.max(0, Math.round(this.now() - this.bootT)),
      type,
      body,
    });
  }

  // Zdarzenie: bez łącza trafia do bufora (najstarsze giną), chyba że nie jest buforowane.
  emit(type, body, bufferable = true) {
    const frame = this.frame("event", type, body);
    if (this.connected && this.io.send(frame)) return;
    if (!bufferable) return;
    if (this.outbox.length >= OUTBOX_LIMIT) {
      this.outbox.shift();
      this.dropped += 1;
    }
    this.outbox.push(frame);
  }

  caps() {
    return { has: [...this.has], clips: [], playback_bytes: PLAYBACK_BYTES, playback_packets: PLAYBACK_PACKETS };
  }

  // Po każdym połączeniu: aktywne, dopóki host nie powie inaczej; caps przed zaległymi zdarzeniami.
  onOpen() {
    this.connected = true;
    this.active = true;
    this.io.send(this.frame("event", "device.caps", this.caps()));
    while (this.outbox.length) {
      if (!this.io.send(this.outbox[0])) return;
      this.outbox.shift();
    }
  }

  onClose() {
    this.connected = false;
  }

  heartbeat() {
    const body = { frame_errors: this.frameErrors };
    if (this.battery !== null && this.has.includes("battery")) body.batt_pct = Math.round(this.battery * 10) / 10;
    if (this.connected) this.io.send(this.frame("event", "body.heartbeat", body));
  }

  requestActive() {
    this.emit("device.activate", {}, false); // spóźniona prośba przełączyłaby IRIS nieoczekiwanie
  }

  // --- mikrofon ---

  utteranceStart() {
    if (!this.active || !this.has.includes("mic")) return 0;
    this.utt = (this.utt % 0xffffffff) + 1;
    this.micSeq = 0;
    this.uttActive = true;
    this.emit("audio.utterance.start", { utt_id: this.utt });
    return this.utt;
  }

  micPacket(bytes) {
    if (!this.uttActive || bytes.length > 1275) return;
    const frame = new Uint8Array(5 + bytes.length);
    const id = this.utt & 0xffff;
    frame[0] = KIND_MIC;
    frame[1] = id >> 8;
    frame[2] = id & 0xff;
    frame[3] = (this.micSeq >> 8) & 0xff;
    frame[4] = this.micSeq & 0xff;
    frame.set(bytes, 5);
    this.micSeq += 1;
    if (this.connected) this.io.sendBinary(frame); // audio nie jest buforowane
  }

  utteranceEnd(durMs) {
    if (!this.uttActive) return;
    this.uttActive = false;
    this.emit("audio.utterance.end", { utt_id: this.utt, dur_ms: Math.max(0, Math.round(durMs)) });
  }

  // --- od hosta ---

  onBinary(buffer) {
    const data = new Uint8Array(buffer);
    if (data.length < 5 || data[0] !== KIND_PLAYBACK) {
      this.frameErrors += 1;
      return;
    }
    const id = (data[1] << 8) | data[2];
    const seq = (data[3] << 8) | data[4];
    if (id === 0) {
      this.frameErrors += 1;
      return;
    }
    if (this.playing) {
      this.busyDrops += 1; // jeden bufor: w trakcie odtwarzania nic nie przyjmujemy
      return;
    }
    if (id !== this.playbackId) {
      this.playback = new Map(); // nowe audio_id zastępuje niezapowiedziane ramki
      this.playbackId = id;
      this.playbackBytes = 0;
    }
    const packet = data.slice(5);
    if (!packet.length || this.playback.has(seq)) return;
    if (seq >= PLAYBACK_PACKETS || this.playbackBytes + packet.length > PLAYBACK_BYTES) return;
    this.playback.set(seq, packet);
    this.playbackBytes += packet.length;
  }

  // Zwraca ack (obiekt body) albo null dla ramki odrzuconej (licznik frame_errors).
  onText(raw) {
    if (new TextEncoder().encode(raw).length > MAX_HOST_FRAME_BYTES) {
      this.frameErrors += 1;
      return null;
    }
    let data;
    try {
      data = JSON.parse(raw);
    } catch {
      this.frameErrors += 1;
      return null;
    }
    const body = data && typeof data === "object" ? data.body ?? {} : null;
    if (
      !data || typeof data !== "object" || Array.isArray(data) ||
      data.v !== PROTOCOL_VERSION || data.t !== "action" ||
      typeof data.id !== "string" || typeof data.type !== "string" ||
      typeof body !== "object" || body === null || Array.isArray(body)
    ) {
      this.frameErrors += 1;
      return null;
    }
    const err = this.action(data.type, body);
    const ack = { ref: data.id, ok: err === null };
    if (err !== null) ack.err = err;
    if (this.connected) this.io.send(this.frame("ack", data.type, ack));
    return ack;
  }

  action(type, body) {
    switch (type) {
      case "ping":
        return null;
      case "haptic": {
        const pattern = body.pattern;
        if (!(typeof pattern === "string" && pattern in HAPTIC_PATTERNS)) {
          return typeof pattern === "string" ? "bad_value" : "bad_body";
        }
        if (!this.has.includes("haptic") || !this.io.vibrate) return "unsupported";
        this.io.vibrate(HAPTIC_PATTERNS[pattern]);
        return null;
      }
      case "speak":
        return this.speak(body);
      case "speak.clip":
        return typeof body.clip === "string" ? "unknown_clip" : "bad_body";
      case "gpio.set":
        if (!isInt(body.pin) || !isInt(body.value)) return "bad_body";
        return "unsupported";
      case "config.set": {
        const { key, value } = body;
        if (!CONFIG_KEYS.includes(key)) return typeof key === "string" ? "unknown_key" : "bad_body";
        if (!isNumber(value)) return "bad_body";
        if ((key === "volume" || key === "batt_warn_pct") && !(value >= 0 && value <= 100)) return "bad_value";
        this.settings[key] = value;
        if (key === "volume" && this.io.setVolume) this.io.setVolume(value / 100);
        return null;
      }
      case "display": {
        if (!DISPLAY_KINDS.includes(body.kind)) return typeof body.kind === "string" ? "bad_value" : "bad_body";
        const withText = body.kind === "heard" || body.kind === "reply";
        if (withText !== (typeof body.text === "string")) return "bad_body";
        if (this.io.onDisplay) this.io.onDisplay(body);
        return null;
      }
      case "device.active":
        if (typeof body.active !== "boolean") return "bad_body";
        this.active = body.active;
        if (this.io.onActive) this.io.onActive(this.active);
        return null;
      default:
        return "unknown_action";
    }
  }

  speak(body) {
    const id = audioIdOf(body.audio_id);
    if (id === null) return "bad_body";
    if (this.playing) return "busy";
    if (id <= 0 || id > 0xffff || id !== this.playbackId || !this.playback.size) return "no_audio";
    const count = Math.max(...this.playback.keys()) + 1;
    const packets = Array.from({ length: count }, (_, seq) => this.playback.get(seq) ?? null);
    this.playing = true;
    Promise.resolve(this.io.play({ audioId: id, packets, volume: this.settings.volume / 100 }))
      .catch(() => null)
      .finally(() => this.playbackDone());
    return null;
  }

  playbackDone() {
    this.playing = false;
    this.playback = new Map();
    this.playbackId = 0;
    this.playbackBytes = 0;
  }
}
