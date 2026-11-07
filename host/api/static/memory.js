// Widok "Pamięć IRIS" (docs/spec.md, sekcja 1): graf pojęć z hosta jako obracana kula.
// Dane: GET /memory, potem strumień /memory/stream (te same podprotokoły z tokenem co /ws).
// Adres hosta i token pochodzą z ustawień klienta (localStorage). Graf nie jest zapisywany w przeglądarce.
// Rysowanie kuli pochodzi z paczki iris-memory (iris_memory/web/memory.html).
import { parseHostAddress } from "./protocol.js";

const $ = (s) => document.querySelector(s);
const canvas = $("#orb"), ctx = canvas.getContext("2d"), stage = $("#stage"), tip = $("#tip"), say = $("#say");
const still = matchMedia("(prefers-reduced-motion: reduce)");

function setting(key) {
  try {
    return localStorage.getItem(key) ?? "";
  } catch {
    return "";
  }
}
const TOKEN = setting("iris.token").trim();
const BASE = parseHostAddress(setting("iris.host"), location.protocol).base || location.origin;
const headers = (extra = {}) => (TOKEN ? { Authorization: `Bearer ${TOKEN}`, ...extra } : extra);

// ---- stan grafu z hosta ----
let HALF = 14 * 864e5, offset = 0, forgot = 0, focus = null;
let DOMAINS = [
  { id: "dom", name: "Dom", info: "dom i sterowanie urządzeniami", rgb: [255, 138, 31] },
  { id: "czas", name: "Czas", info: "przypomnienia, kalendarz, plany", rgb: [255, 61, 154] },
  { id: "ty", name: "Ty", info: "ludzie, upodobania, praca, zajęcia", rgb: [42, 212, 255] },
  { id: "rozmowa", name: "Rozmowa", info: "wszystko inne", rgb: [143, 91, 255] },
];
let DOM = Object.fromEntries(DOMAINS.map((d) => [d.id, d]));
const nodes = new Map(), edges = new Map();
const nowMs = () => Date.now() + offset;
const eff = (o) => o.s * Math.pow(0.5, Math.max(0, nowMs() - o.t) / HALF);
const colorOf = (n) => {
  let r = 0, g = 0, b = 0, w = 0;
  for (const id in n.dom) {
    const c = (DOM[id] || DOM.rozmowa).rgb, k = n.dom[id];
    r += c[0] * k; g += c[1] * k; b += c[2] * k; w += k;
  }
  return w ? [r / w, g / w, b / w] : DOM.rozmowa.rgb;
};
// Domena główna jak na hoście: najwięcej użyć, przy remisie wcześniejsza na liście.
const mainOf = (n) => {
  let best = "rozmowa", most = 0;
  for (const d of DOMAINS) if ((n.dom[d.id] || 0) > most) { best = d.id; most = n.dom[d.id]; }
  return best;
};

const rnd = (a = 1) => (Math.random() - 0.5) * 2 * a;
function spawn() { const v = [rnd(), rnd(), rnd()], l = Math.hypot(...v) || 1; return v.map((x) => x / l); }
function blob() { const o = []; for (let i = 0; i < 52; i++) { const v = [rnd(), rnd(), rnd()], l = Math.hypot(...v) || 1, r = Math.cbrt(Math.random()); o.push({ d: v.map((x) => (x / l) * r), ph: Math.random() * 6.283 }); } return o; }
function addNode(key, label, s, t, hits, dom) { const n = { key, label, s, t, hits, dom: dom || {}, p: spawn(), v: [0, 0, 0], flash: 1, offs: blob() }; nodes.set(key, n); return n; }

