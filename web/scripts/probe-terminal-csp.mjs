/** Isolated #277 feasibility probe. No application/server policy changes and no API/controller requests. */
import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";
import { mkdir, readFile } from "node:fs/promises";
import { createServer } from "node:http";
import { resolve } from "node:path";

import { chromium } from "@playwright/test";

const xtermRoot = new URL("../node_modules/@xterm/xterm/", import.meta.url);
const screenshots = process.argv[2];
if (screenshots) await mkdir(screenshots, { recursive: true });
const policy = "default-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; manifest-src 'self'";

// Served as an external, same-origin module, not an inline script. The wrapper is a prototype, not application code.
const client = `
import { Terminal } from '/xterm.mjs';
const mode = new URL(location.href).searchParams.get('mode');
const nonce = document.querySelector('script[src="/probe.js"]').nonce;
const originalCreate = document.createElement;
window.violations = [];
document.addEventListener('securitypolicyviolation', event => window.violations.push(event.effectiveDirective));
const override = new Proxy(document, {
  get(target, key) {
    if (key === 'createElement') return (...args) => {
      const element = target.createElement(...args);
      const token = mode === 'wrong' ? 'invalid' : nonce;
      if (args[0].toLowerCase() === 'style') element.nonce = token;
      if (args[0].toLowerCase() === 'div') {
        const appendChild = element.appendChild;
        element.appendChild = function(child) {
          // xterm 6.0's scrollbar uses the global document, bypassing documentOverride. Authorize before insertion.
          if (this.classList.contains('xterm-screen') && child instanceof HTMLStyleElement) child.nonce = token;
          return appendChild.call(this, child);
        };
      }
      return element;
    };
    const value = Reflect.get(target, key, target);
    return typeof value === 'function' ? value.bind(target) : value;
  }
});
const term = new Terminal({ cols: 32, rows: 10, fontFamily: 'monospace', fontSize: 14, cursorBlink: false,
  theme: { background: '#111111', foreground: '#eeeeee' },
  ...(mode === 'strict' ? {} : { documentOverride: override }) });
window.term = term;
window.globalUnchanged = document.createElement === originalCreate;
term.open(document.getElementById('terminal'));
term.onData(data => { window.lastInput = data; });
await new Promise(resolve => term.write('Terminal CSP probe\\r\\nASCII  wide \\u754c  emoji \\ud83d\\ude00\\r\\n> ', resolve));
window.ready = true;
window.attack = () => {
  const style = document.createElement('style'); style.textContent = '#sentinel { color: rgb(255, 0, 0) !important; }'; document.head.append(style);
  const wrong = document.createElement('style'); wrong.nonce = 'wrong'; wrong.textContent = '#sentinel { color: rgb(0, 255, 0) !important; }'; document.head.append(wrong);
  document.getElementById('sentinel').setAttribute('style', 'color: rgb(0, 0, 255) !important');
  // Even the valid STYLE nonce must not authorize an inline SCRIPT.
  const script = document.createElement('script'); script.nonce = nonce; script.textContent = 'window.badScript = true'; document.head.append(script);
};
`;

const server = createServer((request, response) => {
  void serve(request, response).catch(() => { response.writeHead(500); response.end(); });
});
async function serve(request, response) {
  if (request.url === "/probe.js") {
    response.setHeader("Content-Type", "text/javascript"); response.end(client); return;
  }
  if (request.url === "/xterm.mjs" || request.url === "/xterm.css") {
    const css = request.url.endsWith(".css");
    response.setHeader("Content-Type", css ? "text/css" : "text/javascript");
    response.end(await readFile(new URL(css ? "css/xterm.css" : "lib/xterm.mjs", xtermRoot))); return;
  }
  if (request.url === "/probe.css") {
    response.setHeader("Content-Type", "text/css");
    response.end("body { margin: 16px; font-family: sans-serif; background: #fff; color: #111; } #terminal { width: 100%; height: 240px; } #sentinel { color: rgb(17, 17, 17); }"); return;
  }
  const nonce = randomBytes(24).toString("base64");
  const mode = new URL(request.url ?? "/", "http://localhost").searchParams.get("mode");
  response.setHeader("Content-Type", "text/html");
  response.setHeader("Cache-Control", "no-store");
  response.setHeader("Content-Security-Policy", policy + (mode === "strict" ? "" : `; style-src-elem 'self' 'nonce-${nonce}'; style-src-attr 'none'`));
  response.end(`<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"><link rel="stylesheet" href="/xterm.css"><link rel="stylesheet" href="/probe.css"><script type="module" src="/probe.js" nonce="${nonce}"></script></head><body><h1>Terminal CSP Probe</h1><div id="terminal"></div><p id="sentinel">Unrelated styles remain blocked</p></body></html>`);
}

