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

process.env["E2E_PORT"] ??= String(await freePort());
const port = process.env["E2E_PORT"];

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
    {
      name: "phone",
      use: { browserName: "chromium", viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 },
    },
    {
      name: "desktop",
      use: { browserName: "chromium", viewport: { width: 1280, height: 800 } },
    },
  ],
  webServer: {
    // The production build, served in front of the real server's demo mode (e2e/serve.mjs).
    command: "npm run build && node e2e/serve.mjs",
    url: `http://127.0.0.1:${port}/`,
    reuseExistingServer: false,
    timeout: 180_000,
    env: { E2E_PORT: port },
    stderr: "pipe",
  },
});
