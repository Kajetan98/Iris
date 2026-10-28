// Głos IRIS jako świecąca fala na canvas. Amplituda z poziomu dźwięku: głos IRIS, gdy mówi,
// mikrofon, gdy słucha. Kilka pasm o różnych fazach, zwężonych na końcach; kolory się sumują.

const TAU = Math.PI * 2;
const SAMPLES = 120;
const SPAN = 0.88; // część szerokości zajęta przez falę
const MAX_DPR = 1.5; // miękkie, świecące linie nie potrzebują pełnej rozdzielczości ekranu
const SLOW_FRAME_MS = 32; // bez dźwięku wystarczy ok. 30 klatek na sekundę

// Cel dla stanu: amp (wysokość bez dźwięku, 0-1), gain (wpływ poziomu dźwięku), speed (tempo
// ruchu), spread (rozejście pasm; 0 to jedna linia), warm (0: chłodne kolory słuchania, 1: pełna
// paleta mowy IRIS), light (jasność).
const STATES = {
  offline: { amp: 0, gain: 0, speed: 0, spread: 0.05, warm: 0.3, light: 0.3 },
  connecting: { amp: 0.04, gain: 0, speed: 0.5, spread: 0.3, warm: 0.3, light: 0.45 },
  inactive: { amp: 0.03, gain: 0, speed: 0.25, spread: 0.25, warm: 0.4, light: 0.4 },
  idle: { amp: 0.09, gain: 0, speed: 0.35, spread: 0.55, warm: 0.65, light: 0.8 },
  listening: { amp: 0.1, gain: 0.8, speed: 0.8, spread: 0.6, warm: 0, light: 1 },
  thinking: { amp: 0.16, gain: 0, speed: 1.7, spread: 0.85, warm: 0.6, light: 0.9 },
  speaking: { amp: 0.26, gain: 0.8, speed: 1.15, spread: 1, warm: 1, light: 1 },
};
// Bez ruchu: stała wysokość dla stanu, bez śledzenia dźwięku.
const STILL_AMP = { listening: 0.45, thinking: 0.3, speaking: 0.7 };

// Pasma: w (grubość w px CSS), a (jasność), k (dwie częstotliwości wzdłuż fali), p (fazy),
// s (prędkości), amp (udział w wysokości), warm/cool (kolory od lewej do prawej), dots (kropki).
const STRANDS = [
  { w: 7, a: 0.28, k: [1.7, 3.2], p: [0.4, 2.0], s: [0.7, 0.5], amp: 0.75,
    warm: ["#ff8a5c", "#c45cff", "#5b6cff", "#8b5cf6", "#ff7a9a"], cool: ["#4f7dff", "#3d6bff", "#5b8cff", "#4f7dff", "#7aa8ff"] },
  { w: 3.4, a: 0.95, k: [1.9, 3.6], p: [0.2, 1.4], s: [1.0, 0.6], amp: 1,
    warm: ["#ff9466", "#7c5cff", "#3d7bff", "#5b7cff", "#ff7a8a"], cool: ["#6aa8ff", "#4f7dff", "#3d7bff", "#5b8cff", "#8fbaff"] },
  { w: 2.4, a: 0.85, k: [2.2, 3.1], p: [1.1, 0.3], s: [0.8, 0.9], amp: 0.6,
    warm: ["#ff7ab0", "#a855f7", "#6d5dfc", "#3b82f6", "#c084fc"], cool: ["#7f9cff", "#6d8bff", "#4f7dff", "#3b82f6", "#9db8ff"] },
  { w: 2.2, a: 0.85, k: [1.6, 4.1], p: [2.3, 0.9], s: [0.9, 0.7], amp: 1.1,
    warm: ["#c05cff", "#8b5cf6", "#ffd84a", "#ffb347", "#ff6fa0"], cool: ["#9db8ff", "#6d8bff", "#bcd2ff", "#8fb3ff", "#7aa8ff"] },
  { w: 1.5, a: 0.9, k: [2.0, 2.8], p: [0.8, 2.6], s: [1.1, 0.8], amp: 0.5,
    warm: ["#ffd6c2", "#ffffff", "#f3e8ff", "#ffffff", "#ffd1dc"], cool: ["#dbe7ff", "#ffffff", "#eef3ff", "#ffffff", "#dbe7ff"] },
  { w: 1.6, a: 0.95, k: [2.4, 3.4], p: [1.7, 1.2], s: [0.7, 1.0], amp: 1.15, dots: true,
    warm: ["#ffe4d6", "#ffffff", "#ffffff", "#ffffff", "#ffd1dc"], cool: ["#e3edff", "#ffffff", "#ffffff", "#ffffff", "#e3edff"] },
  { w: 2.8, a: 0.7, k: [1.8, 3.8], p: [2.9, 0.1], s: [0.6, 1.1], amp: 0.85,
    warm: ["#fb7185", "#ff8a5c", "#f472b6", "#ff8a5c", "#fb923c"], cool: ["#5b8cff", "#7aa8ff", "#6d8bff", "#7aa8ff", "#5b8cff"] },
];

