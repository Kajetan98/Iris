// Klient IRIS w przeglądarce (docs/spec.md, sekcja 1, "Klient w przeglądarce").
// Fala głosu IRIS, napisy rozmowy, mowa z przyciskiem (WebCodecs Opus) i odtwarzanie odpowiedzi.

import { Wave } from "./wave.js";
import { Body, validDeviceId } from "./protocol.js";

const RATE = 16000; // spec, sekcja 6
const FRAME = RATE / 50; // 20 ms
const MAX_UTTERANCE_S = 60;
const BACKOFF_MIN_MS = 500;
const BACKOFF_MAX_MS = 5000;
const THINKING_TIMEOUT_MS = 30000;
const RESUME_AFTER_MS = 20000; // po tylu ms w tle połączenie jest odnawiane (spec, sekcja 1)
const MAX_TURNS = 20;
const CLOSE_REASONS = {
  4000: "To urządzenie połączyło się z hostem w innej karcie lub oknie.",
  4001: "Host ma już połączone maksimum urządzeń.",
  4002: "Niepoprawna nazwa urządzenia. Popraw ją w ustawieniach.",
  4003: "Host odrzucił połączenie: brak lub zły token. Wpisz w ustawieniach token z IRIS_TOKEN (przez tunel host musi go mieć).",
};
const STATE_TEXT = {
  offline: "Brak połączenia z hostem",
  connecting: "Łączę się z hostem",
  inactive: "W spoczynku: aktywne jest inne urządzenie",
  idle: "Przytrzymaj przycisk i mów",
  listening: "Słucham",
  thinking: "Przygotowuję odpowiedź",
  speaking: "Mówię",
};

const $ = (id) => document.getElementById(id);
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
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
  device: "",
  backoff: BACKOFF_MIN_MS,
  retry: null,
  heartbeat: null,
  ui: "offline",
  thinkingTimer: null,
  audio: null, // AudioContext odtwarzania
  gain: null,
  analyser: null,
  levelBuf: null,
  volume: 0.5,
  voice: null, // "ready" | "disabled" | "error" z /health, null przed odpowiedzią
  mic: null, // {stream, ctx, node}
  micLevel: 0,
  held: false, // przycisk mówienia wciśnięty
  talk: null, // bieżąca wypowiedź
  turns: new Map(), // turn -> {li, heard, reply, note, words}
  replyTurn: null, // tura, której odpowiedź zaraz zabrzmi
  played: [], // {audioId, packets, duration, sampleRate} (testy)
  lastSamples: null,
  hiddenAt: null, // performance.now() przejścia strony w tło
};

const supported = window.isSecureContext && "AudioEncoder" in window && "AudioDecoder" in window;
const has = ["speaker", "display"];
if (supported && navigator.mediaDevices?.getUserMedia) has.unshift("mic");
if (navigator.vibrate) has.push("haptic");

const wave = new Wave($("wave"), { level: currentLevel, reducedMotion });

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
    onActive: (active) => activeChanged(active),
    onDisplay: (caption) => showCaption(caption),
  },
});

// --- stan widoku ---

function linkUp() {
  return Boolean(state.ws && state.ws.readyState === WebSocket.OPEN);
}

function restState() {
  return body.active ? "idle" : "inactive";
}

// Przełączenie w trakcie tury: host pomija audio i odpowiedź urządzenia, które przestało być
// aktywne (spec, sekcja 1), więc tura kończy się tutaj. Odtwarzana odpowiedź gra do końca.
function activeChanged(active) {
  if (state.ui === "speaking") return render(); // koniec odtwarzania ustawi stan spoczynku
  if (active) return state.ui === "listening" || state.ui === "thinking" ? render() : setUi("idle");
  if (state.talk) stopTalking();
  if (state.ui === "listening" || state.ui === "thinking") noteOnLatest("Aktywne jest teraz inne urządzenie.");
  setUi("inactive");
}

