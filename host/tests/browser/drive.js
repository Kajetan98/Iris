// Scenariusze klienta przeglądarkowego dla host/tests/test_browser.py (Playwright, Chromium).
//   node drive.js <scenariusz> <adres hosta> '<json z opcjami>'
// Ostatnia linia stdout to wynik w JSON.
const { chromium } = require("playwright");

const [scenario, base, rawOpts] = process.argv.slice(2);
const opts = JSON.parse(rawOpts || "{}");

async function connect(page, device, token = "") {
  await page.goto(`${base}/app/`);
  await page.fill("#device", device);
  await page.fill("#token", token);
  await page.click("#connect");
}

async function waitText(page, selector, text, timeout = 10000) {
  await page.waitForFunction(
    ([sel, t]) => document.querySelector(sel).textContent.includes(t),
    [selector, text],
    { timeout },
  );
}

const scenarios = {
  // Reguły ack z protocol.js dla podanych ramek (porównanie z symulatorem po stronie pytest).
  async acks(page) {
    await page.goto(`${base}/app/`);
    return page.evaluate(async (frames) => {
      const { Body } = await import("/app/protocol.js");
      return frames.map((frame) => {
        const body = new Body({ io: { send: () => true, sendBinary: () => true, play: () => 0, vibrate: null } });
        const ack = body.onText(frame);
        return { ack, frameErrors: body.frameErrors };
      });
    }, opts.frames);
  },

  // Wypowiedź z PCM (ten sam tor co mikrofon), odpowiedź odtworzona w przeglądarce.
  async voice(page) {
    await connect(page, opts.device, opts.token);
    await waitText(page, "#status", "Połączono");
    await waitText(page, "#active", "Aktywne urządzenie");
    const t = Array.from({ length: 16000 * opts.seconds }, (_, i) => 0.4 * Math.sin((2 * Math.PI * opts.hz * i) / 16000));
    const uttId = await page.evaluate((samples) => window.iris.sayPcm(samples), t);
    await page.waitForFunction(() => window.iris.state.played.length >= 1, null, { timeout: 20000 });
    return page.evaluate((uttId) => {
      const { samples, rate } = window.iris.state.lastSamples;
      const step = Math.max(1, Math.round(rate / 16000));
      return {
        uttId,
        played: window.iris.state.played,
        rate: rate / step,
        samples: Array.from(samples.filter((_, i) => i % step === 0), (x) => Math.round(x * 32767)),
        caps: window.iris.body.caps(),
        log: Array.from(document.querySelectorAll("#log li"), (li) => li.textContent),
      };
    }, uttId);
  },

  // Prawdziwy mikrofon (sztuczne urządzenie Chromium): przycisk wciśnięty przez opts.ms.
  async mic(page) {
    await connect(page, opts.device, opts.token);
    await waitText(page, "#active", "Aktywne urządzenie");
    const box = await page.locator("#talk").boundingBox();
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await page.waitForTimeout(opts.ms);
    await page.mouse.up();
    await page.waitForTimeout(1500); // koniec kodowania i wysyłki
    return { classOn: await page.locator("#talk.on").count() };
  },

  // Urządzenie w spoczynku i przycisk "Używaj tego urządzenia".
  async switching(page) {
    await connect(page, opts.device, opts.token);
    await waitText(page, "#active", "W spoczynku");
    const talkDisabled = await page.locator("#talk").isDisabled();
    const before = await page.evaluate(() => window.iris.body.active);
    await page.click("#activate");
    await waitText(page, "#active", "Aktywne urządzenie");
    return { talkDisabled, before, after: await page.evaluate(() => window.iris.body.active), activateHidden: await page.locator("#activate").isHidden() };
  },

  // Samo połączenie: stan widoczny w interfejsie i możliwości wysłane do hosta.
  async connectOnly(page) {
    await connect(page, opts.device, opts.token);
    await waitText(page, "#status", "Połączono");
    await page.waitForTimeout(opts.stayMs || 0);
    return { status: await page.textContent("#status"), active: await page.textContent("#active"), caps: await page.evaluate(() => window.iris.body.caps()) };
  },

  // Zły token: kod 4003 i komunikat, bez połączenia.
  async badToken(page) {
    await connect(page, opts.device, opts.token);
    await waitText(page, "#log", "brak lub zły token");
    return { status: await page.textContent("#status") };
  },

  // Aplikacja do zainstalowania: manifest, service worker i pliki w pamięci podręcznej.
  async pwa(page) {
    await page.goto(`${base}/app/`);
    const manifest = await page.evaluate(async () => (await fetch(document.querySelector("link[rel=manifest]").href)).json());
    await page.evaluate(() => navigator.serviceWorker.ready);
    await page.waitForFunction(async () => (await caches.keys()).length > 0, null, { timeout: 10000 });
    const cached = await page.evaluate(async () => {
      const cache = await caches.open((await caches.keys())[0]);
      return (await cache.keys()).map((r) => new URL(r.url).pathname);
    });
    return { manifest, cached, secure: await page.evaluate(() => window.isSecureContext) };
  },
};

(async () => {
  const browser = await chromium.launch({
    args: ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream", "--autoplay-policy=no-user-gesture-required"],
  });
  const context = await browser.newContext({ ignoreHTTPSErrors: true, permissions: ["microphone"] });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(m.text());
  });
  let result;
  try {
    result = { ok: true, value: await scenarios[scenario](page), errors };
  } catch (e) {
    result = { ok: false, error: String(e), errors };
  }
  await browser.close();
  console.log(JSON.stringify(result));
})();
