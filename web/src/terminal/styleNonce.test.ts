import { afterEach, describe, expect, it, vi } from "vitest";

import { terminalDocument } from "./styleNonce";

afterEach(() => { document.head.querySelectorAll("script[data-terminal-style-nonce]").forEach((script) => script.remove()); vi.unstubAllEnvs(); });

function carrier(nonce: string) {
  const script = document.createElement("script");
  script.dataset["terminalStyleNonce"] = "";
  script.nonce = nonce;
  document.head.append(script);
}

describe("terminal-local style nonces", () => {
  it("is disabled without the preview gate and fails closed for missing or malformed tokens", () => {
    carrier("a".repeat(32));
    vi.stubEnv("VITE_TERMINAL_API_PREVIEW", "0");
    expect(terminalDocument(document)).toBeUndefined();
    vi.stubEnv("VITE_TERMINAL_API_PREVIEW", "1");
    document.head.querySelector("script[data-terminal-style-nonce]")?.remove();
    expect(terminalDocument(document)).toBeUndefined();
    carrier("__HLP_TERMINAL_STYLE_NONCE__");
    expect(terminalDocument(document)).toBeUndefined();
  });

  it("authorizes only adapter styles and the terminal-local 6.0 scrollbar path", () => {
    vi.stubEnv("VITE_TERMINAL_API_PREVIEW", "1");
    const nonce = "a".repeat(32);
    carrier(nonce);
    const create = Object.getOwnPropertyDescriptor(Document.prototype, "createElement");
    const ownCreate = Object.getOwnPropertyDescriptor(document, "createElement");
    const append = Object.getOwnPropertyDescriptor(Node.prototype, "appendChild");
    const local = terminalDocument(document)!;
    expect(local instanceof Document).toBe(true);
    expect(local.createElement("STYLE").nonce).toBe(nonce);
    const screen = local.createElement("div");
    screen.className = "xterm-screen";
    const style = document.createElement("style");
    expect(screen.appendChild(style)).toBe(style);
    expect(style.nonce).toBe(nonce);
    const other = local.createElement("div");
    const unrelated = document.createElement("style");
    other.appendChild(unrelated);
    expect(unrelated.nonce).toBe("");
    const script = document.createElement("script");
    screen.appendChild(script);
    expect(script.nonce).toBe("");
    expect(local.createTextNode("plain").textContent).toBe("plain");
    expect(local.body).toBe(document.body);
    expect(Object.getOwnPropertyDescriptor(Document.prototype, "createElement")).toEqual(create);
    expect(Object.getOwnPropertyDescriptor(document, "createElement")).toEqual(ownCreate);
    expect(Object.getOwnPropertyDescriptor(Node.prototype, "appendChild")).toEqual(append);
    expect(document.createElement("style").nonce).toBe("");
  });
});
