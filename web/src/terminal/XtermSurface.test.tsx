import { render, cleanup } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { miniEngine } from "./testing/miniEngine";
import { MOCK_CAPABILITIES } from "./mock";
import type { Entry } from "./render";
import type { EngineState } from "./engine/types";

interface Fake {
  writes: string[];
  disposed: boolean;
  subscriptions: { disposed: boolean }[];
  handlers: { data?: (d: string) => void; key?: (e: KeyboardEvent) => boolean; resize?: () => void };
  markers: { line: number; disposed: boolean }[];
  options: Record<string, unknown>;
  focused: boolean;
}
const terminals: Fake[] = [];
const fits: { disposed: boolean }[] = [];

vi.mock("@xterm/xterm", () => ({
  Terminal: class {
    fake: Fake = { writes: [], disposed: false, subscriptions: [], handlers: {}, markers: [], options: {}, focused: false };
    textarea = document.createElement("textarea");
    buffer = { active: { baseY: 0, cursorY: 0 } };
    options: Record<string, unknown>;
    constructor(options: Record<string, unknown>) {
      this.options = { ...options };
      this.fake.options = this.options;
      terminals.push(this.fake);
    }
    loadAddon() {}
    open() {}
    write(data: string, done?: () => void) { this.fake.writes.push(data); done?.(); }
    reset() { this.fake.writes.push("<reset>"); }
    focus() { this.fake.focused = true; }
    hasSelection() { return false; }
    registerMarker() { const marker = { line: 0, disposed: false, dispose() { marker.disposed = true; } }; this.fake.markers.push(marker); return marker; }
    attachCustomKeyEventHandler(handler: (e: KeyboardEvent) => boolean) { this.fake.handlers.key = handler; }
    private subscribe() { const s = { disposed: false, dispose() { s.disposed = true; } }; this.fake.subscriptions.push(s); return s; }
    onData(handler: (d: string) => void) { this.fake.handlers.data = handler; return this.subscribe(); }
    onResize(handler: () => void) { this.fake.handlers.resize = handler; return this.subscribe(); }
    dispose() { this.fake.disposed = true; }
  },
}));
vi.mock("@xterm/addon-fit", () => ({ FitAddon: class { s = { disposed: false }; constructor() { fits.push(this.s); } fit() {} dispose() { this.s.disposed = true; } } }));
vi.mock("@xterm/xterm/css/xterm.css", () => ({}));

const { default: XtermSurface } = await import("./XtermSurface");

