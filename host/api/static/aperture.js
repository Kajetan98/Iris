// Przysłona IRIS na canvas. Otwiera się według stanu i poziomu dźwięku (głos IRIS albo mikrofon).
// Siedem listków, otwór obraca się przy otwieraniu jak w obiektywie; w otworze powłoka soczewki.

const BLADES = 7;
const TAU = Math.PI * 2;

// Cel dla stanu: open (0-1), gain (wpływ poziomu dźwięku), breathe (powolne pulsowanie),
// sheen (widoczność powłoki), spin (obrót powłoki, obr/s), dim (przygaszenie).
const STATES = {
  offline: { open: 0.0, gain: 0, breathe: 0, sheen: 0, spin: 0, dim: 1 },
  connecting: { open: 0.07, gain: 0, breathe: 0.05, sheen: 0.1, spin: 0.05, dim: 0.6 },
  inactive: { open: 0.1, gain: 0, breathe: 0, sheen: 0.15, spin: 0, dim: 0.7 },
  idle: { open: 0.24, gain: 0, breathe: 0, sheen: 0.35, spin: 0, dim: 0 },
  listening: { open: 0.3, gain: 0.6, breathe: 0, sheen: 0.5, spin: 0.08, dim: 0 },
  thinking: { open: 0.27, gain: 0, breathe: 0.07, sheen: 0.6, spin: 0.25, dim: 0 },
  speaking: { open: 0.32, gain: 0.62, breathe: 0, sheen: 0.85, spin: 0.35, dim: 0 },
};
// Bez ruchu: stałe otwarcie dla stanu, bez pulsowania, obrotu i śledzenia dźwięku.
const STILL_OPEN = { listening: 0.55, speaking: 0.6 };

function clamp(x, lo, hi) {
  return Math.min(hi, Math.max(lo, x));
}

