// The web server of the Playwright smoke: a synthetic network behind the production build, on free ports.
//
//   1. starts `uv run --extra web hlp.py --demo serve` (the real server, with synthetic data: no controller, no
//      `.env`) on a free port and reads the demo login it prints on stderr;
//   2. serves web/dist with `vite preview`, forwarding /api, /healthz and /readyz to it (vite.config.ts), with the
//      content-security policy the real server sends on every response, so a script or style it forbids fails the
//      smoke instead of failing for the first user;
//   3. answers GET /__e2e/credentials with the demo login, for the tests (the password is random per run).
//
// Usage: node e2e/serve.mjs   (E2E_PORT is the port to serve on; `npm run build` must have run.)
import { spawn } from "node:child_process";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { preview } from "vite";

const web = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const repo = path.resolve(web, "..");
const port = Number(process.env.E2E_PORT);
if (!Number.isInteger(port) || port <= 0) throw new Error("E2E_PORT is not set");

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

const apiPort = await freePort();
const env = { ...process.env };
delete env.VIRTUAL_ENV; // a developer's own environment must not leak into uv's
const server = spawn("uv", ["run", "--extra", "web", "hlp.py", "--demo", "serve", "--port", String(apiPort)], {
  cwd: repo,
  env,
  stdio: ["ignore", "inherit", "pipe"],
});
server.on("exit", (code) => {
  if (code !== 0 && code !== null) {
    console.error(`the demo server exited with code ${code}`);
    process.exit(1);
  }
});
for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => {
    server.kill("SIGTERM");
    process.exit(0);
  });
}
process.on("exit", () => {
  server.kill("SIGTERM");
});

const credentials = await new Promise((resolve, reject) => {
  let text = "";
  const timer = setTimeout(() => {
    reject(new Error(`the demo server printed no login within 120 s:\n${text}`));
  }, 120_000);
  server.stderr.on("data", (chunk) => {
    process.stderr.write(chunk);
    text += chunk.toString();
    const found = /Demo login: user (\S+), password (\S+) \(/.exec(text);
    if (found) {
      clearTimeout(timer);
      resolve({ username: found[1], password: found[2] });
    }
  });
});

const target = `http://127.0.0.1:${apiPort}`;
let policy = "";
for (let attempt = 0; attempt < 100 && policy === ""; attempt += 1) {
  try {
    const response = await fetch(`${target}/healthz`);
    policy = response.headers.get("content-security-policy") ?? "";
  } catch {
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
}
if (policy === "") throw new Error("the demo server did not answer /healthz with a content-security policy");

process.env.HLP_API_TARGET = target;
const app = await preview({
  root: web,
  configFile: path.join(web, "vite.config.ts"),
  preview: {
    host: "127.0.0.1",
    port,
    strictPort: true,
    headers: { "Content-Security-Policy": policy, "X-Content-Type-Options": "nosniff" },
  },
  plugins: [
    {
      name: "e2e-credentials",
      configurePreviewServer(previewServer) {
        previewServer.middlewares.use("/__e2e/credentials", (_request, response) => {
          response.setHeader("Content-Type", "application/json");
          response.end(JSON.stringify({ ...credentials, policy }));
        });
      },
    },
  ],
});
app.printUrls();
