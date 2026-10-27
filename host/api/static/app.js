// Klient IRIS w przeglądarce (docs/spec.md, sekcja 1, "Klient w przeglądarce").
// Połączenie WebSocket z hostem, mowa z przyciskiem (WebCodecs Opus) i odtwarzanie odpowiedzi.

import { Body, validDeviceId } from "./protocol.js";

const RATE = 16000; // spec, sekcja 6
const FRAME = RATE / 50; // 20 ms
const MAX_UTTERANCE_S = 60;
const BACKOFF_MIN_MS = 500;
const BACKOFF_MAX_MS = 5000;
const CLOSE_REASONS = {
  4000: "połączenie przejęło to samo urządzenie w innym miejscu",
  4001: "host ma już połączone maksimum urządzeń",
  4002: "niepoprawna nazwa urządzenia",
  4003: "brak lub zły token",
};

const $ = (id) => document.getElementById(id);
const store = {
  get(key, fallback) {
    try {
      return localStorage.getItem(key) ?? fallback;
    } catch {
      return fallback;
    }
  },
  set(key, value) {
    try {
      localStorage.setItem(key, value);
    } catch {
      /* tryb prywatny: ustawienia tylko do zamknięcia strony */
    }
  },
};

const state = {
  ws: null,
  wanted: false, // użytkownik chce być połączony (ponawiamy po zerwaniu)
  backoff: BACKOFF_MIN_MS,
  retry: null,
  heartbeat: null,
  audio: null, // AudioContext odtwarzania
  gain: null,
  volume: 0.5,
  mic: null, // {stream, ctx, node}
  talk: null, // bieżąca wypowiedź
  played: [], // {audioId, packets, duration, sampleRate} (testy i dziennik)
  lastSamples: null,
};

const supported = window.isSecureContext && "AudioEncoder" in window && "AudioDecoder" in window;
const has = ["speaker"];
if (supported && navigator.mediaDevices?.getUserMedia) has.unshift("mic");
if (navigator.vibrate) has.push("haptic");

const body = new Body({
  has,
  io: {
    send: (text) => sendText(text),
    sendBinary: (bytes) => sendBytes(bytes),
    play: (playback) => play(playback),
    vibrate: navigator.vibrate ? (pattern) => navigator.vibrate(pattern) : null,
    setVolume: (v) => {
      state.volume = v;
      if (state.gain) state.gain.gain.value = v;
    },
    onActive: () => render(),
  },
});

// --- dziennik i widok ---

function log(text) {
  const li = document.createElement("li");
  li.textContent = `${new Date().toLocaleTimeString("pl-PL")} ${text}`;
  $("log").prepend(li);
  while ($("log").children.length > 30) $("log").lastChild.remove();
}

function setStatus(text) {
  $("status").textContent = text;
}

function render() {
  const connected = Boolean(state.ws && state.ws.readyState === WebSocket.OPEN);
  $("active").textContent = !connected ? "" : body.active ? "Aktywne urządzenie" : "W spoczynku";
  $("talk").disabled = !connected || !body.active || !has.includes("mic");
  $("activate").hidden = !connected || body.active;
  $("connect").hidden = state.wanted;
  $("disconnect").hidden = !state.wanted;
}

// --- połączenie ---

function sendText(text) {
  if (state.ws?.readyState !== WebSocket.OPEN) return false;
  state.ws.send(text);
  return true;
}

function sendBytes(bytes) {
  if (state.ws?.readyState !== WebSocket.OPEN) return false;
  state.ws.send(bytes);
  return true;
}