function applySnapshot(snap, quiet) {
  offset = Date.parse(snap.now) - Date.now();
  HALF = snap.params.half_life_days * 864e5;
  forgot = snap.forgotten || 0;
  if (Array.isArray(snap.domains) && snap.domains.length) {
    DOMAINS = snap.domains;
    DOM = Object.fromEntries(DOMAINS.map((d) => [d.id, d]));
  }
  const keep = new Set();
  for (const n of snap.nodes) {
    keep.add(n.key);
    const t = Date.parse(n.last_used), o = nodes.get(n.key);
    if (o) { if (t > o.t + 1) o.flash = 1; o.label = n.label; o.s = n.strength; o.t = t; o.hits = n.hits; o.dom = n.contexts || {}; }
    else { const x = addNode(n.key, n.label, n.strength, t, n.hits, n.contexts || {}); x.flash = quiet ? 0 : 1; }
  }
  for (const k of [...nodes.keys()]) if (!keep.has(k)) nodes.delete(k);
  const ek = new Set();
  for (const l of snap.links) {
    const k = l.a + "|" + l.b, t = Date.parse(l.last_used), e = edges.get(k);
    ek.add(k);
    if (e) { if (t > e.t + 1) e.flash = 1; e.s = l.strength; e.t = t; }
    else edges.set(k, { a: l.a, b: l.b, s: l.strength, t, flash: quiet ? 0 : 1 });
  }
  for (const k of [...edges.keys()]) if (!ek.has(k)) edges.delete(k);
  $("#clock").textContent = `Siła maleje o połowę co ${String(snap.params.half_life_days).replace(".", ",")} dni bez użycia.`;
  state.snapshots += 1;
  refresh();
}

// ---- host: REST i strumień ----
const state = { connected: false, snapshots: 0, ws: null, backoff: 1000 };

