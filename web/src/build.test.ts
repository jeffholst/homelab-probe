import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

import { expect, it } from "vitest";

it.each([undefined, "0", "1"])("ships the nonce marker only with the preview build gate set to 1 (flag: %s)", (flag) => {
  const output = mkdtempSync(join(tmpdir(), "hlp-build-gate-"));
  const env = { ...process.env };
  delete env["VITE_TERMINAL_MOCK"];
  delete env["VITE_TERMINAL_API_PREVIEW"];
  if (flag !== undefined) env["VITE_TERMINAL_API_PREVIEW"] = flag;
  try {
    execFileSync(process.execPath, [resolve("node_modules/vite/bin/vite.js"), "build", "--outDir", output], {
      cwd: process.cwd(), env, timeout: 30_000, stdio: "pipe",
    });
    const html = readFileSync(join(output, "index.html"), "utf8");
    const document = new DOMParser().parseFromString(html, "text/html");
    const bootstrap = document.querySelector('script[src="/theme-init.js"]');
    expect(bootstrap).not.toBeNull();
    if (flag === "1") {
      expect(bootstrap?.hasAttribute("data-terminal-style-nonce")).toBe(true);
      expect(bootstrap?.getAttribute("nonce")).toBe("__HLP_TERMINAL_STYLE_NONCE__");
      expect(document.querySelectorAll("[nonce]")).toHaveLength(1);
    } else {
      expect(document.querySelectorAll("[nonce], [data-terminal-style-nonce]")).toHaveLength(0);
      expect(html).not.toContain("__HLP_TERMINAL_STYLE_NONCE__");
    }
  } finally {
    rmSync(output, { recursive: true, force: true });
  }
}, 35_000);