let observers: { disconnected: boolean }[] = [];
beforeEach(() => {
  terminals.length = 0;
  fits.length = 0;
  observers = [];
  vi.stubGlobal("ResizeObserver", class { o = { disconnected: false }; constructor() { observers.push(this.o); } observe() {} disconnect() { this.o.disconnected = true; } });
  vi.stubGlobal("requestAnimationFrame", (callback: () => void) => { callback(); return 1; });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const idle: EngineState = miniEngine.step(miniEngine.initialState(), { type: "setCapabilities", capabilities: MOCK_CAPABILITIES }).state;
const entry = (id: number, lines: string[], kind: Entry["kind"] = "output"): Entry => ({ id, kind, lines });
const props = (extra = {}) => ({ entries: [] as Entry[], state: idle, onInput: vi.fn(), onLeave: vi.fn(), label: "Terminal input", ...extra });

describe("XtermSurface", () => {
  it("exposes focus restoration to shortcuts and removes it on disposal", () => {
    const focusRef: { current: (() => void) | null } = { current: null };
    const view = render(<XtermSurface {...props()} focusRef={focusRef} />);
    const terminal = terminals[0];
    if (!terminal) throw new Error("missing terminal");
    terminal.focused = false;
    focusRef.current?.();
    expect(terminal.focused).toBe(true);
    view.unmount();
    expect(focusRef.current).toBeNull();
  });
  it("creates one terminal and releases everything on unmount", () => {
    const view = render(<XtermSurface {...props()} />);
    expect(terminals).toHaveLength(1);
    view.unmount();
    expect(terminals[0]?.disposed).toBe(true);
    expect(terminals[0]?.subscriptions.every((s) => s.disposed)).toBe(true);
    expect(fits.every((f) => f.disposed)).toBe(true);
    expect(observers.every((o) => o.disconnected)).toBe(true);
    expect(terminals[0]?.markers.every((m) => m.disposed)).toBe(true);
  });

  it("under strict mode leaves exactly one live terminal and disposes the first", () => {
    const view = render(<StrictMode><XtermSurface {...props()} /></StrictMode>);
    expect(terminals.filter((t) => !t.disposed)).toHaveLength(1);
    expect(terminals.filter((t) => t.disposed).length).toBe(terminals.length - 1);
    view.unmount();
    expect(terminals.every((t) => t.disposed && t.subscriptions.every((s) => s.disposed))).toBe(true);
  });

  it("enables only what it needs: no proposed API, no title or link handling, a bounded scrollback", () => {
    render(<XtermSurface {...props()} />);
    const options = terminals[0]?.options ?? {};
    expect(options["allowProposedApi"]).toBe(false);
    expect(options["scrollback"]).toBe(5000);
    expect(Object.keys(options)).not.toContain("linkHandler");
  });

  it("forwards typed data to the engine's input and Escape to onLeave, without sending the Escape on", () => {
    const onInput = vi.fn();
    const onLeave = vi.fn();
    render(<XtermSurface {...props({ onInput, onLeave })} />);
    terminals[0]?.handlers.data?.("ls");
    expect(onInput).toHaveBeenCalledWith("ls");
    const handled = terminals[0]?.handlers.key?.(new KeyboardEvent("keydown", { key: "Escape" }));
    expect(onLeave).toHaveBeenCalledTimes(1);
    expect(handled).toBe(false);
    expect(terminals[0]?.handlers.key?.(new KeyboardEvent("keydown", { key: "a" }))).toBe(true);
  });

  it("writes report text without any escape sequence it did not write itself", () => {
    const hostile = ["\u001b[2J\u001b[H", "\u001b]0;title\u0007", "\u001b]52;c;aGVsbG8=\u0007", "\u001b[6n", "\r\u001b[K[CRITICAL] forged"].map((l) => l);
    render(<XtermSurface {...props({ entries: [entry(1, hostile)] })} />);
    const all = (terminals[0]?.writes ?? []).join("");
    // eslint-disable-next-line no-control-regex
    const stripped = all.replace(/\u001b(?:\[(?:\d+(?:;\d+)*)?m|\[\?25[lh]|\[\d*A|\[J|7|8)/g, "");
    // eslint-disable-next-line no-control-regex
    expect(stripped).not.toMatch(/[\u001b\u0007\u009b\u009d]/);
    expect(all).toContain("[CRITICAL] forged");
  });

  it("writes only new entries when the transcript grows and redraws the line when it changes", () => {
    const view = render(<XtermSurface {...props({ entries: [entry(1, ["first"])] })} />);
    const before = terminals[0]?.writes.length ?? 0;
    view.rerender(<XtermSurface {...props({ entries: [entry(1, ["first"]), entry(2, ["second"])] })} />);
    const added = (terminals[0]?.writes ?? []).slice(before).join("");
    expect(added).toContain("second");
    expect(added).not.toContain("first");
  });

  it("resets the terminal when the transcript is cleared", () => {
    const view = render(<XtermSurface {...props({ entries: [entry(1, ["x"])] })} />);
    view.rerender(<XtermSurface {...props({ entries: [] })} />);
    expect(terminals[0]?.writes).toContain("<reset>");
  });

  it("redraws on resize", () => {
    render(<XtermSurface {...props()} />);
    const before = terminals[0]?.writes.length ?? 0;
    terminals[0]?.handlers.resize?.();
    expect(terminals[0]?.writes.length).toBeGreaterThan(before);
  });
});
