import { describe, expect, it } from "vitest";

import { cleanLine, cleanLines } from "./clean";
import { miniEngine } from "./testing/miniEngine";
import { MOCK_CAPABILITIES } from "./mock";
import { PROMPT, renderEntry, renderErase, renderLive, suggestionLine, type Entry, type Palette } from "./render";

const PALETTE: Palette = { text: "#e8eefb", muted: "#9fb0cf", accent: "#3b9bff", brand: "#ff7a1a", warning: "#ffd27a", danger: "#ff9b8f", success: "#7be3a8" };

// The only escape sequences this module may write: colours/styles, cursor save and restore, cursor show and hide,
// moving up, and erasing downwards. Anything else left after removing them is a leak.
// eslint-disable-next-line no-control-regex
const OWN_SEQUENCES = /\u001b(?:\[(?:\d+(?:;\d+)*)?m|\[\?25[lh]|\[\d*A|\[J|7|8)/g;
const HOSTILE = [
  "\u001b[2J\u001b[Hclear", "\u001b]0;title\u0007", "\u001b]52;c;aGVsbG8=\u0007", "\u001b]8;;https://example.invalid\u0007x\u001b]8;;\u0007",
  "\u001b[6n", "\u001bP1$r\u001b\\", "\u009b2J", "\u009d0;title\u009c", "\r\u001b[K[CRITICAL] forged", "\u0007\u0008\u0000", "a\u202eb\u200bc", "\u001bc",
];

// eslint-disable-next-line no-control-regex
const hasStrayEscape = (text: string) => /[\u001b\u009b\u009d\u0090\u009e\u009f\u0007\u0000]/.test(text.replace(OWN_SEQUENCES, ""));

describe("cleanLine and cleanLines", () => {
  it("remove every control character, the escape character first, and the direction and zero-width tricks", () => {
    // eslint-disable-next-line no-control-regex
    for (const hostile of HOSTILE) expect(cleanLine(hostile)).not.toMatch(/[\u0000-\u0008\u000b-\u001f\u007f-\u009f\u200b\u202a-\u202e\u2060-\u2069\ufeff]/);
    expect(cleanLine("\u001b[31mred\u001b[0m")).toBe("[31mred[0m");
  });

  it("split on every kind of line break and keep indentation", () => {
    expect(cleanLines("a\r\nb\rc\nd\u2028e\u0085f")).toEqual(["a", "b", "c", "d", "e", "f"]);
    expect(cleanLines("  indented\ttab")).toEqual(["  indented    tab"]);
  });
});

describe("renderEntry", () => {
  it("never lets report text carry an escape sequence of its own", () => {
    for (const kind of ["command", "output", "notice", "warning", "error"] as const) {
      const entry: Entry = { id: 1, kind, lines: HOSTILE.flatMap(cleanLines) };
      expect(hasStrayEscape(renderEntry(entry, PALETTE))).toBe(false);
    }
  });

  it("also cleans lines that were not cleaned by the caller (defence in depth)", () => {
    const rendered = renderEntry({ id: 1, kind: "output", lines: ["\u001b]0;pwned\u0007text"] }, PALETTE);
    expect(rendered).toContain("]0;pwned");
    expect(hasStrayEscape(rendered)).toBe(false);
  });

  it("makes each kind visibly different and every line ends with CRLF and a reset", () => {
    const shown = (kind: Entry["kind"]) => renderEntry({ id: 1, kind, lines: ["x"] }, PALETTE);
    const kinds = ["command", "output", "notice", "warning", "error"] as const;
    expect(new Set(kinds.map(shown)).size).toBe(5);
    expect(shown("command")).toContain(PROMPT);
    expect(shown("error")).toContain("✕");
    expect(shown("warning")).toContain("!");
    for (const kind of kinds) expect(shown(kind).endsWith("\u001b[0m\r\n")).toBe(true);
  });

  it("cannot forge a status line: a fake prompt or CRITICAL tag in output stays an output line", () => {
    const forged = renderEntry({ id: 1, kind: "output", lines: ["\r\u001b[K[CRITICAL] all clear"] }, PALETTE);
    expect(forged).not.toContain("\r\u001b[K");
    expect(forged.split("\r\n").filter(Boolean)).toHaveLength(1);
  });
});

describe("renderLive", () => {
  const typed = (text: string) => {
    let state = miniEngine.step(miniEngine.initialState(), { type: "setCapabilities", capabilities: MOCK_CAPABILITIES }).state;
    for (const action of miniEngine.decodeInput(text, miniEngine.initialInputState()).actions) state = miniEngine.step(state, action).state;
    return state;
  };

  it("draws the prompt, the line and puts the cursor back where the engine says it is", () => {
    const drawn = renderLive(typed("query cl"), PALETTE);
    expect(drawn).toContain(`${PROMPT}`);
    expect(drawn).toContain("query cl");
    expect(drawn.indexOf("\u001b7")).toBeLessThan(drawn.indexOf("\u001b8"));
    expect(drawn.startsWith("\u001b[?25l")).toBe(true);
    expect(drawn.endsWith("\u001b[?25h")).toBe(true);
  });

  it("splits the line at the cursor so a mid-line edit shows the rest after the saved position", () => {
    let state = typed("query clients");
    state = miniEngine.step(state, { type: "left" }).state;
    const drawn = renderLive(state, PALETTE);
    expect(drawn.indexOf("query client")).toBeLessThan(drawn.indexOf("\u001b7"));
    expect(drawn.indexOf("\u001b7")).toBeLessThan(drawn.lastIndexOf("s"));
  });

  it("is clean for a hostile line, notice and suggestion", () => {
    const state = { ...typed("x"), line: { text: HOSTILE.join(" "), cursor: 3 } };
    expect(hasStrayEscape(renderLive(state, PALETTE))).toBe(false);
    expect(hasStrayEscape(suggestionLine([{ label: HOSTILE.join(""), description: "", kind: "choice" }], 0, PALETTE))).toBe(false);
  });

  it("says that waiting can be stopped while a command runs, and draws no prompt", () => {
    const running = { ...typed("info"), running: true };
    const drawn = renderLive(running, PALETTE);
    expect(drawn).toContain("waiting for the result");
    expect(drawn).not.toContain(PROMPT);
  });

  it("shows the notice and the highlighted suggestions below the line", () => {
    let state = typed("");
    state = miniEngine.step(state, { type: "tab" }).state;
    expect(state.suggestions.selected).toBe(0);
    expect(renderLive(state, PALETTE)).toContain("\u001b[7mdiagnose");
    const empty = miniEngine.step(typed(""), { type: "submit" }).state;
    expect(renderLive(empty, PALETTE)).toContain("Type a command first.");
  });

  it("bounds the suggestions line", () => {
    const items = Array.from({ length: 30 }, (_, i) => ({ label: `c${i}`, description: "", kind: "command" as const }));
    expect(suggestionLine(items, null, PALETTE)).toContain("+22 more");
  });
});

it("renderErase moves up and clears downwards, nothing else", () => {
  expect(renderErase(0)).toBe("\r\u001b[J");
  expect(renderErase(3)).toBe("\u001b[3A\r\u001b[J");
});
