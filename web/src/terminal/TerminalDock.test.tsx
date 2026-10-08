import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode, useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { PANEL_HEIGHT_KEY } from "../theme/storage";
import type { ExecuteOutcome, TerminalBackend } from "./backend";
import { MOCK_CAPABILITIES } from "./mock";
import { miniEngine } from "./testing/miniEngine";
import { TerminalDock, type DockMode } from "./TerminalDock";

// The xterm surface is replaced by a stand-in with a "key" button per key, so these tests are about the panel and
// the session, not about the library (XtermSurface.test.tsx covers that). The factory runs when the module is first
// imported, which is how "nothing is loaded before the panel opens" is checked.
const loads = vi.hoisted(() => ({ count: 0 }));
vi.mock("./XtermSurface", () => {
  loads.count += 1;
  return {
    default: ({ onInput, onLeave, entries }: { onInput: (d: string) => void; onLeave: () => void; entries: { lines: readonly string[] }[] }) => (
      <div data-testid="surface">
        <button type="button" onClick={() => { for (const key of "info") onInput(key); }}>type info</button>
        <button type="button" onClick={() => { onInput("\r"); onInput("\r"); }}>double enter</button>
        <button type="button" onClick={() => { onInput("\r"); }}>enter</button>
        <button type="button" onClick={() => { onInput("\x03"); }}>ctrl-c</button>
        <button type="button" onClick={onLeave}>escape</button>
        <div data-testid="written">{entries.map((e) => e.lines.join("|")).join("\n")}</div>
      </div>
    ),
  };
});

interface Controls { resolve(outcome: ExecuteOutcome): void; backend: TerminalBackend; executed: string[][]; loaded: number }
function backend(options: { fail?: boolean } = {}): Controls {
  const pending: ((outcome: ExecuteOutcome) => void)[] = [];
  const controls: Controls = {
    executed: [], loaded: 0, resolve: (outcome) => { pending.shift()?.(outcome); },
    backend: {
      loadCapabilities: () => { controls.loaded += 1; return options.fail ? Promise.reject(new Error("no")) : Promise.resolve(MOCK_CAPABILITIES); },
      execute: (argv) => { controls.executed.push([...argv]); return new Promise((resolve) => { pending.push(resolve); }); },
    },
  };
  return controls;
}

function Host({ controls, initial = "open" }: { controls: Controls; initial?: DockMode }) {
  const [mode, setMode] = useState(initial);
  return (
    <>
      <button type="button" onClick={() => { setMode("open"); }}>reopen</button>
      <TerminalDock services={{ engine: miniEngine, backend: controls.backend }} mode={mode} onMode={setMode} />
    </>
  );
}

beforeEach(() => { loads.count = 0; });

const click = (name: string) => userEvent.click(screen.getByRole("button", { name }));

describe("the panel", () => {
  it("loads nothing and shows nothing while closed", () => {
    render(<Host controls={backend()} initial="closed" />);
    expect(screen.queryByRole("region", { name: "Terminal" })).toBeNull();
    expect(loads.count).toBe(0);
  });

  it("loads the terminal chunk and the command list when first opened, once even in strict mode", async () => {
    const controls = backend();
    render(<StrictMode><Host controls={controls} initial="closed" /></StrictMode>);
    await click("reopen");
    expect(await screen.findByTestId("surface")).toBeInTheDocument();
    expect(loads.count).toBe(1);
    await waitFor(() => { expect(screen.getByText("Ready")).toBeInTheDocument(); });
    expect(controls.loaded).toBe(1);
  });

  it("opens, collapses, expands, restores and closes, with a name on every control", async () => {
    render(<Host controls={backend()} />);
    expect(screen.getByRole("region", { name: "Terminal" })).toHaveAttribute("data-mode", "open");
    await click("Collapse the terminal panel");
    expect(screen.getByRole("region", { name: "Terminal" })).toHaveAttribute("data-mode", "collapsed");
    await click("Expand the terminal panel");
    await click("Fill the page with the terminal");
    expect(screen.getByRole("region", { name: "Terminal" })).toHaveAttribute("data-mode", "expanded");
    expect(screen.getByRole("button", { name: "Restore the terminal panel size" })).toHaveAttribute("aria-pressed", "true");
    await click("Restore the terminal panel size");
    expect(screen.getByRole("button", { name: "Clear the terminal output" })).toBeInTheDocument();
    await click("Close the terminal panel");
    expect(screen.queryByRole("region", { name: "Terminal" })).toBeNull();
  });

  it("resizes with the keyboard, within limits, and remembers only the height", async () => {
    render(<Host controls={backend()} />);
    const handle = screen.getByRole("slider", { name: "Resize the terminal" });
    const start = Number(handle.getAttribute("aria-valuenow"));
    handle.focus();
    await userEvent.keyboard("{ArrowUp}");
    expect(Number(handle.getAttribute("aria-valuenow"))).toBe(start + 24);
    expect(window.localStorage.getItem(PANEL_HEIGHT_KEY)).toBe(String(start + 24));
    await userEvent.keyboard("{Home}");
    expect(Number(handle.getAttribute("aria-valuenow"))).toBe(160);
    expect(Object.keys(window.localStorage)).toEqual([PANEL_HEIGHT_KEY]);
  });

  it("keeps room for itself on the page and gives it back when closed", async () => {
    render(<Host controls={backend()} />);
    expect(document.documentElement.style.getPropertyValue("--dock-space")).not.toBe("0px");
    await click("Close the terminal panel");
    expect(document.documentElement.style.getPropertyValue("--dock-space")).toBe("0px");
  });
});

