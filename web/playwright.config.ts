import net from "node:net";

import { defineConfig } from "@playwright/test";

/** A free port, chosen once: the workers load this file again and must see the same one (they inherit the variable). */
async function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const probe = net.createServer();
    probe.once("error", reject);
    probe.listen(0, "127.0.0.1", () => {
      const address = probe.address();
      probe.close(() => {
        if (address !== null && typeof address === "object") resolve(address.port);
        else reject(new Error("no port"));
      });
    });
  });
}

// The demo server for the ordinary tests, and for each setup project servers with no settings (setup mode), with
// settings but no administrator (admin mode), and three for the tests that talk to a stub controller (#281): one to run
// the whole setup on, one whose environment forces the "nothing was saved" fallback, and a read-only one. The setup
// tests change their servers, so no two projects share one.
const KINDS = ["SETUP", "ADMIN", "STUB", "FALLBACK", "READONLY"] as const;
const SIZES = ["PHONE", "DESKTOP"] as const;
for (const name of ["E2E_PORT", ...SIZES.flatMap((size) => KINDS.map((kind) => `E2E_${kind}_${size}`))]) {
  process.env[name] ??= String(await freePort());
}
const port = process.env["E2E_PORT"] ?? "";
const at = (kind: (typeof KINDS)[number], size: (typeof SIZES)[number]) => `http://127.0.0.1:${process.env[`E2E_${kind}_${size}`] ?? ""}`;
const servers = [
  `demo:${port}`,
  ...SIZES.flatMap((size) => KINDS.map((kind) => `${kind.toLowerCase()}:${process.env[`E2E_${kind}_${size}`] ?? ""}`)),
].join(",");
const metadata = (size: (typeof SIZES)[number]) => ({
  adminURL: at("ADMIN", size),
  stubURL: at("STUB", size),
  fallbackURL: at("FALLBACK", size),
  readonlyURL: at("READONLY", size),
});
const phone = { browserName: "chromium", viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 } as const;
const desktop = { browserName: "chromium", viewport: { width: 1280, height: 800 } } as const;

export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/*.spec.ts",
  fullyParallel: true,
  forbidOnly: Boolean(process.env["CI"]),
  retries: process.env["CI"] ? 1 : 0,
  reporter: process.env["CI"] ? [["github"], ["list"]] : "list",
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    trace: "retain-on-failure",
  },
  projects: [
    // Against `hlp --demo serve`.
    { name: "phone", testIgnore: "**/setup*.spec.ts", use: phone },
    { name: "desktop", testIgnore: "**/setup*.spec.ts", use: desktop },
    // Against servers in their setup, admin and stub modes (the specs read the other servers from `metadata`).
    { name: "setup-phone", testMatch: "**/setup*.spec.ts", metadata: metadata("PHONE"), use: { ...phone, baseURL: at("SETUP", "PHONE") } },
    { name: "setup-desktop", testMatch: "**/setup*.spec.ts", metadata: metadata("DESKTOP"), use: { ...desktop, baseURL: at("SETUP", "DESKTOP") } },
  ],
  webServer: {
    // The production build, served in front of the real servers (e2e/serve.mjs, which starts the demo preview last: its
    // port answering means every server is ready).
    command: "npm run build && node e2e/serve.mjs",
    url: `http://127.0.0.1:${port}/`,
    reuseExistingServer: false,
    timeout: 180_000,
    env: { E2E_PORT: port, E2E_SERVERS: servers },
    stderr: "pipe",
  },
});