export class Aperture {
  constructor(canvas, { level = () => 0, reducedMotion = false } = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.level = level;
    this.reducedMotion = reducedMotion;
    this.state = "offline";
    this.open = 0;
    this.sheen = 0;
    this.dim = 1;
    this.angle = 0;
    this.smoothLevel = 0;
    this.time = 0;
    this.frame = null;
    this.last = 0;
    this.readColors();
    new ResizeObserver(() => this.resize()).observe(canvas);
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
      this.readColors();
      this.request();
    });
    this.resize();
  }

  readColors() {
    const css = getComputedStyle(document.documentElement);
    const v = (name) => css.getPropertyValue(name).trim();
    this.colors = { blade: v("--blade"), seam: v("--seam"), mist: v("--mist") };
  }

  resize() {
    const dpr = window.devicePixelRatio || 1;
    const size = Math.round(this.canvas.clientWidth * dpr);
    if (size > 0 && this.canvas.width !== size) {
      this.canvas.width = size;
      this.canvas.height = size;
    }
    this.request();
  }

  setState(state) {
    this.state = STATES[state] ? state : "idle";
    this.request();
  }

  target() {
    const t = STATES[this.state];
    if (this.reducedMotion) {
      return { ...t, open: STILL_OPEN[this.state] ?? t.open, gain: 0, breathe: 0, spin: 0 };
    }
    return t;
  }

  // Pętla działa tylko, gdy coś się rusza (oszczędza baterię telefonu).
  request() {
    if (this.frame === null) {
      this.last = performance.now();
      this.frame = requestAnimationFrame((t) => this.step(t));
    }
  }

  step(now) {
    this.frame = null;
    const dt = Math.min(0.1, (now - this.last) / 1000);
    this.last = now;
    this.time += dt;
    const t = this.target();
    const raw = t.gain > 0 ? clamp(this.level(), 0, 1) : 0;
    this.smoothLevel += (raw - this.smoothLevel) * (1 - Math.exp(-dt * (raw > this.smoothLevel ? 30 : 9)));
    const breathe = t.breathe * Math.sin(this.time * TAU * 0.4);
    const goal = clamp(t.open + breathe + t.gain * this.smoothLevel, 0, 1);
    const k = this.reducedMotion ? 1 : 1 - Math.exp(-dt * 7);
    this.open += (goal - this.open) * k;
    this.sheen += (t.sheen - this.sheen) * k;
    this.dim += (t.dim - this.dim) * k;
    this.angle += t.spin * TAU * dt;
    this.draw();
    const settled = Math.abs(goal - this.open) < 0.002 && Math.abs(t.sheen - this.sheen) < 0.01;
    const moving = !this.reducedMotion && (t.gain > 0 || t.breathe > 0 || t.spin > 0);
    if (moving || !settled) this.request();
  }

  draw() {
    const { ctx, canvas, colors } = this;
    const s = canvas.width;
    if (!s) return;
    const c = s / 2;
    const R = s * 0.47;
    ctx.clearRect(0, 0, s, s);

    // listki: tarcza z cieniowaniem od środka
    const shade = ctx.createRadialGradient(c, c, R * 0.1, c, c, R);
    shade.addColorStop(0, colors.blade);
    shade.addColorStop(1, colors.seam);
    ctx.beginPath();
    ctx.arc(c, c, R, 0, TAU);
    ctx.fillStyle = shade;
    ctx.fill();

    // otwór: wielokąt obracający się przy otwieraniu
    const r = R * (0.05 + 0.74 * this.open);
    const phase = -Math.PI / 2 + this.open * 0.95;
    const pts = Array.from({ length: BLADES }, (_, k) => {
      const a = phase + (TAU * k) / BLADES;
      return [c + r * Math.cos(a), c + r * Math.sin(a), a];
    });

    // krawędzie listków: od wierzchołka otworu stycznie do obudowy
    ctx.strokeStyle = colors.mist;
    ctx.globalAlpha = 0.35 * (1 - this.dim * 0.5);
    ctx.lineWidth = Math.max(1, s / 260);
    for (const [x, y, a] of pts) {
      const dx = Math.cos(a + Math.PI / 2 - 0.42);
      const dy = Math.sin(a + Math.PI / 2 - 0.42);
      const px = x - c;
      const py = y - c;
      const b = px * dx + py * dy;
      const t = -b + Math.sqrt(b * b - (px * px + py * py - R * R));
      ctx.beginPath();
      ctx.moveTo(x, y);
      ctx.lineTo(x + dx * t, y + dy * t);
      ctx.stroke();
    }
    ctx.globalAlpha = 1;

    ctx.save();
    ctx.beginPath();
    pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
    ctx.closePath();
    ctx.clip();
    // soczewka: ciemna głębia
    const lens = ctx.createRadialGradient(c, c, 0, c, c, Math.max(r, 1));
    lens.addColorStop(0, "#06090d");
    lens.addColorStop(1, "#1b2633");
    ctx.fillStyle = lens;
    ctx.fillRect(c - r, c - r, 2 * r, 2 * r);
    // powłoka: zielono-fioletowo-bursztynowy odblask, jak na prawdziwej soczewce
    if (this.sheen > 0.01 && ctx.createConicGradient) {
      const coat = ctx.createConicGradient(this.angle, c, c);
      coat.addColorStop(0, "rgba(62,150,128,0.0)");
      coat.addColorStop(0.18, "rgba(62,150,128,0.9)");
      coat.addColorStop(0.42, "rgba(118,96,182,0.8)");
      coat.addColorStop(0.66, "rgba(214,150,66,0.85)");
      coat.addColorStop(0.9, "rgba(62,150,128,0.0)");
      ctx.globalAlpha = this.sheen * 0.55 * (1 - this.dim);
      ctx.fillStyle = coat;
      ctx.fillRect(c - r, c - r, 2 * r, 2 * r);
      // odblask w środku zostawia głębię
      const core = ctx.createRadialGradient(c, c, 0, c, c, r);
      core.addColorStop(0, "rgba(6,9,13,0.95)");
      core.addColorStop(0.55, "rgba(6,9,13,0.55)");
      core.addColorStop(1, "rgba(6,9,13,0)");
      ctx.globalAlpha = 1;
      ctx.fillStyle = core;
      ctx.fillRect(c - r, c - r, 2 * r, 2 * r);
    }
    ctx.restore();

    // obudowa
    ctx.beginPath();
    ctx.arc(c, c, R, 0, TAU);
    ctx.strokeStyle = colors.seam;
    ctx.lineWidth = Math.max(1.5, s / 90);
    ctx.stroke();
  }
}