describe("running commands", () => {
  async function ready(controls = backend()) {
    render(<Host controls={controls} />);
    await waitFor(() => { expect(screen.getByText("Ready")).toBeInTheDocument(); });
    return controls;
  }

  it("runs a typed command once, shows its output and is usable again", async () => {
    const controls = await ready();
    await click("type info");
    await click("enter");
    expect(controls.executed).toEqual([["info"]]);
    expect(screen.getByText("Running")).toBeInTheDocument();
    await act(() => { controls.resolve({ kind: "output", text: "Application: 10.6", truncated: false }); return Promise.resolve(); });
    expect(screen.getByTestId("written")).toHaveTextContent("Application: 10.6");
    expect(screen.getByText("Ready")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("The command finished.");
  });

  it("never issues a second request: rapid Enter, or Enter while running", async () => {
    const controls = await ready();
    await click("type info");
    await click("double enter");
    await click("enter");
    expect(controls.executed).toHaveLength(1);
    expect(screen.getByRole("status")).toHaveTextContent("still running");
  });

  it("does nothing for an empty line", async () => {
    const controls = await ready();
    await click("enter");
    expect(controls.executed).toEqual([]);
    expect(screen.getByRole("status")).toHaveTextContent("Type a command first.");
  });

  it("ctrl-c while running says that waiting stopped, never that the command was cancelled, and the prompt stays locked", async () => {
    const controls = await ready();
    await click("type info");
    await click("enter");
    await click("ctrl-c");
    const text = screen.getByTestId("written").textContent;
    expect(text).toContain("Stopped waiting. The command may still be running on the server.");
    expect(text).not.toMatch(/cancel|terminat|killed/i);
    expect(screen.getByText("Running")).toBeInTheDocument();
    await click("type info");
    await click("enter");
    expect(controls.executed).toHaveLength(1);
    await act(() => { controls.resolve({ kind: "output", text: "late", truncated: false }); return Promise.resolve(); });
    expect(screen.getByText("Ready")).toBeInTheDocument();
  });

  it("shows failures as did-not-run or outcome-unknown, and a truncated output as incomplete", async () => {
    const controls = await ready();
    await click("type info");
    await click("enter");
    await act(() => { controls.resolve({ kind: "failed", message: "Nope.", ran: "no" }); return Promise.resolve(); });
    expect(screen.getByTestId("written")).toHaveTextContent("Did not run: Nope.");
    await click("type info");
    await click("enter");
    await act(() => { controls.resolve({ kind: "failed", message: "Timed out.", ran: "unknown" }); return Promise.resolve(); });
    expect(screen.getByTestId("written")).toHaveTextContent("Outcome unknown: Timed out.");
    await click("type info");
    await click("enter");
    await act(() => { controls.resolve({ kind: "output", text: "part", truncated: true }); return Promise.resolve(); });
    expect(screen.getByTestId("written")).toHaveTextContent("The server cut the output: it is incomplete.");
  });

  it("bounds a long report and says so", async () => {
    const controls = await ready();
    await click("type info");
    await click("enter");
    await act(() => { controls.resolve({ kind: "output", text: Array.from({ length: 2500 }, (_, i) => `line ${i}`).join("\n"), truncated: false }); return Promise.resolve(); });
    expect(screen.getByTestId("written")).toHaveTextContent("Output truncated: showing the first 2000 of 2500 lines.");
  });

  it("hiding the panel never cancels a running command and says so", async () => {
    const controls = await ready();
    await click("type info");
    await click("enter");
    await click("Collapse the terminal panel");
    expect(screen.getByText("A command is still running.")).toBeInTheDocument();
    await click("Close the terminal panel (the command keeps running)");
    await act(() => { controls.resolve({ kind: "output", text: "finished while hidden", truncated: false }); return Promise.resolve(); });
    await click("reopen");
    expect(screen.getByTestId("written")).toHaveTextContent("finished while hidden");
  });

  it("with the command list unavailable, says so and Enter does nothing", async () => {
    const controls = backend({ fail: true });
    render(<Host controls={controls} />);
    await waitFor(() => { expect(screen.getByText("Unavailable")).toBeInTheDocument(); });
    await click("type info");
    await click("enter");
    expect(controls.executed).toEqual([]);
    expect(screen.getByRole("status")).toHaveTextContent(/unavailable/i);
  });

  it("puts report text in the transcript log as plain text without control characters", async () => {
    const controls = await ready();
    await click("type info");
    await click("enter");
    await act(() => { controls.resolve({ kind: "output", text: "ok\u001b[2J\u001b]0;t\u0007done", truncated: false }); return Promise.resolve(); });
    const log = screen.getByRole("log", { name: "Terminal transcript" });
    // eslint-disable-next-line no-control-regex
    expect(log.textContent).not.toMatch(/[\u001b\u0007]/);
    expect(within(log).getByText(/ok\[2J\]0;tdone/)).toBeInTheDocument();
  });

  it("shortcut buttons add text and never run it", async () => {
    const controls = await ready();
    await userEvent.click(await screen.findByRole("button", { name: "diagnose" }));
    expect(controls.executed).toEqual([]);
    expect(screen.getByRole("toolbar", { name: "Shortcuts" })).toBeInTheDocument();
  });

  it("Escape hands focus to the panel so the keyboard is never trapped", async () => {
    await ready();
    await click("escape");
    expect(screen.getByRole("region", { name: "Terminal" })).toHaveFocus();
  });
});