function setUi(next) {
  state.ui = next;
  clearTimeout(state.thinkingTimer);
  if (next === "thinking") {
    state.thinkingTimer = setTimeout(() => {
      noteOnLatest("Host nie odpowiedział w ciągu 30 s.", true);
      setUi(restState());
    }, THINKING_TIMEOUT_MS);
  }
  wave.setState(next);
  render();
}

function render() {
  const up = linkUp();
  document.body.dataset.state = state.ui;
  document.body.dataset.link = up ? "up" : "down";
  $("state").textContent = STATE_TEXT[state.ui];
  $("status").textContent = up ? `${state.device}: połączono` : state.wanted ? `${state.device}: łączę` : "nie połączono";
  const talk = $("talk");
  talk.hidden = !up || !body.active; // bez połączenia zostaje "Połącz", w spoczynku "Używaj tego urządzenia"
  talk.disabled =
    !up || !body.active || !has.includes("mic") || ["disabled", "error"].includes(state.voice) ||
    state.ui === "thinking" || state.ui === "speaking";
  $("talk-label").textContent = state.talk ? "Puść, gdy skończysz" : "Przytrzymaj i mów";
  $("activate").hidden = !up || body.active;
  $("reconnect").hidden = state.wanted;
  $("disconnect").hidden = !state.wanted;
  $("hint").hidden = state.turns.size > 0;
}

function notice(text) {
  $("notice").textContent = text;
}

// --- napisy ---

function turnFor(id) {
  let turn = state.turns.get(id);
  if (turn) return turn;
  const li = document.createElement("li");
  li.className = "turn";
  const heard = document.createElement("p");
  heard.className = "heard";
  heard.hidden = true;
  const reply = document.createElement("p");
  reply.className = "reply";
  reply.hidden = true;
  reply.setAttribute("aria-live", "polite");
  const note = document.createElement("p");
  note.className = "note";
  note.hidden = true;
  li.append(heard, reply, note);
  $("turns").append(li);
  turn = { li, heard, reply, note, words: [] };
  state.turns.set(id, turn);
  while (state.turns.size > MAX_TURNS) {
    const [oldest] = state.turns.keys();
    state.turns.get(oldest).li.remove();
    state.turns.delete(oldest);
  }
  render();
  return turn;
}

function setNote(turn, text, warn = false) {
  turn.note.textContent = text;
  turn.note.classList.toggle("warn", warn);
  turn.note.hidden = !text;
}

function noteOnLatest(text, warn) {
  const last = [...state.turns.values()].at(-1);
  if (last) setNote(last, text, warn);
  else notice(text);
}

function scrollToLatest() {
  const log = document.querySelector(".talk-log");
  log.scrollTo({ top: log.scrollHeight, behavior: reducedMotion ? "auto" : "smooth" });
}

function showCaption({ turn: id, kind, text, truncated }) {
  const turn = turnFor(id);
  if (kind === "heard") {
    turn.heard.replaceChildren();
    const who = document.createElement("span");
    who.className = "who";
    who.textContent = "Ty";
    turn.heard.append(who, ` ${text}`);
    turn.heard.hidden = false;
  } else if (kind === "reply") {
    // Całe zdanie od razu w DOM (czytnik ekranu dostaje je w całości), słowa odsłaniane z mową.
    turn.reply.replaceChildren();
    turn.words = text.split(/(\s+)/).map((part) => {
      const span = document.createElement("span");
      span.textContent = part;
      if (part.trim()) span.className = reducedMotion ? "word" : "word pending";
      turn.reply.append(span);
      return span;
    });
    turn.reply.hidden = false;
    state.replyTurn = turn;
    setNote(turn, truncated ? "Odpowiedź skrócona do limitu mowy." : "");
    turn.fallback = setTimeout(() => revealAll(turn), 4000); // gdyby dźwięk nie zagrał
  } else if (kind === "nothing") {
    setNote(turn, "Nic nie rozpoznano. Mów bliżej mikrofonu.");
    setUi(restState());
  } else if (kind === "failed") {
    setNote(turn, "Nie udało się przygotować odpowiedzi.", true);
    setUi(restState());
  }
  scrollToLatest();
}

