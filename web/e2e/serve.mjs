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
//      `setup` and `admin` get a setup token chosen here (HLP_SETUP_TOKEN). The tests never let them contact a
//      controller, and nothing they do sends a notification;
//   2. serves web/dist on the port given with `vite preview`, forwarding /api, /healthz and /readyz to that server
//      (vite.config.ts), with the content-security policy the real server sends on every response, so a script or
//      style it forbids fails the tests instead of failing for the first user;
//   3. answers GET /__e2e/credentials with the demo login (the password is random per run) or the setup token.
//
// The previews start in the order given and the demo one is last, so the port Playwright waits for (E2E_PORT, the
// demo) answering means every server is ready. Usage: node e2e/serve.mjs   (`npm run build` must have run.)
import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { chmodSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
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

/** Starts the real server (demo or setup) and resolves with its address and what the tests need to log in. */
async function startServer(mode) {
  const apiPort = await freePort();
  const env = { ...process.env };
  delete env.VIRTUAL_ENV; // a developer's own environment must not leak into uv's
  const hlp = ["run", "--project", repo, "--extra", "web", path.join(repo, "hlp.py")];
  let args = [...hlp, "--demo", "serve", "--port", String(apiPort)];
  let cwd = repo;
  const setupToken = randomBytes(18).toString("base64url");
  if (mode === "setup" || mode === "admin") {
    for (const name of Object.keys(env)) if (name.startsWith("UNIFI_") || name.startsWith("NOTIFY_") || name === "HLP_ENV") delete env[name];
    const dataDir = mkdtempSync(path.join(os.tmpdir(), `hlp-e2e-${mode}-`));
    directories.push(dataDir);
    env.HLP_SETUP_TOKEN = setupToken;
    if (mode === "admin") {
      // An address that cannot exist (.invalid) and a key that is not one: the server never needs either here.
      const settings = { UNIFI_URL: "https://controller.invalid", UNIFI_API_KEY: "e2e-placeholder-key-not-a-real-one" };
      Object.assign(env, settings);
      const file = path.join(dataDir, ".env");
      writeFileSync(file, Object.entries(settings).map(([name, value]) => `${name}=${value}\n`).join(""));
      chmodSync(file, 0o600);
    }
    args = [...hlp, "serve", "--port", String(apiPort), "--data-dir", dataDir];
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
      ? { setupToken }
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
async function servePreview(port, server) {
  process.env.HLP_API_TARGET = server.target;
  const app = await preview({
    root: web,
    configFile: path.join(web, "vite.config.ts"),
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
  if (!["demo", "setup", "admin"].includes(mode)) throw new Error(`E2E_SERVERS: unknown mode ${mode}`);
  if (!Number.isInteger(Number(port)) || Number(port) <= 0) throw new Error(`E2E_SERVERS: bad port in ${entry}`);
  return { mode, port: Number(port) };
});
wanted.sort((a, b) => Number(a.mode === "demo") - Number(b.mode === "demo"));
const servers = await Promise.all(wanted.map(({ mode }) => startServer(mode)));
for (const [index, { port }] of wanted.entries()) await servePreview(port, servers[index]);