const STOPS = [0.08, 0.3, 0.52, 0.72, 0.92];

function clamp(x, lo, hi) {
  return Math.min(hi, Math.max(lo, x));
}

function rgb(hex) {
  const n = parseInt(hex.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

for (const s of STRANDS) {
  s.warmRgb = s.warm.map(rgb);
  s.coolRgb = s.cool.map(rgb);
}

// Obwiednia: zero na końcach, największa wysokość trochę na prawo od środka (jak w nagraniu mowy).
function envelope(u) {
  const taper = Math.pow(Math.sin(Math.PI * u), 2.4);
  const focus = Math.exp(-(((u - 0.56) / 0.21) ** 2));
  return taper * (0.28 + 0.72 * focus);
}

export class Wave {
  constructor(canvas, { level = () => 0, reducedMotion = false } = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.level = level;
    this.reducedMotion = reducedMotion;
    this.state = "offline";
    this.now = { ...STATES.offline };
    this.smoothLevel = 0;
    this.phase = 1.3; // czas fali (rośnie z tempem stanu)
    this.time = 0;
    this.frame = null;
    this.running = false;
    this.settled = false;
    this.last = 0;
    this.gradients = new Map(); // pasmo -> {key, gradient}
    this.u = Float64Array.from({ length: SAMPLES }, (_, i) => i / (SAMPLES - 1));
    this.env = this.u.map(envelope);
    this.thin = this.u.map((u) => Math.pow(Math.sin(Math.PI * u), 0.9)); // zwężenie grubości
    this.ys = new Float64Array(SAMPLES);
    // Iskry wokół lewego końca, jak rozproszone światło w paśmie.
    let seed = 7;
    const rand = () => ((seed = (seed * 16807) % 2147483647) / 2147483647);
    this.sparks = Array.from({ length: 46 }, () => ({ u: 0.06 + 0.42 * rand() ** 1.6, dy: rand() * 2 - 1, f: 0.6 + rand() * 2.2, ph: rand() * TAU }));
    new ResizeObserver(() => this.resize()).observe(canvas);
    this.resize();
  }

  resize() {
    const dpr = Math.min(MAX_DPR, window.devicePixelRatio || 1);
    const w = Math.round(this.canvas.clientWidth * dpr);
    const h = Math.round(this.canvas.clientHeight * dpr);
    if (w > 0 && h > 0 && (this.canvas.width !== w || this.canvas.height !== h)) {
      this.canvas.width = w;
      this.canvas.height = h;
    }
    this.dpr = dpr;
    this.request();
  }

  setState(state) {
    this.state = STATES[state] ? state : "idle";
    this.request();
  }

  target() {
    const t = STATES[this.state];
    if (this.reducedMotion) return { ...t, amp: STILL_AMP[this.state] ?? t.amp, gain: 0, speed: 0 };
    return t;
  }

  // Pętla działa tylko, gdy coś się rusza (oszczędza baterię telefonu).
  request() {
    if (this.frame !== null) return;
    if (!this.running) {
      this.running = true;
      this.last = performance.now();
    }
    this.frame = requestAnimationFrame((t) => this.step(t));
  }

  step(now) {
    this.frame = null;
    const t = this.target();
    // Bez śledzenia dźwięku i po zakończonym przejściu między stanami: co druga klatka.
    if (t.gain === 0 && this.settled && now - this.last < SLOW_FRAME_MS) {
      this.frame = requestAnimationFrame((n) => this.step(n));
      return;
    }
    const dt = Math.min(0.1, (now - this.last) / 1000);
    this.last = now;
    this.time += dt;
    const raw = t.gain > 0 ? clamp(this.level(), 0, 1) : 0;
    // Szybkie narastanie, wolniejsze opadanie: fala nadąża za sylabami, ale nie migocze.
    this.smoothLevel += (raw - this.smoothLevel) * (1 - Math.exp(-dt * (raw > this.smoothLevel ? 26 : 7)));
    const k = this.reducedMotion ? 1 : 1 - Math.exp(-dt * 5);
    let settled = true;
    for (const key of ["amp", "spread", "warm", "light", "speed"]) {
      this.now[key] += (t[key] - this.now[key]) * k;
      if (Math.abs(t[key] - this.now[key]) > 0.002) settled = false;
    }
    this.settled = settled;
    this.phase += dt * this.now.speed;
    this.draw();
    const moving = !this.reducedMotion && (t.speed > 0 || this.smoothLevel > 0.002);
    if (moving || !settled) this.request();
    else this.running = false;
  }

  // Gradient pasma od lewej do prawej; liczony od nowa tylko przy zmianie kolorów lub szerokości.
  gradient(n, strand, x0, span) {
    const key = `${Math.round(this.now.warm * 100)}:${x0}:${span}`;
    const cached = this.gradients.get(n);
    if (cached && cached.key === key) return cached.gradient;
    const warm = Math.round(this.now.warm * 100) / 100;
    const g = this.ctx.createLinearGradient(x0, 0, x0 + span, 0);
    g.addColorStop(0, "rgba(0,0,0,0)");
    STOPS.forEach((at, i) => {
      const w = strand.warmRgb[i];
      const c = strand.coolRgb[i];
      const mix = (j) => Math.round(c[j] + (w[j] - c[j]) * warm);
      g.addColorStop(at, `rgb(${mix(0)},${mix(1)},${mix(2)})`);
    });
    g.addColorStop(1, "rgba(0,0,0,0)");
    this.gradients.set(n, { key, gradient: g });
    return g;
  }

  draw() {
    const { ctx, canvas, dpr } = this;
    const W = canvas.width;
    const H = canvas.height;
    if (!W || !H) return;
    const s = this.now;
    const height = clamp(s.amp + this.target().gain * this.smoothLevel, 0, 1);
    const x0 = (W * (1 - SPAN)) / 2;
    const span = W * SPAN;
    const cy = H / 2;
    const A = H * 0.46 * height;
    ctx.clearRect(0, 0, W, H);
    ctx.globalCompositeOperation = "lighter";
    // poświata tła jest w CSS (.presence), żeby nie malować całego canvas w każdej klatce

    const base = STRANDS[1];
    STRANDS.forEach((strand, n) => {
      const color = this.gradient(n, strand, x0, span);
      for (let i = 0; i < SAMPLES; i++) {
        const u = this.u[i];
        const own = 0.62 * Math.sin(TAU * strand.k[0] * u + strand.p[0] + strand.s[0] * this.phase) +
          0.38 * Math.sin(TAU * strand.k[1] * u + strand.p[1] - strand.s[1] * this.phase);
        const common = 0.62 * Math.sin(TAU * base.k[0] * u + base.p[0] + base.s[0] * this.phase) +
          0.38 * Math.sin(TAU * base.k[1] * u + base.p[1] - base.s[1] * this.phase);
        const scale = 1 + (strand.amp - 1) * s.spread;
        this.ys[i] = cy + A * this.env[i] * scale * (common + (own - common) * s.spread);
      }
      const alpha = strand.a * s.light; // przez globalAlpha, gradient zostaje w pamięci
      const line = () => {
        ctx.beginPath();
        for (let i = 0; i < SAMPLES; i++) {
          const x = x0 + this.u[i] * span;
          if (i) ctx.lineTo(x, this.ys[i]);
          else ctx.moveTo(x, this.ys[i]);
        }
      };
      ctx.lineCap = "round";
      ctx.lineJoin = "round";
      if (strand.dots) {
        line();
        ctx.setLineDash([0.001, 4.2 * dpr]);
        ctx.lineWidth = strand.w * dpr;
        ctx.strokeStyle = color;
        ctx.globalAlpha = alpha;
        ctx.stroke();
        ctx.setLineDash([]);
        return;
      }
      // poświata: jedno szerokie, słabe pociągnięcie pod rdzeniem
      line();
      ctx.lineWidth = Math.min(strand.w * 3.4, 14) * dpr;
      ctx.strokeStyle = color;
      ctx.globalAlpha = alpha * 0.16;
      ctx.stroke();
      // rdzeń: wstęga zwężona na końcach
      ctx.beginPath();
      for (let i = 0; i < SAMPLES; i++) {
        const x = x0 + this.u[i] * span;
        const half = (strand.w * dpr * this.thin[i]) / 2;
        if (i) ctx.lineTo(x, this.ys[i] - half);
        else ctx.moveTo(x, this.ys[i] - half);
      }
      for (let i = SAMPLES - 1; i >= 0; i--) {
        ctx.lineTo(x0 + this.u[i] * span, this.ys[i] + (strand.w * dpr * this.thin[i]) / 2);
      }
      ctx.closePath();
      ctx.fillStyle = color;
      ctx.globalAlpha = alpha;
      ctx.fill();
    });
    ctx.globalAlpha = 1;

    // iskry przy lewym końcu, migoczą tylko w ruchu
    for (const p of this.sparks) {
      const i = Math.round(p.u * (SAMPLES - 1));
      const flicker = this.reducedMotion ? 0.6 : 0.5 + 0.5 * Math.sin(this.time * TAU * p.f * 0.5 + p.ph);
      const a = 0.55 * s.light * flicker * (0.3 + 0.7 * s.spread);
      if (a < 0.02) continue;
      const x = x0 + p.u * span;
      const y = this.ys[i] + p.dy * (2 + 6 * this.thin[i]) * dpr * (0.4 + s.spread);
      ctx.fillStyle = `rgba(255,${190 + Math.round(50 * (1 - s.warm))},${220},${a})`;
      ctx.fillRect(x, y, 1.4 * dpr, 1.4 * dpr);
    }
    ctx.globalCompositeOperation = "source-over";
  }
}