function revealAll(turn) {
  clearTimeout(turn.fallback);
  for (const w of turn.words) w.classList.remove("pending");
}

// Słowa odsłaniane proporcjonalnie do liczby znaków w czasie odtwarzania (przybliżenie).
function revealWithSpeech(turn, duration) {
  clearTimeout(turn.fallback);
  if (reducedMotion || !turn.words.length) return revealAll(turn);
  const total = turn.words.reduce((n, w) => n + w.textContent.length, 0) || 1;
  const start = performance.now();
  let chars = 0;
  const due = turn.words.map((w) => {
    const at = (chars / total) * duration * 1000;
    chars += w.textContent.length;
    return at;
  });
  const tick = () => {
    const elapsed = performance.now() - start;
    turn.words.forEach((w, i) => {
      if (due[i] <= elapsed) w.classList.remove("pending");
    });
    if (elapsed < duration * 1000) requestAnimationFrame(tick);
    else revealAll(turn);
  };
  requestAnimationFrame(tick);
}

// --- połączenie ---

function sendText(text) {
  if (!linkUp()) return false;
  state.ws.send(text);
  return true;
}

function sendBytes(bytes) {
  if (!linkUp()) return false;
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
    $("device").setCustomValidity("Małe litery, cyfry i myślnik, do 32 znaków.");
    $("device").reportValidity();
    return false;
  }
  $("device").setCustomValidity("");
  store.set("iris.device", device);
  store.set("iris.token", token);
  state.device = device;
  state.wanted = true;
  notice("");
  ensureAudio();
  open(device, token);
  return true;
}

function open(device, token) {
  clearTimeout(state.retry);
  if (state.ws) {
    const old = state.ws;
    state.ws = null;
    old.close(1000);
  }
  setUi("connecting");
  // Token w podprotokole: przeglądarka nie może ustawić nagłówka (spec, sekcja 1).
  const ws = new WebSocket(wsUrl(device), token ? ["iris", `token.${token}`] : ["iris"]);
  ws.binaryType = "arraybuffer";
  state.ws = ws;
  ws.onopen = () => {
    state.backoff = BACKOFF_MIN_MS;
    notice("");
    body.onOpen();
    let first = true;
    state.heartbeat = setInterval(() => {
      body.heartbeat();
      // Po pierwszej sekundzie: przy złym tokenie host zamyka połączenie od razu (4003),
      // a zapytanie z tym tokenem skończyłoby się tylko błędem 401.
      if (first && state.ws === ws) checkVoice(token);
      first = false;
    }, 1000);
    body.heartbeat();
    setUi(restState());
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
    if (state.wanted) {
      const delay = (state.backoff / 1000).toLocaleString("pl-PL");
      notice(`${reason ?? "Połączenie przerwane."} Ponawiam za ${delay} s.`);
      state.retry = setTimeout(() => open(device, token), state.backoff);
      state.backoff = Math.min(BACKOFF_MAX_MS, state.backoff * 2);
      setUi("connecting");
    } else {
      setUi("offline");
    }
  };
}

// Bez głosu na hoście (STT i TTS) wypowiedź nie dostałaby odpowiedzi, więc przycisk jest wyłączony.
// Gdy stanu nie da się sprawdzić, przycisk działa.
async function checkVoice(token) {
  try {
    const res = await fetch("/health", { headers: token ? { Authorization: `Bearer ${token}` } : {} });
    state.voice = res.ok ? (await res.json()).voice : null;
  } catch {
    state.voice = null;
  }
  if (state.voice === "disabled") notice("Na hoście nie jest włączone rozpoznawanie i synteza mowy ([brain.stt] i [brain.tts]).");
  else if (state.voice === "error") notice("Host nie wczytał modeli mowy. Szczegóły są w logu hosta.");
  render();
}

