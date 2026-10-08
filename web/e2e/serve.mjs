// The web servers of the Playwright tests: the production build in front of real servers, on free ports.
//
// E2E_SERVERS lists them as `mode:port,...` (the default is `demo:$E2E_PORT`). For each one this script
//   1. starts the real server on a free port of its own, in a temporary data directory where it needs one:
//      - `demo`: `uv run --extra web hlp.py --demo serve` (synthetic data: no controller, no `.env`), reading the demo
//        login it prints on stderr;
//      - `setup`: `hlp serve` with no settings at all (no UNIFI_*, NOTIFY_* or HLP_ENV variable, an empty data
//        directory), so it starts in the setup mode;
//      - `admin`: `hlp serve` with settings (UNIFI_URL and UNIFI_API_KEY in the environment and in the data directory's
//        `.env`, for an address that does not exist) and no account, so it starts in the admin mode;
//      - `stub`, `fallback`, `readonly`: like `setup`, for the tests that need a controller to talk to (#281): a
//        server to run the whole setup on, one whose environment sets UNIFI_VERIFY_SSL so that finishing returns the
//        "nothing was saved" files, and one started with --read-only. Their credentials also name the stub
//        controller (see below);
//      all but `demo` get a setup token chosen here (HLP_SETUP_TOKEN). Nothing they do reaches a real controller or
//      sends a notification;
//      The stub controllers (`e2e/stub_controller.py`) are two HTTPS servers on 127.0.0.1 with the synthetic demo
//      network: one shows a certificate that can be pinned, the other one that cannot; both accept one random key;
//   2. serves web/dist on the port given with `vite preview`, forwarding /api, /healthz and /readyz to that server
//      (vite.config.ts), with the content-security policy the real server sends on every response, so a script or
//      style it forbids fails the tests instead of failing for the first user;
//   3. answers GET /__e2e/credentials with the demo login (the password is random per run) or the setup token.
//
// The previews start in the order given and the demo one is last, so the port Playwright waits for (E2E_PORT, the
// demo) answering means every server is ready. Usage: node e2e/serve.mjs   (`npm run build` must have run.)
import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { chmodSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { preview } from "vite";

const web = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const repo = path.resolve(web, "..");

function portOf(name) {
  const value = Number(process.env[name]);
  if (!Number.isInteger(value) || value <= 0) throw new Error(`${name} is not set`);
  return value;
}

function freePort() {
  return new Promise((resolve, reject) => {
    const probe = net.createServer();
    probe.once("error", reject);
    probe.listen(0, "127.0.0.1", () => {
      const { port: found } = probe.address();
      probe.close(() => {
        resolve(found);
      });
    });
  });
}

const children = [];
const directories = [];
function stopAll() {
  for (const child of children) child.kill("SIGTERM");
  for (const directory of directories) rmSync(directory, { recursive: true, force: true });
}
for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => {
    stopAll();
    process.exit(0);
  });
}
process.on("exit", stopAll);

/** Starts a stub controller (see stub_controller.py) and resolves with its address, fingerprint and key. */
function startStub(cert, key) {
  const child = spawn("uv", ["run", "--project", repo, "python", path.join(web, "e2e", "stub_controller.py"), `--key=${key}`, "--cert", cert], {
    cwd: repo,
    env: { ...process.env, VIRTUAL_ENV: undefined },
    stdio: ["ignore", "pipe", "inherit"],
  });
  children.push(child);
  return new Promise((resolve, reject) => {
    let text = "";
    const timer = setTimeout(() => {
      reject(new Error(`the ${cert} stub controller printed nothing within 120 s:\n${text}`));
    }, 120_000);
    child.on("exit", (code) => {
      if (code !== 0 && code !== null) {
        console.error(`the ${cert} stub controller exited with code ${code}`);
        process.exit(1);
      }
    });
    child.stdout.on("data", (chunk) => {
      text += chunk.toString();
      const line = text.split("\n")[0];
      if (!text.includes("\n")) return;
      clearTimeout(timer);
      const { port, fingerprint } = JSON.parse(line);
      resolve({ url: `https://127.0.0.1:${port}`, fingerprint, key });
    });
  });
}

let stubs = null;
/** The two stub controllers, started on first use: what a test types into the setup to reach them. */
function controllers() {
  stubs ??= (async () => {
    // Always exercise option-looking keys so argument parsing cannot regress intermittently.
    const key = `-${randomBytes(18).toString("base64url")}`;
    const [good, other] = await Promise.all([startStub("good", key), startStub("other", key)]);
    return { url: good.url, key, fingerprint: good.fingerprint, otherUrl: other.url, otherFingerprint: other.fingerprint };
  })();
  return stubs;
}

const SETUP_LIKE = new Set(["setup", "admin", "stub", "fallback", "readonly"]);

