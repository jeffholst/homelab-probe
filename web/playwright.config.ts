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

// The demo server for the ordinary tests, and for each setup project a server with no settings (setup mode) and one
// with settings but no administrator (admin mode): the setup tests change their server, so no two projects share one.
for (const name of ["E2E_PORT", "E2E_SETUP_PHONE", "E2E_ADMIN_PHONE", "E2E_SETUP_DESKTOP", "E2E_ADMIN_DESKTOP"]) {
  process.env[name] ??= String(await freePort());
}
const port = process.env["E2E_PORT"] ?? "";
const at = (name: string) => `http://127.0.0.1:${process.env[name] ?? ""}`;
const servers = [
  `demo:${port}`,
  `setup:${process.env["E2E_SETUP_PHONE"] ?? ""}`,
  `admin:${process.env["E2E_ADMIN_PHONE"] ?? ""}`,
  `setup:${process.env["E2E_SETUP_DESKTOP"] ?? ""}`,
  `admin:${process.env["E2E_ADMIN_DESKTOP"] ?? ""}`,
].join(",");
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
    { name: "phone", testIgnore: "**/setup.spec.ts", use: phone },
    { name: "desktop", testIgnore: "**/setup.spec.ts", use: desktop },
    // Against servers in their setup and admin modes (e2e/setup.spec.ts reads the admin one from `metadata`).
    { name: "setup-phone", testMatch: "**/setup.spec.ts", metadata: { adminURL: at("E2E_ADMIN_PHONE") }, use: { ...phone, baseURL: at("E2E_SETUP_PHONE") } },
    {
      name: "setup-desktop",
      testMatch: "**/setup.spec.ts",
      metadata: { adminURL: at("E2E_ADMIN_DESKTOP") },
      use: { ...desktop, baseURL: at("E2E_SETUP_DESKTOP") },
    },
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