// Telefon potrafi w tle zerwać WebSocket bez zdarzenia close, a przeglądarka nie wysyła pingów.
// Po dłuższej nieobecności strona łączy się od nowa; nowe połączenie zastępuje stare (4000).
function visibilityChanged() {
  if (document.visibilityState === "hidden") {
    state.hiddenAt = performance.now();
    return;
  }
  const away = state.hiddenAt === null ? 0 : performance.now() - state.hiddenAt;
  state.hiddenAt = null;
  if (state.wanted && !state.talk && away >= RESUME_AFTER_MS) open(state.device, $("token").value.trim());
}

function disconnect() {
  state.wanted = false;
  clearTimeout(state.retry);
  const ws = state.ws;
  state.ws = null;
  if (ws) ws.close(1000);
  clearInterval(state.heartbeat);
  body.onClose();
  setUi("offline");
}

// --- odtwarzanie ---

function ensureAudio() {
  if (!state.audio) {
    state.audio = new AudioContext();
    state.gain = state.audio.createGain();
    state.gain.gain.value = state.volume;
    state.analyser = state.audio.createAnalyser();
    state.analyser.fftSize = 1024;
    state.levelBuf = new Float32Array(state.analyser.fftSize);
    state.gain.connect(state.analyser).connect(state.audio.destination);
  }
  state.audio.resume(); // wymaga gestu użytkownika (Połącz albo przycisk mówienia)
}

function rms(samples) {
  let sum = 0;
  for (const x of samples) sum += x * x;
  return Math.sqrt(sum / (samples.length || 1));
}

// Poziom dla fali: głos IRIS podczas mówienia, mikrofon podczas słuchania (0-1).
// Pierwiastek z RMS: cicha mowa też porusza falą, głośna nie wybija jej od razu do maksimum.
function currentLevel() {
  if (state.ui === "speaking" && state.analyser) {
    state.analyser.getFloatTimeDomainData(state.levelBuf);
    return Math.min(1, Math.sqrt(rms(state.levelBuf)) * 1.9);
  }
  if (state.ui === "listening") return Math.min(1, Math.sqrt(state.micLevel) * 2.4);
  return 0;
}

// Dekoduje pakiety Opus (brakujące to cisza).
async function decodePackets(packets) {
  const pieces = [];
  const decoder = new AudioDecoder({
    output: (data) => {
      const samples = new Float32Array(data.numberOfFrames);
      data.copyTo(samples, { planeIndex: 0, format: "f32-planar" });
      pieces.push({ timestamp: data.timestamp, rate: data.sampleRate, samples });
      data.close();
    },
    error: (e) => notice(`Błąd dekodowania mowy: ${e.message}`),
  });
  decoder.configure({ codec: "opus", sampleRate: RATE, numberOfChannels: 1 });
  packets.forEach((packet, seq) => {
    if (packet) decoder.decode(new EncodedAudioChunk({ type: "key", timestamp: seq * 20000, data: packet }));
  });
  await decoder.flush();
  decoder.close();
  // Każdy fragment na miejscu z jego znacznika czasu, więc brakujące pakiety zostają ciszą.
  const rate = pieces.length ? pieces[0].rate : RATE;
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
  const turn = state.replyTurn;
  state.replyTurn = null;
  setUi("speaking");
  if (turn) revealWithSpeech(turn, duration);
  await new Promise((resolve) => {
    source.onended = resolve;
    source.start();
    setTimeout(resolve, duration * 1000 + 500); // gdyby kontekst był wstrzymany
  });
  if (turn) revealAll(turn);
  state.played.push({ audioId, packets: packets.length, duration, sampleRate: rate });
  setUi(restState());
}

// --- mikrofon ---

