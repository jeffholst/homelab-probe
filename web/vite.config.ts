import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// `npm run dev` and `vite preview` (the Playwright smoke) forward the server's own paths to a running
// `hlp serve`, usually `uv run --extra web hlp.py --demo serve`, so the app talks to it from one origin, like the
// production bundle will. The server refuses an unsafe request whose Origin is not its own Host (that is its CSRF
// protection), so the proxy gives it the target's origin; nothing else about the request is changed.
const target = process.env["HLP_API_TARGET"] ?? "http://127.0.0.1:8787";

const forward = {
  target,
  changeOrigin: true,
  configure(proxy: { on(event: "proxyReq", listener: (request: { getHeader(name: string): unknown; setHeader(name: string, value: string): void }) => void): void }) {
    proxy.on("proxyReq", (request) => {
      if (request.getHeader("origin") !== undefined) request.setHeader("origin", target);
    });
  },
};

const proxy = { "/api": forward, "/healthz": forward, "/readyz": forward };

export default defineConfig({
  base: "/",
  plugins: [react()],
  // The bundle is written to web/dist. Copying it into the Python package is the release step, not this one.
  build: { outDir: "dist", sourcemap: false, target: "es2022" },
  server: { proxy },
  preview: { proxy },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    restoreMocks: true,
  },
});