async function api(method, path, body) {
  const res = await fetch(BASE + path, {
    method,
    headers: headers(body ? { "Content-Type": "application/json" } : {}),
    body: body ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401) throw new Error("host odrzucił token; wpisz go w ustawieniach rozmowy");
  if (!res.ok && res.status !== 204) throw new Error(`odpowiedź ${res.status}`);
  return res.status === 204 ? null : res.json();
}

function streamUrl() {
  const url = new URL(BASE);
  return `${url.protocol === "https:" ? "wss:" : "ws:"}//${url.host}/memory/stream`;
}

function connect() {
  let ws;
  try {
    ws = new WebSocket(streamUrl(), TOKEN ? ["iris", `token.${TOKEN}`] : ["iris"]);
  } catch {
    retry();
    return;
  }
  state.ws = ws;
  ws.onopen = () => { state.connected = true; state.backoff = 1000; };
  ws.onmessage = (m) => {
    try {
      applySnapshot(JSON.parse(m.data));
    } catch {
      /* uszkodzona ramka: następna przyjdzie przy kolejnej zmianie */
    }
  };
  ws.onclose = (e) => {
    state.connected = false;
    if (e.code === 4003) {
      say.textContent = "Host odrzucił token. Wpisz go w ustawieniach rozmowy i wróć tutaj.";
      return;
    }
    retry();
  };
}

function retry() {
  setTimeout(connect, state.backoff);
  state.backoff = Math.min(state.backoff * 2, 15000);
}

// ---- układ: silne pojęcia blisko środka, słabe na brzegu ----
function relax(dt) {
  const arr = [...nodes.values()], E = arr.map(eff);
  for (let i = 0; i < arr.length; i++) {
    const a = arr[i];
    for (let j = i + 1; j < arr.length; j++) {
      const b = arr[j];
      const dx = a.p[0] - b.p[0], dy = a.p[1] - b.p[1], dz = a.p[2] - b.p[2], d2 = dx * dx + dy * dy + dz * dz + 0.02, f = 0.0035 / d2, d = Math.sqrt(d2);
      for (let k = 0; k < 3; k++) { const u = ([dx, dy, dz][k] / d) * f; a.v[k] += u; b.v[k] -= u; }
    }
    const r = Math.hypot(...a.p) || 1, target = 0.82 - 0.6 * E[i], pull = (r - target) * 2.2;
    for (let k = 0; k < 3; k++) a.v[k] -= (a.p[k] / r) * pull * dt;
  }
  for (const e of edges.values()) {
    const a = nodes.get(e.a), b = nodes.get(e.b);
    if (!a || !b) continue;
    const w = eff(e), dx = b.p[0] - a.p[0], dy = b.p[1] - a.p[1], dz = b.p[2] - a.p[2], d = Math.hypot(dx, dy, dz) || 1, rest = 0.5 - 0.3 * w, f = (d - rest) * (0.9 + 2 * w) * dt;
    const u = [dx / d, dy / d, dz / d];
    for (let k = 0; k < 3; k++) { a.v[k] += u[k] * f; b.v[k] -= u[k] * f; }
  }
  const damp = Math.pow(0.04, dt);
  for (const n of arr) { for (let k = 0; k < 3; k++) { n.v[k] *= damp; n.p[k] += n.v[k] * dt; } const r = Math.hypot(...n.p); if (r > 1.12) for (let k = 0; k < 3; k++) n.p[k] *= 1.12 / r; }
}

// ---- rysowanie ----
const sprite = (rgb) => { const c = document.createElement("canvas"); c.width = c.height = 32; const g = c.getContext("2d"), gr = g.createRadialGradient(16, 16, 0, 16, 16, 16); gr.addColorStop(0, `rgba(${rgb},1)`); gr.addColorStop(0.25, `rgba(${rgb},.55)`); gr.addColorStop(1, `rgba(${rgb},0)`); g.fillStyle = gr; g.fillRect(0, 0, 32, 32); return c; };
const SP = [sprite("255,255,255")], CACHE = new Map();
const spriteFor = (c) => { const q = c.map((v) => Math.round(v / 12) * 12), key = q.join(","); if (!CACHE.has(key)) CACHE.set(key, sprite(key)); return CACHE.get(key); };
const rgba = (c, a) => `rgba(${c.map(Math.round).join(",")},${a.toFixed(3)})`;
const dust = Array.from({ length: 520 }, (_, i) => { const v = spawn(), r = 0.95 + Math.random() * 0.14; return { p: v.map((x) => x * r), ph: Math.random() * 6.283, i: i % 4 }; });
let W = 1, H = 1, dpr = 1, yaw = 0.6, pitch = 0.35, dragging = false, last = 0, clock = 0, spin = 0, hover = null, pointer = null;
function fit() { const r = stage.getBoundingClientRect(); dpr = Math.min(devicePixelRatio || 1, 1.5); W = Math.max(1, Math.round(r.width * dpr)); H = Math.max(1, Math.round(r.height * dpr)); canvas.width = W; canvas.height = H; }
new ResizeObserver(fit).observe(stage); fit();
function project(p, R) {
  const cy = Math.cos(yaw + spin), sy = Math.sin(yaw + spin), cp = Math.cos(pitch), sp = Math.sin(pitch);
  const x1 = p[0] * cy + p[2] * sy, z1 = -p[0] * sy + p[2] * cy, y2 = p[1] * cp - z1 * sp, z2 = p[1] * sp + z1 * cp, s = 3.4 / (3.4 - z2);
  return [W / 2 + x1 * R * s, H / 2 + y2 * R * s, z2, s];
}
const domainRgb = (i) => (DOMAINS[i % DOMAINS.length] || DOM.rozmowa).rgb;
function frame(now) {
  const dt = last ? Math.min((now - last) / 1000, 1 / 30) : 1 / 60; last = now; clock += dt;
  if (!dragging && !still.matches) spin += dt * 0.12;
  relax(dt);
  if (Math.floor(clock * 2) !== Math.floor((clock - dt) * 2)) refresh();
  const R = Math.min(W, H) * 0.37, k = dpr;
  ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.clearRect(0, 0, W, H); ctx.globalCompositeOperation = "lighter";
  const arr = [...nodes.values()], total = arr.reduce((s, n) => s + eff(n), 0), glow = Math.min(1, 0.3 + total / 6);
  const bs = R * (0.8 + 0.5 * glow), bg = ctx.createRadialGradient(W / 2, H / 2, 0, W / 2, H / 2, bs);
  bg.addColorStop(0, `rgba(255,255,255,${(0.22 * glow).toFixed(3)})`); bg.addColorStop(0.3, `rgba(170,110,255,${(0.12 * glow).toFixed(3)})`); bg.addColorStop(0.7, `rgba(60,90,255,${(0.04 * glow).toFixed(3)})`); bg.addColorStop(1, "rgba(40,40,255,0)");
  ctx.fillStyle = bg; ctx.fillRect(W / 2 - bs, H / 2 - bs, bs * 2, bs * 2);
  ctx.lineWidth = Math.max(1, k * 0.8);
  [[1.38, 0.2, 1.5], [1.38, 2.4, 1.1], [1.38, 4.3, 0.9], [1.55, 1.0, 0.6], [1.55, 3.7, 1.8]].forEach(([m, a0, len], i) => { const a = a0 + clock * (i % 2 ? -0.05 : 0.04); ctx.strokeStyle = rgba(domainRgb(i), 0.3); ctx.beginPath(); ctx.arc(W / 2, H / 2, R * m, a, a + len); ctx.stroke(); });
  for (const d of dust) { const q = project(d.p, R), f = 0.5 + 0.5 * q[2], al = (0.1 + 0.4 * f * f) * (0.7 + 0.3 * Math.sin(clock * 2 + d.ph)), sz = 1.4 * k * q[3]; ctx.globalAlpha = al * 0.6; ctx.drawImage(spriteFor(domainRgb(d.i)), q[0] - sz, q[1] - sz, sz * 2, sz * 2); }
  // włókna: połączenia pojęć
  ctx.lineWidth = Math.max(0.8, k * 0.8);
  for (const e of edges.values()) {
    const a = nodes.get(e.a), b = nodes.get(e.b); if (!a || !b) continue;
    const w = eff(e), mid = a.p.map((x, i) => (x + b.p[i]) / 2), ml = Math.hypot(...mid) || 1, c = mid.map((x) => x + (x / ml) * 0.22);
    const pa = project(a.p, R), pb = project(b.p, R), gr = ctx.createLinearGradient(pa[0], pa[1], pb[0], pb[1]);
    gr.addColorStop(0, rgba(colorOf(a), 1)); gr.addColorStop(0.5, "rgba(255,255,255,0.85)"); gr.addColorStop(1, rgba(colorOf(b), 1));
    const dim = focus && mainOf(a) !== focus && mainOf(b) !== focus ? 0.12 : 1;
    ctx.globalAlpha = Math.min(1, 0.12 + 0.6 * Math.sqrt(w) + 0.4 * e.flash) * dim; ctx.strokeStyle = gr; ctx.beginPath();
    let sparkAt = null;
    for (let i = 0; i <= 12; i++) { const t = i / 12, u = 1 - t, p = [0, 1, 2].map((q) => u * u * a.p[q] + 2 * u * t * c[q] + t * t * b.p[q]), s = project(p, R); if (i) ctx.lineTo(s[0], s[1]); else ctx.moveTo(s[0], s[1]); if (e.flash > 0.05 && Math.abs(t - (1 - e.flash)) < 0.05) sparkAt = s; }
    ctx.stroke();
    if (sparkAt) { const z = 7 * k; ctx.globalAlpha = 0.9; ctx.drawImage(SP[0], sparkAt[0] - z, sparkAt[1] - z, z * 2, z * 2); }
    e.flash = Math.max(0, e.flash - dt * 0.7);
  }
  // pojęcia jako chmury cząstek
  const proj = [];
  for (const n of arr) {
    const e = eff(n), q = project(n.p, R), se = Math.sqrt(e), cnt = 10 + Math.floor(40 * se), rad = (0.045 + 0.09 * se) * R * q[3];
    proj.push({ n, e, q });
    const fl = n.flash, col = colorOf(n), csp = spriteFor(col), dim = focus && mainOf(n) !== focus ? 0.12 : 1, cores = Math.round(1 + 6 * e);
    for (let i = 0; i < cnt; i++) {
      const o = n.offs[i], wob = still.matches ? 0 : Math.sin(clock * 1.4 + o.ph) * 0.12, px = q[0] + (o.d[0] * (1 + wob)) * rad, py = q[1] + (o.d[1] * (1 + wob)) * rad, f = 0.5 + 0.5 * q[2];
      const tw = 0.7 + 0.3 * Math.sin(clock * 3 + o.ph), sz = (1.9 + 3 * se * (i < 5 ? 1.5 : 1)) * k * q[3];
      const core = i < cores;
      ctx.globalAlpha = Math.min(1, ((0.3 + 0.7 * se) * (0.35 + 0.65 * f * f) * tw + fl * 0.5) * dim * (core ? 0.4 + 0.6 * e : 1));
      ctx.drawImage(core || fl > 0.5 ? SP[0] : csp, core ? q[0] + o.d[0] * rad * 0.25 - sz : px - sz, core ? q[1] + o.d[1] * rad * 0.25 - sz : py - sz, sz * 2, sz * 2);
    }
    if (fl > 0) { const z = rad * (2.2 + (1 - fl) * 3); ctx.globalAlpha = fl * 0.6; ctx.strokeStyle = rgba(col, 1); ctx.beginPath(); ctx.arc(q[0], q[1], z, 0, 6.283); ctx.stroke(); }
    n.flash = Math.max(0, fl - dt * 0.8);
  }
  // etykiety: najsilniejsze, świeżo użyte i wskazane (canvas rysuje tekst, nie HTML)
  ctx.globalCompositeOperation = "source-over"; ctx.font = `${Math.round(12 * k)}px "Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif`; ctx.textBaseline = "middle";
  proj.sort((a, b) => b.e - a.e);
  proj.forEach((o, i) => { const inFocus = !focus || mainOf(o.n) === focus, show = inFocus && ((i < 9 && o.e > 0.12) || o.n.flash > 0.2 || focus) || hover === o.n; if (!show) return; ctx.globalAlpha = hover === o.n ? 1 : Math.min(0.95, 0.45 + o.e); const lc = colorOf(o.n); ctx.fillStyle = rgba(lc.map((v) => v + (255 - v) * 0.55), 1); ctx.fillText(o.n.label, o.q[0] + (6 + 14 * o.e) * k, o.q[1] - 8 * k); });
  ctx.globalAlpha = 1;
  if (pointer && !dragging) {
    let best = null, bd = (22 * dpr) ** 2;
    for (const o of proj) { const d = (o.q[0] - pointer[0] * dpr) ** 2 + (o.q[1] - pointer[1] * dpr) ** 2; if (d < bd) { bd = d; best = o; } }
    hover = best ? best.n : null;
    if (best) {
      const days = (nowMs() - best.n.t) / 864e5;
      const ctxText = Object.entries(best.n.dom).map(([id, c]) => `${(DOM[id] || { name: id }).name.toLowerCase()} ${c}`).join(", ");
      tip.textContent = `${best.n.label}: siła ${best.e.toFixed(2).replace(".", ",")}, użyć ${best.n.hits}, ostatnio ${days < 0.05 ? "przed chwilą" : days.toFixed(1).replace(".", ",") + " dnia temu"}. Kontekst: ${ctxText}`;
      tip.style.left = Math.min(pointer[0] + 14, stage.clientWidth - 230) + "px"; tip.style.top = Math.max(8, pointer[1] - 10) + "px"; tip.style.opacity = 1;
    } else tip.style.opacity = 0;
  } else { hover = null; tip.style.opacity = 0; }
  raf = visible ? requestAnimationFrame(frame) : 0;
}
let raf = 0, visible = true;
new IntersectionObserver((en) => { visible = en[en.length - 1].isIntersecting; if (visible && !raf) { last = 0; raf = requestAnimationFrame(frame); } }).observe(stage);

// ---- obsługa ----
stage.addEventListener("pointerdown", (e) => { dragging = true; stage.setPointerCapture(e.pointerId); pointer = null; });
stage.addEventListener("pointermove", (e) => {
  const r = stage.getBoundingClientRect();
  if (dragging) { yaw += e.movementX * 0.008; pitch = Math.max(-1.2, Math.min(1.2, pitch + e.movementY * 0.008)); }
  else pointer = [e.clientX - r.left, e.clientY - r.top];
});
stage.addEventListener("pointerup", () => { dragging = false; });
stage.addEventListener("pointerleave", () => { pointer = null; });

const input = $("#in");
$("#teach").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  say.textContent = "IRIS zapamiętuje.";
  try {
    const r = await api("POST", "/memory/learn", { text });
    if (!r.fresh.length && !r.reinforced.length) {
      say.textContent = "Nie znalazłem w tym zdaniu niczego do zapamiętania (liczby nie są zapisywane).";
      return;
    }
    const parts = [];
    if (r.fresh.length) parts.push(`Nowe: ${r.fresh.join(", ")}.`);
    if (r.reinforced.length) parts.push(`Wzmocnione: ${r.reinforced.join(", ")}.`);
    parts.push(`Kontekst: ${(DOM[r.domain] || { name: r.domain }).name.toLowerCase()}.`);
    say.textContent = parts.join(" ");
    input.value = "";
    if (!state.connected) applySnapshot(await api("GET", "/memory"));
  } catch (err) {
    say.textContent = `Nie udało się zapamiętać (${err.message}).`;
  }
});