function wsUrl(device) {
  const scheme = location.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${location.host}/ws?device=${encodeURIComponent(device)}`;
}

function connect() {
  const device = $("device").value.trim();
  const token = $("token").value.trim();
  if (!validDeviceId(device)) {
    setStatus("Nazwa urządzenia: małe litery, cyfry i '-', do 32 znaków");
    return;
  }
  store.set("iris.device", device);
  store.set("iris.token", token);
  state.wanted = true;
  ensureAudio();
  open(device, token);
  render();
}

function open(device, token) {
  clearTimeout(state.retry);
  setStatus("Łączenie…");
  // Token w podprotokole: przeglądarka nie może ustawić nagłówka (spec, sekcja 1).
  const ws = new WebSocket(wsUrl(device), token ? ["iris", `token.${token}`] : ["iris"]);
  ws.binaryType = "arraybuffer";
  state.ws = ws;
  ws.onopen = () => {
    state.backoff = BACKOFF_MIN_MS;
    setStatus(`Połączono jako ${device}`);
    body.onOpen();
    state.heartbeat = setInterval(() => body.heartbeat(), 1000);
    body.heartbeat();
    render();
  };
  ws.onmessage = (ev) => {
    if (typeof ev.data === "string") body.onText(ev.data);
    else body.onBinary(ev.data);
  };
  ws.onclose = (ev) => {
    if (state.ws !== ws) return;
    clearInterval(state.heartbeat);
    body.onClose();
    state.ws = null;
    const reason = CLOSE_REASONS[ev.code];
    if (reason) log(`Rozłączono: ${reason} (${ev.code})`);
    if (state.wanted) {
      setStatus(`Brak połączenia${reason ? `: ${reason}` : ""}, ponawiam za ${state.backoff / 1000} s`);
      state.retry = setTimeout(() => open(device, token), state.backoff);
      state.backoff = Math.min(BACKOFF_MAX_MS, state.backoff * 2);
    } else {
      setStatus("Rozłączono");
    }
    render();
  };
}

function disconnect() {
  state.wanted = false;
  clearTimeout(state.retry);
  if (state.ws) state.ws.close(1000);
  render();
}

// --- odtwarzanie ---

function ensureAudio() {
  if (!state.audio) {
    state.audio = new AudioContext();
    state.gain = state.audio.createGain();
    state.gain.gain.value = state.volume;
    state.gain.connect(state.audio.destination);
  }
  state.audio.resume(); // wymaga gestu użytkownika (przycisk Połącz)
}

// Dekoduje pakiety Opus (brakujące to cisza) i odtwarza. Zwraca czas trwania w sekundach.
async function decodePackets(packets) {
  const pieces = [];
  const decoder = new AudioDecoder({
    output: (data) => {
      const samples = new Float32Array(data.numberOfFrames);
      data.copyTo(samples, { planeIndex: 0, format: "f32-planar" });
      pieces.push({ timestamp: data.timestamp, rate: data.sampleRate, samples });
      data.close();
    },
    error: (e) => log(`Błąd dekodowania: ${e.message}`),
  });
  decoder.configure({ codec: "opus", sampleRate: RATE, numberOfChannels: 1 });
  packets.forEach((packet, seq) => {
    if (packet) decoder.decode(new EncodedAudioChunk({ type: "key", timestamp: seq * 20000, data: packet }));
  });
  await decoder.flush();
  decoder.close();
  const rate = pieces.length ? pieces[0].rate : RATE;
  // Każdy fragment na miejscu z jego znacznika czasu, więc brakujące pakiety zostają ciszą.
  const out = new Float32Array(Math.round((packets.length * 20 * rate) / 1000));
  for (const p of pieces) {
    const at = Math.round((p.timestamp / 1e6) * rate);
    if (at < out.length) out.set(p.samples.subarray(0, out.length - at), at);
  }
  return { samples: out, rate };
}

async function play({ audioId, packets }) {
  ensureAudio();
  const { samples, rate } = await decodePackets(packets);
  const buffer = state.audio.createBuffer(1, Math.max(1, samples.length), rate);
  buffer.copyToChannel(samples, 0);
  const source = state.audio.createBufferSource();
  source.buffer = buffer;
  source.connect(state.gain);
  const duration = samples.length / rate;
  state.lastSamples = { samples, rate };
  await new Promise((resolve) => {
    source.onended = resolve;
    source.start();
    setTimeout(resolve, duration * 1000 + 500); // gdyby kontekst był wstrzymany
  });
  state.played.push({ audioId, packets: packets.length, duration, sampleRate: rate });
  log(`Odtworzono odpowiedź ${audioId} (${duration.toFixed(1)} s)`);
}

// --- mikrofon ---

function newEncoder() {
  const encoder = new AudioEncoder({
    output: (chunk) => {
      const bytes = new Uint8Array(chunk.byteLength);
      chunk.copyTo(bytes);
      body.micPacket(bytes);
    },
    error: (e) => log(`Błąd kodowania: ${e.message}`),
  });
  encoder.configure({ codec: "opus", sampleRate: RATE, numberOfChannels: 1, bitrate: 24000, opus: { frameDuration: 20000 } });
  return encoder;
}

// Wypowiedź z dowolnego źródła PCM 16 kHz: start, pakiety po 20 ms, koniec po opróżnieniu kodera.
function beginUtterance() {
  const uttId = body.utteranceStart();
  if (!uttId) return null;
  return { uttId, encoder: newEncoder(), pending: new Float32Array(FRAME), filled: 0, samples: 0 };
}

function pushSamples(talk, input) {
  let offset = 0;
  while (offset < input.length && talk.samples < MAX_UTTERANCE_S * RATE) {
    const n = Math.min(FRAME - talk.filled, input.length - offset);
    talk.pending.set(input.subarray(offset, offset + n), talk.filled);
    talk.filled += n;
    offset += n;
    if (talk.filled === FRAME) {
      encodeFrame(talk, talk.pending);
      talk.pending = new Float32Array(FRAME);
      talk.filled = 0;
    }
  }
}

function encodeFrame(talk, frame) {
  const data = new AudioData({
    format: "f32-planar",
    sampleRate: RATE,
    numberOfFrames: FRAME,
    numberOfChannels: 1,
    timestamp: Math.round((talk.samples / RATE) * 1e6),
    data: frame,
  });
  talk.encoder.encode(data);
  data.close();
  talk.samples += FRAME;
}

async function endUtterance(talk) {
  if (talk.filled > 0) encodeFrame(talk, talk.pending); // ostatnia ramka dopełniona ciszą
  await talk.encoder.flush();
  talk.encoder.close();
  body.utteranceEnd((talk.samples / RATE) * 1000);
}

async function openMic() {
  if (state.mic) return state.mic;
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
  });
  const ctx = new AudioContext({ sampleRate: RATE });
  await ctx.audioWorklet.addModule("mic-worklet.js");
  const source = ctx.createMediaStreamSource(stream);
  const node = new AudioWorkletNode(ctx, "iris-capture");
  const sink = ctx.createGain();
  sink.gain.value = 0; // węzeł musi być podłączony do wyjścia, żeby działał; bez odsłuchu
  source.connect(node).connect(sink).connect(ctx.destination);
  node.port.onmessage = (ev) => {
    if (state.talk) pushSamples(state.talk, ev.data);
  };
  state.mic = { stream, ctx, node };
  return state.mic;
}

async function startTalking() {
  if (state.talk || $("talk").disabled) return;
  try {
    const mic = await openMic();
    await mic.ctx.resume();
  } catch (e) {
    log(`Brak dostępu do mikrofonu: ${e.message}`);
    return;
  }
  state.talk = beginUtterance();
  if (state.talk) $("talk").classList.add("on");
}

async function stopTalking() {
  const talk = state.talk;
  if (!talk) return;
  state.talk = null;
  $("talk").classList.remove("on");
  await endUtterance(talk);
}

// --- bateria ---

async function watchBattery() {
  if (!navigator.getBattery) return;
  try {
    const battery = await navigator.getBattery();
    // Komputer bez baterii zgłasza 100% i ładowanie z czasem 0; wtedy nie podajemy poziomu.
    const update = () => {
      const noBattery = battery.charging && battery.chargingTime === 0 && battery.level === 1;
      body.battery = noBattery ? null : battery.level * 100;
    };
    update();
    battery.addEventListener("levelchange", update);
    battery.addEventListener("chargingchange", update);
    if (body.battery !== null) has.push("battery");
  } catch {
    /* bez poziomu baterii */
  }
}

// --- start ---

function init() {
  $("device").value = store.get("iris.device", "przegladarka");
  $("token").value = store.get("iris.token", "");
  $("connect").addEventListener("click", connect);
  $("disconnect").addEventListener("click", disconnect);
  $("activate").addEventListener("click", () => body.requestActive());
  const talk = $("talk");
  talk.addEventListener("pointerdown", (e) => {
    talk.setPointerCapture(e.pointerId);
    startTalking();
  });
  for (const ev of ["pointerup", "pointercancel"]) talk.addEventListener(ev, stopTalking);
  talk.addEventListener("keydown", (e) => {
    if (e.code === "Space" && !e.repeat) {
      e.preventDefault();
      startTalking();
    }
  });
  talk.addEventListener("keyup", (e) => {
    if (e.code === "Space") stopTalking();
  });
  if (!supported) {
    $("unsupported").hidden = false;
  }
  if ("serviceWorker" in navigator && window.isSecureContext) {
    navigator.serviceWorker.register("sw.js").catch(() => null); // bez instalacji, klient działa dalej
  }
  watchBattery();
  render();
}

// Do testów automatycznych (Playwright): ten sam tor co mikrofon, ale z podanego PCM 16 kHz.
window.iris = {
  body,
  state,
  async sayPcm(samples) {
    const talk = beginUtterance();
    if (!talk) return 0;
    pushSamples(talk, Float32Array.from(samples));
    await endUtterance(talk);
    return talk.uttId;
  },
};

init();