/** Starts the real server (demo or setup) and resolves with its address and what the tests need to log in. */
async function startServer(mode) {
  const apiPort = await freePort();
  const env = { ...process.env };
  delete env.VIRTUAL_ENV; // a developer's own environment must not leak into uv's
  const hlp = ["run", "--project", repo, "--extra", "web", path.join(repo, "hlp.py")];
  let args = [...hlp, "--demo", "serve", "--port", String(apiPort)];
  let cwd = repo;
  let dataDir = null;
  const setupToken = randomBytes(18).toString("base64url");
  if (SETUP_LIKE.has(mode)) {
    for (const name of Object.keys(env)) if (name.startsWith("UNIFI_") || name.startsWith("NOTIFY_") || name === "HLP_ENV") delete env[name];
    dataDir = mkdtempSync(path.join(os.tmpdir(), `hlp-e2e-${mode}-`));
    directories.push(dataDir);
    env.HLP_SETUP_TOKEN = setupToken;
    if (mode === "admin") {
      // An address that cannot exist (.invalid) and a key that is not one: the server never needs either here.
      const settings = { UNIFI_URL: "https://controller.invalid", UNIFI_API_KEY: "e2e-placeholder-key-not-a-real-one" };
      Object.assign(env, settings);
      const file = path.join(dataDir, ".env");
      writeFileSync(file, Object.entries(settings).map(([name, value]) => `${name}=${value}\n`).join(""));
      chmodSync(file, 0o600);
      // A note about a device that is not in any inventory (the controller here does not exist), as the notes store
      // writes it: a backup of this server must carry it, and a restore must bring it back.
      const notes = path.join(dataDir, "snapshots", "e2e-site");
      mkdirSync(notes, { recursive: true, mode: 0o700 });
      writeFileSync(
        path.join(notes, "notes.json"),
        JSON.stringify({
          version: 1,
          site: "e2e-site",
          entries: {
            "95b16d11cf12c305": {
              author: "e2e",
              context: { at: 1790000000, name: "Old switch" },
              created_at: 1790000000,
              modified_at: 1790000000,
              modified_by: "e2e",
              subject: "device:AA:BB:CC:DD:EE:01",
              text: "A note kept for a device that is gone.",
            },
          },
        }),
        { mode: 0o600 },
      );
    }
    // The setting that makes `finish` return the files instead of saving: the environment would win over the saved file
    // (it only matters when the controller's certificate is pinned, which changes UNIFI_VERIFY_SSL).
    if (mode === "fallback") env.UNIFI_VERIFY_SSL = "true";
    args = [...hlp, "serve", "--port", String(apiPort), "--data-dir", dataDir, ...(mode === "readonly" ? ["--read-only"] : [])];
    cwd = dataDir; // ./hlp.toml is looked for here: there is none
  }
  const server = spawn("uv", args, { cwd, env, stdio: ["ignore", "inherit", "pipe"] });
  children.push(server);
  server.on("exit", (code) => {
    if (code !== 0 && code !== null) {
      console.error(`the ${mode} server exited with code ${code}`);
      process.exit(1);
    }
  });

  let text = "";
  server.stderr.on("data", (chunk) => {
    process.stderr.write(chunk);
    text += chunk.toString();
  });
  const credentials =
    mode !== "demo"
      ? // `dataDir` lets a test look at what the server did and did not write (the harness and the tests share a machine).
        { setupToken, dataDir, ...(["stub", "fallback", "readonly"].includes(mode) ? { controller: await controllers() } : {}) }
      : await new Promise((resolve, reject) => {
          const timer = setTimeout(() => {
            reject(new Error(`the demo server printed no login within 120 s:\n${text}`));
          }, 120_000);
          const look = () => {
            const found = /Demo login: user (\S+), password (\S+) \(/.exec(text);
            if (found) {
              clearTimeout(timer);
              resolve({ username: found[1], password: found[2] });
            } else {
              setTimeout(look, 50);
            }
          };
          look();
        });

  const target = `http://127.0.0.1:${apiPort}`;
  let policy = "";
  for (let attempt = 0; attempt < 1200 && policy === ""; attempt += 1) {
    try {
      const response = await fetch(`${target}/healthz`);
      policy = response.headers.get("content-security-policy") ?? "";
    } catch {
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
  }
  if (policy === "") throw new Error(`the ${mode} server did not answer /healthz with a content-security policy`);
  return { target, credentials, policy };
}

/** Serves the build on `port` in front of the server at `target` (vite.config.ts reads HLP_API_TARGET when loaded). */
async function servePreview(port, server, outDir = "dist") {
  process.env.HLP_API_TARGET = server.target;
  const app = await preview({
    root: web,
    configFile: path.join(web, "vite.config.ts"),
    build: { outDir: path.join(web, outDir) },
    preview: {
      host: "127.0.0.1",
      port,
      strictPort: true,
      headers: { "Content-Security-Policy": server.policy, "X-Content-Type-Options": "nosniff" },
    },
    plugins: [
      {
        name: "e2e-credentials",
        configurePreviewServer(previewServer) {
          previewServer.middlewares.use("/__e2e/credentials", (_request, response) => {
            response.setHeader("Content-Type", "application/json");
            response.end(JSON.stringify({ ...server.credentials, policy: server.policy }));
          });
        },
      },
    ],
  });
  app.printUrls();
}

const wanted = (process.env.E2E_SERVERS ?? `demo:${portOf("E2E_PORT")}`).split(",").map((entry) => {
  const [mode, port] = entry.split(":");
  if (!["demo", "setup", "admin", "stub", "fallback", "readonly"].includes(mode)) throw new Error(`E2E_SERVERS: unknown mode ${mode}`);
  if (!Number.isInteger(Number(port)) || Number(port) <= 0) throw new Error(`E2E_SERVERS: bad port in ${entry}`);
  return { mode, port: Number(port) };
});
wanted.sort((a, b) => Number(a.mode === "demo") - Number(b.mode === "demo"));
const servers = await Promise.all(wanted.map(({ mode }) => startServer(mode)));
for (const [index, { port }] of wanted.entries()) await servePreview(port, servers[index]);
const terminalApiPort = process.env.E2E_TERMINAL_API_PORT;
if (terminalApiPort) {
  const demoIndex = wanted.findIndex(({ mode }) => mode === "demo");
  if (demoIndex < 0) throw new Error("the terminal API preview needs a demo server");
  await servePreview(portOf("E2E_TERMINAL_API_PORT"), servers[demoIndex], "dist-terminal-api");
}