function newEncoder() {
  const encoder = new AudioEncoder({
    output: (chunk) => {
      const bytes = new Uint8Array(chunk.byteLength);
      chunk.copyTo(bytes);
      body.micPacket(bytes);
    },
    error: (e) => notice(`Błąd kodowania mowy: ${e.message}`),
  });
  encoder.configure({ codec: "opus", sampleRate: RATE, numberOfChannels: 1, bitrate: 24000, opus: { frameDuration: 20000 } });
  return encoder;
}

// Wypowiedź z dowolnego źródła PCM 16 kHz: start, pakiety po 20 ms, koniec po opróżnieniu kodera.
function beginUtterance() {
  const uttId = body.utteranceStart();
  if (!uttId) return null;
  turnFor(uttId);
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
    state.micLevel = rms(ev.data);
    if (state.talk) pushSamples(state.talk, ev.data);
  };
  state.mic = { stream, ctx, node };
  return state.mic;
}

async function startTalking() {
  if (state.held || state.talk || $("talk").disabled) return;
  state.held = true;
  ensureAudio();
  try {
    const mic = await openMic();
    await mic.ctx.resume();
  } catch (e) {
    state.held = false;
    notice(`Brak dostępu do mikrofonu (${e.message}). Zezwól na mikrofon w ustawieniach przeglądarki.`);
    return;
  }
  if (!state.held) return; // puszczony, zanim przeglądarka dała mikrofon
  state.talk = beginUtterance();
  if (!state.talk) return;
  $("talk").setAttribute("aria-pressed", "true");
  setUi("listening");
  scrollToLatest();
}

async function stopTalking() {
  state.held = false;
  const talk = state.talk;
  if (!talk) return;
  state.talk = null;
  $("talk").setAttribute("aria-pressed", "false");
  setUi("thinking");
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

function openSettings() {
  $("settings").showModal();
  $("device").focus();
}

function init() {
  const saved = store.get("iris.device", null);
  $("device").value = saved ?? "";
  $("token").value = store.get("iris.token", "");
  $("open-settings").addEventListener("click", openSettings);
  $("close-settings").addEventListener("click", () => $("settings").close());
  $("settings-form").addEventListener("submit", (e) => {
    e.preventDefault();
    if (connect()) $("settings").close();
  });
  $("disconnect").addEventListener("click", () => {
    disconnect();
    $("settings").close();
  });
  $("reconnect").addEventListener("click", () => (validDeviceId($("device").value.trim()) ? connect() : openSettings()));
  $("activate").addEventListener("click", () => body.requestActive());
  const talk = $("talk");
  talk.addEventListener("pointerdown", (e) => {
    talk.setPointerCapture(e.pointerId);
    startTalking();
  });
  for (const ev of ["pointerup", "pointercancel"]) talk.addEventListener(ev, stopTalking);
  talk.addEventListener("contextmenu", (e) => e.preventDefault()); // długie przytrzymanie na telefonie
  talk.addEventListener("keydown", (e) => {
    if ((e.code === "Space" || e.code === "Enter") && !e.repeat) {
      e.preventDefault();
      startTalking();
    }
  });
  talk.addEventListener("keyup", (e) => {
    if (e.code === "Space" || e.code === "Enter") stopTalking();
  });
  if (!supported) $("unsupported").hidden = false;
  if ("serviceWorker" in navigator && window.isSecureContext) {
    navigator.serviceWorker.register("sw.js").catch(() => null); // bez instalacji, klient działa dalej
  }
  watchBattery();
  document.addEventListener("visibilitychange", visibilityChanged);
  render();
  if (saved && validDeviceId(saved)) connect(); // ponowne otwarcie aplikacji łączy od razu
  else openSettings();
}

// Do testów automatycznych (Playwright): ten sam tor co mikrofon, ale z podanego PCM 16 kHz.
window.iris = {
  body,
  state,
  async sayPcm(samples) {
    const talk = beginUtterance();
    if (!talk) return 0;
    setUi("listening");
    pushSamples(talk, Float32Array.from(samples));
    setUi("thinking");
    await endUtterance(talk);
    return talk.uttId;
  },
};

init();