await new Promise((done, reject) => {
  server.once("error", reject);
  server.listen(0, "127.0.0.1", done);
});
let browser;
try {
  browser = await chromium.launch();
  const port = server.address().port;
  for (const viewport of [{ width: 390, height: 844 }, { width: 1280, height: 800 }]) {
    for (const mode of ["strict", "nonce", "wrong"]) {
      const page = await browser.newPage({ viewport });
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.goto(`http://127.0.0.1:${port}/?mode=${mode}`);
      await page.waitForFunction(() => window.ready);
      await page.waitForTimeout(100);
      const initial = await page.evaluate(() => ({
        violations: window.violations.slice(), globalUnchanged: window.globalUnchanged,
        styles: [...document.querySelectorAll(".xterm style")].map((s) => ({ authorized: Boolean(s.nonce), applied: Boolean(s.sheet) })),
        spanDisplay: getComputedStyle(document.querySelector(".xterm-rows span")).display,
        font: getComputedStyle(document.querySelector(".xterm-rows")).fontFamily,
        color: getComputedStyle(document.querySelector(".xterm-rows")).color,
      }));
      assert.deepEqual(errors, []);
      assert.equal(initial.globalUnchanged, true);
      if (mode === "nonce") {
        assert.equal(initial.violations.length, 0);
        assert.equal(initial.spanDisplay, "inline-block");
        assert.match(initial.font, /monospace/);
        assert.equal(initial.color, "rgb(238, 238, 238)");
        assert(initial.styles.length >= 3 && initial.styles.every((s) => s.authorized && s.applied));
        if (screenshots) await page.screenshot({ path: resolve(screenshots, `terminal-csp-${viewport.width}-dark.png`) });
        await page.evaluate(() => { window.term.options.theme = { background: "#ffffff", foreground: "#111111" }; window.term.resize(28, 8); });
        await page.locator(".xterm-helper-textarea").focus();
        await page.keyboard.type("hello");
        await page.waitForFunction(() => window.lastInput === "o");
        await page.waitForFunction(() => getComputedStyle(document.querySelector(".xterm-rows")).color === "rgb(17, 17, 17)");
        assert.equal(await page.evaluate(() => window.term.cols), 28);
        assert.equal(await page.evaluate(() => window.term.rows), 8);
        assert.equal(await page.evaluate(() => window.violations.length), 0);
        if (screenshots) await page.screenshot({ path: resolve(screenshots, `terminal-csp-${viewport.width}-light.png`) });
        await page.evaluate(() => window.attack());
        await page.waitForTimeout(100);
        const denied = await page.evaluate(() => ({ color: getComputedStyle(document.getElementById("sentinel")).color,
          script: Boolean(window.badScript), violations: window.violations.slice() }));
        assert.equal(denied.color, "rgb(17, 17, 17)");
        assert.equal(denied.script, false);
        assert(denied.violations.includes("style-src-elem") && denied.violations.includes("style-src-attr") && denied.violations.includes("script-src-elem"));
      } else {
        assert(initial.violations.length > 0);
        assert.notEqual(initial.spanDisplay, "inline-block");
      }
      console.log(`${viewport.width}px ${mode}: passed`);
      await page.close();
    }
  }
} finally {
  await browser?.close();
  await new Promise((done) => server.close(done));
}
