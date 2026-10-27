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

// Stan widoku z <body data-state> (offline, connecting, inactive, idle, listening, thinking, speaking).
async function waitState(page, name, timeout = 10000) {
  await page.waitForSelector(`body[data-state="${name}"]`, { timeout });
}

// Zapis kolejnych stanów widoku i napisy rozmowy, do sprawdzenia po turze.
async function recordStates(page) {
  await page.evaluate(() => {
    window.__states = [document.body.dataset.state];
    new MutationObserver(() => {
      const s = document.body.dataset.state;
      if (window.__states.at(-1) !== s) window.__states.push(s);
    }).observe(document.body, { attributes: true, attributeFilter: ["data-state"] });
  });
}

function captions(page) {
  return page.evaluate(() =>
    Array.from(document.querySelectorAll("#turns li"), (li) => ({
      heard: li.querySelector(".heard").hidden ? null : li.querySelector(".heard").textContent,
      reply: li.querySelector(".reply").hidden ? null : li.querySelector(".reply").textContent,
      pending: li.querySelectorAll(".word.pending").length,
      note: li.querySelector(".note").hidden ? null : li.querySelector(".note").textContent,
    })),
  );
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
    await waitText(page, "#status", "połączono");
    await waitState(page, "idle");
    await recordStates(page);
    const t = Array.from({ length: 16000 * opts.seconds }, (_, i) => 0.4 * Math.sin((2 * Math.PI * opts.hz * i) / 16000));
    const uttId = await page.evaluate((samples) => window.iris.sayPcm(samples), t);
    await page.waitForFunction(() => window.iris.state.played.length >= 1, null, { timeout: 20000 });
    await waitState(page, "idle");
    const turns = await captions(page);
    const states = await page.evaluate(() => window.__states);
    return page.evaluate(([uttId, turns, states]) => {
      const { samples, rate } = window.iris.state.lastSamples;
      const step = Math.max(1, Math.round(rate / 16000));
      return {
        uttId,
        played: window.iris.state.played,
        rate: rate / step,
        samples: Array.from(samples.filter((_, i) => i % step === 0), (x) => Math.round(x * 32767)),
        caps: window.iris.body.caps(),
        turns,
        states,
      };
    }, [uttId, turns, states]);
  },

  // Wypowiedź bez rozpoznanego tekstu albo z błędem hosta: napis z uwagą, powrót do spoczynku.
  async noReply(page) {
    await connect(page, opts.device, opts.token);
    await waitState(page, "idle");
    const t = new Array(16000).fill(0);
    await page.evaluate((samples) => window.iris.sayPcm(samples), t);
    await page.waitForFunction(() => document.querySelector("#turns .note:not([hidden])"), null, { timeout: 20000 });
    await waitState(page, "idle");
    return { turns: await captions(page), state: await page.textContent("#state") };
  },

  // Prawdziwy mikrofon (sztuczne urządzenie Chromium): przycisk wciśnięty przez opts.ms.
  async mic(page) {
    await connect(page, opts.device, opts.token);
    await waitState(page, "idle");
    await page.waitForTimeout(1300); // sprawdzenie głosu na hoście po pierwszej sekundzie
    await page.waitForSelector("#talk:not([disabled])");
    const box = await page.locator("#talk").boundingBox();
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await waitState(page, "listening");
    const pressed = await page.getAttribute("#talk", "aria-pressed");
    await page.waitForTimeout(opts.ms);
    await page.mouse.up();
    await page.waitForTimeout(1500); // koniec kodowania i wysyłki
    return { pressed, released: await page.getAttribute("#talk", "aria-pressed") };
  },

  // Urządzenie w spoczynku i przycisk "Używaj tego urządzenia".
  async switching(page) {
    await connect(page, opts.device, opts.token);
    await waitState(page, "inactive");
    const talkHidden = await page.locator("#talk").isHidden();
    const before = await page.evaluate(() => window.iris.body.active);
    await page.click("#activate");
    await waitState(page, "idle");
    return {
      talkHidden,
      before,
      after: await page.evaluate(() => window.iris.body.active),
      activateHidden: await page.locator("#activate").isHidden(),
      talkVisible: await page.locator("#talk").isVisible(),
    };
  },

  // Samo połączenie: stan widoczny w interfejsie i możliwości wysłane do hosta.
  async connectOnly(page) {
    await connect(page, opts.device, opts.token);
    await waitText(page, "#status", "połączono");
    await page.waitForTimeout(opts.stayMs || 0);
    return {
      status: await page.textContent("#status"),
      state: await page.textContent("#state"),
      notice: await page.textContent("#notice"),
      talkDisabled: await page.locator("#talk").isDisabled(),
      caps: await page.evaluate(() => window.iris.body.caps()),
    };
  },

  // Zły token: kod 4003 i komunikat, bez połączenia.
  async badToken(page) {
    await connect(page, opts.device, opts.token);
    await waitText(page, "#notice", "Host odrzucił token");
    return { notice: await page.textContent("#notice"), state: await page.textContent("#state") };
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