async function forget(key, label) {
  try {
    await api("DELETE", `/memory/concepts/${encodeURIComponent(key)}`);
    say.textContent = `Zapomniane: ${label}.`;
    if (!state.connected) applySnapshot(await api("GET", "/memory"));
  } catch (err) {
    say.textContent = `Nie udało się usunąć (${err.message}).`;
  }
}

$("#wipe").addEventListener("click", async () => {
  if (!nodes.size) { say.textContent = "Pamięć jest już pusta."; return; }
  if (!confirm(`Usunąć całą pamięć IRIS na hoście (${nodes.size} pojęć)? Tego nie da się cofnąć.`)) return;
  try {
    await api("DELETE", "/memory?confirm=true");
    say.textContent = "Pamięć wyczyszczona.";
    if (!state.connected) applySnapshot(await api("GET", "/memory"));
  } catch (err) {
    say.textContent = `Nie udało się wyczyścić (${err.message}).`;
  }
});

function refresh() {
  const arr = [...nodes.values()].map((n) => [n, eff(n)]).sort((a, b) => b[1] - a[1] || (a[0].key < b[0].key ? -1 : 1));
  const list = $("#list");
  list.textContent = "";
  arr.slice(0, 12).forEach(([n, e]) => {
    const li = document.createElement("li"), nm = document.createElement("span"), bar = document.createElement("span"), fill = document.createElement("b"), c = document.createElement("span"), del = document.createElement("button");
    nm.className = "nm"; nm.textContent = n.label;
    bar.className = "bar"; fill.style.width = Math.round(e * 100) + "%"; fill.style.background = rgba(colorOf(n), 1); bar.append(fill);
    c.className = "n"; c.textContent = "×" + n.hits;
    del.type = "button"; del.textContent = "Usuń"; del.setAttribute("aria-label", `Usuń pojęcie: ${n.label}`);
    del.addEventListener("click", () => forget(n.key, n.label));
    li.dataset.key = n.key;
    li.append(nm, bar, c, del);
    list.append(li);
  });
  const lg = $("#legend"), counts = {};
  for (const n of nodes.values()) { const m = mainOf(n); counts[m] = (counts[m] || 0) + 1; }
  if (lg.dataset.domains !== DOMAINS.map((d) => d.id).join(",")) {
    lg.textContent = "";
    lg.dataset.domains = DOMAINS.map((d) => d.id).join(",");
    DOMAINS.forEach((d) => {
      const b = document.createElement("button"), sw = document.createElement("span"), txt = document.createElement("span"), nm = document.createElement("span"), small = document.createElement("small"), ct = document.createElement("span");
      b.type = "button"; b.dataset.id = d.id; b.setAttribute("aria-pressed", "false");
      sw.className = "sw"; sw.style.color = rgba(d.rgb, 1);
      nm.className = "nm"; nm.textContent = d.name; small.textContent = d.info; txt.append(nm, small);
      ct.className = "ct";
      b.append(sw, txt, ct);
      b.addEventListener("click", () => { focus = focus === d.id ? null : d.id; lg.querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", String(x.dataset.id === focus))); });
      lg.append(b);
    });
    const core = document.createElement("div"), sw = document.createElement("span"), txt = document.createElement("span"), small = document.createElement("small");
    core.className = "core"; sw.className = "sw"; txt.textContent = "Biały rdzeń"; small.textContent = "siła zapamiętania: im jaśniejszy i większy, tym mocniej";
    txt.append(small); core.append(sw, txt); lg.append(core);
  }
  lg.querySelectorAll("button").forEach((b) => { b.querySelector(".ct").textContent = counts[b.dataset.id] || 0; });
  $("#empty").style.display = nodes.size ? "none" : "block";
  const stats = $("#stats");
  stats.textContent = "";
  const a = document.createElement("span"), b = document.createElement("span");
  a.textContent = `${nodes.size} pojęć, ${edges.size} połączeń`;
  b.textContent = forgot ? `zapomniane: ${forgot}` : "";
  stats.append(a, b);
  canvas.setAttribute("aria-label", `Wizualizacja pamięci IRIS: ${nodes.size} pojęć i ${edges.size} połączeń. Silniejsze pojęcia leżą bliżej środka.`);
}

async function start() {
  try {
    applySnapshot(await api("GET", "/memory"), true);
    for (let i = 0; i < 240; i++) relax(1 / 60);
    say.textContent = nodes.size ? "Połączono. Wskaż pojęcie, żeby zobaczyć szczegóły." : "Połączono. Pamięć jest jeszcze pusta.";
    connect();
  } catch (err) {
    say.textContent = `Nie udało się pobrać pamięci (${err.message}). Sprawdź adres hosta i token w ustawieniach rozmowy.`;
  }
}

refresh();
raf = requestAnimationFrame(frame);
start();
window.irisMemory = { nodes, edges, state };
