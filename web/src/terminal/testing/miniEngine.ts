/* eslint-disable no-control-regex -- an input decoder is about control characters */
/**
 * A small stand-in for the command-line engine (#275) so the panel (#276) can be built, tested and previewed before
 * the real engine exists. It follows the contract in `engine/types.ts` but only just: it edits by code point (not by
 * grapheme), has no word movement, no paste confirmation and the simplest suggestions. It is NOT the product:
 * `defaultEngine.ts` switches to the real engine when #275 lands, and this file is then used by tests only.
 */
import type {
  Argv, Candidate, Capabilities, DecodedInput, EngineAction, EngineApi, EngineOptions, EngineState, Effect, LineState, Notice,
  InputState, StepResult, SubmitGate, Tokenized, Token, WrappedLine,
} from "../engine/types";

const DEFAULTS: Required<EngineOptions> = { historyLimit: 100, suggestionLimit: 50, maxLineLength: 4096 };
const UNQUOTED = /^[A-Za-z0-9_@%+=:,./-]+$/;

function tokenize(text: string, cursor: number): Tokenized {
  const tokens: Token[] = [];
  let i = 0;
  let problem: Tokenized["problem"] = null;
  while (i < text.length) {
    while (i < text.length && text[i] === " ") i++;
    if (i >= text.length) break;
    const start = i;
    let value = "";
    let quoted = false;
    let quote = "";
    while (i < text.length && (quote !== "" || text[i] !== " ")) {
      const ch = text[i] ?? "";
      if (quote !== "") {
        if (ch === quote) quote = "";
        else value += ch;
      } else if (ch === '"' || ch === "'") {
        quote = ch;
        quoted = true;
      } else if (ch === "\\") {
        quoted = true;
        if (i + 1 >= text.length) problem = { kind: "dangling_escape", at: i };
        else value += text[++i] ?? "";
      } else value += ch;
      i++;
    }
    if (quote !== "") problem = { kind: "unterminated_quote", at: start };
    tokens.push({ value, start, end: i, quoted });
  }
  let index = tokens.findIndex((t) => cursor >= t.start && cursor <= t.end);
  let prefixCodepoints = 0;
  if (index < 0) {
    index = tokens.length;
  } else {
    const t = tokens[index];
    prefixCodepoints = t ? Array.from(text.slice(t.start, cursor)).length : 0;
  }
  return { tokens, problem, active: { index, prefixCodepoints } };
}

function quoteToken(value: string): string {
  return UNQUOTED.test(value) ? value : `'${value.replaceAll("'", "'\\''")}'`;
}

function argvOf(tokens: readonly Token[]): string[] {
  const values = tokens.map((t) => t.value);
  return values[0] === "hlp" ? values.slice(1) : values;
}

function suggestionsFor(state: EngineState): { items: Candidate[]; truncated: boolean } {
  const caps = state.capabilities;
  if (!caps) return { items: [], truncated: false };
  const { tokens, active } = state.tokenized;
  const prefix = (tokens[active.index]?.value ?? "").slice(0, active.prefixCodepoints);
  const first = tokens[0]?.value === "hlp" ? 1 : 0;
  let items: Candidate[];
  if (active.index <= first) {
    items = [...caps.commands.filter((c) => c.status !== "unavailable").map((c) => ({ label: c.name, description: c.description, kind: "command" as const })),
      ...caps.staticOperations.map((name) => ({ label: name, description: "Static", kind: "command" as const }))];
  } else if (prefix.startsWith("-")) {
    const command = caps.commands.find((c) => c.name === tokens[first]?.value);
    items = [...(command?.options ?? []), ...caps.globalOptions].filter((o) => o.status !== "unavailable")
      .flatMap((o) => o.flags.map((flag) => ({ label: flag, description: o.description, kind: "option" as const })));
  } else {
    const command = caps.commands.find((c) => c.name === tokens[first]?.value);
    items = (command?.choices ?? []).map((label) => ({ label, description: "", kind: "choice" as const }));
  }
  items = items.filter((c) => c.label.startsWith(prefix)).sort((a, b) => a.label.localeCompare(b.label));
  return { items: items.slice(0, state.options.suggestionLimit), truncated: items.length > state.options.suggestionLimit };
}

function gate(state: EngineState): SubmitGate {
  if (!state.capabilities) return { allowed: false, reason: "no_capabilities" };
  if (state.running) return { allowed: false, reason: "running" };
  if (state.tokenized.problem) return { allowed: false, reason: "malformed" };
  const argv = argvOf(state.tokenized.tokens);
  if (argv.length === 0) return { allowed: false, reason: "empty" };
  return { allowed: true, argv: argv as Argv };
}

function withLine(state: EngineState, text: string, cursor: number, extra: Partial<EngineState> = {}): EngineState {
  const tokenized = tokenize(text, cursor);
  const next = { ...state, ...extra, line: { text, cursor }, revision: state.revision + 1, tokenized, notice: null };
  // Like the real engine must: the metadata-based suggestions for the token under the cursor are always in the state
  // (seq 0), so the shortcut toolbar and Tab read the same list and `selectSuggestion` indexes into it.
  return { ...next, suggestions: { ...suggestionsFor(next), seq: 0, revision: next.revision, selected: null } };
}

function initialState(options: EngineOptions = {}): EngineState {
  return {
    options: { ...DEFAULTS, ...options }, line: { text: "", cursor: 0 }, revision: 0, tokenized: tokenize("", 0),
    history: { entries: [], position: null, draft: null }, pendingPaste: null,
    suggestions: { items: [], truncated: false, seq: 0, revision: 0, selected: null },
    completionSeq: 0, completionPending: null, executionSeq: 0, activeExecution: null,
    capabilities: null, running: false, notice: null,
  };
}

function step(state: EngineState, action: EngineAction): StepResult {
  const { text, cursor } = state.line;
  const chars = Array.from(text);
  const before = Array.from(text.slice(0, cursor));
  const insert = (s: string) => withLine(state, text.slice(0, cursor) + s + text.slice(cursor), cursor + s.length);
  const none = (s: EngineState): StepResult => ({ state: s, effects: [] });
  // While a command runs the prompt is locked: only Ctrl+C, a finished execution, a new command list and a reset get through.
  if (state.running && !["interrupt", "executionFinished", "setCapabilities", "reset", "submit"].includes(action.type)) return none(state);
  switch (action.type) {
    case "insert": return /[\u0000-\u001f\u007f]/.test(action.text) ? none(state) : none(insert(action.text));
    case "paste": return /[\u0000-\u0008\u000b-\u001f\u007f]/.test(action.text.replace(/\r?\n/g, " "))
      ? none({ ...state, notice: { kind: "paste_refused", reason: "control_characters" } })
      : none(insert(action.text.replace(/\s*\r?\n\s*/g, " ")));
    case "left": return none(withLine(state, text, cursor - (before.at(-1)?.length ?? 0)));
    case "right": { const next = chars[before.length]; return none(withLine(state, text, cursor + (next?.length ?? 0))); }
    case "home": return none(withLine(state, text, 0));
    case "end": return none(withLine(state, text, text.length));
    case "backspace": { const last = before.at(-1); return last ? none(withLine(state, text.slice(0, cursor - last.length) + text.slice(cursor), cursor - last.length)) : none(state); }
    case "delete": { const next = chars[before.length]; return next ? none(withLine(state, text.slice(0, cursor) + text.slice(cursor + next.length), cursor)) : none(state); }
    case "clearLine": return none(withLine(state, "", 0));
    case "setCapabilities": return none(withLine({ ...state, capabilities: action.capabilities }, text, cursor));
    case "historyPrevious": {
      const h = state.history; if (h.entries.length === 0) return none(state);
      const position = h.position === null ? h.entries.length - 1 : Math.max(0, h.position - 1);
      const entry = h.entries[position] ?? "";
      return none(withLine(state, entry, entry.length, { history: { ...h, position, draft: h.draft ?? state.line } }));
    }
    case "historyNext": {
      const h = state.history; if (h.position === null) return none(state);
      if (h.position >= h.entries.length - 1) { const d: LineState = h.draft ?? { text: "", cursor: 0 }; return none(withLine(state, d.text, d.cursor, { history: { ...h, position: null, draft: null } })); }
      const entry = h.entries[h.position + 1] ?? "";
      return none(withLine(state, entry, entry.length, { history: { ...h, position: h.position + 1 } }));
    }
    case "tab": {
      const found = suggestionsFor(state);
      const only = found.items.length === 1 ? found.items[0] : undefined;
      if (only) return none(replaceActive(state, only.label));
      return none({ ...state, suggestions: { ...state.suggestions, ...found, revision: state.revision, selected: found.items.length ? 0 : null } });
    }
    case "selectSuggestion": {
      const item = action.index >= 0 ? state.suggestions.items[action.index] : undefined;
      return item ? none(replaceActive(state, item.label)) : none(state);
    }
    case "dismissSuggestions": return none({ ...state, suggestions: { ...state.suggestions, selected: null } });
    case "submit": {
      const g = gate(state);
      if (!g.allowed) {
        const notice: Notice = g.reason === "running" ? { kind: "busy" } : g.reason === "no_capabilities" ? { kind: "no_capabilities" } : g.reason === "empty" ? { kind: "empty" } : { kind: "malformed", problem: state.tokenized.problem ?? { kind: "unterminated_quote", at: 0 } };
        return none({ ...state, notice });
      }
      const entries = state.history.entries.at(-1) === text ? state.history.entries : [...state.history.entries, text].slice(-state.options.historyLimit);
      const seq = state.executionSeq + 1;
      const effect: Effect = { type: "execute", seq, argv: g.argv, line: text };
      const running = withLine(state, "", 0, { running: true, executionSeq: seq, activeExecution: seq, history: { entries, position: null, draft: null } });
      return { state: running, effects: [effect] };
    }
    case "interrupt": return { state: state.running ? state : withLine(state, "", 0), effects: [{ type: "interrupted", running: state.running }] };
    case "executionFinished": return none(action.seq === state.activeExecution ? { ...state, running: false, activeExecution: null } : state);
    case "reset": return none(initialState(state.options));
    default: return none(state);
  }
}

function replaceActive(state: EngineState, label: string): EngineState {
  const { tokens, active } = state.tokenized;
  const t = tokens[active.index];
  const value = quoteToken(label);
  if (!t) { const text = state.line.text + value; return withLine(state, text, text.length); }
  const text = state.line.text.slice(0, t.start) + value + state.line.text.slice(t.end);
  return withLine(state, text, t.start + value.length);
}

function wrapLine(line: LineState, columns: number, promptWidth: number): WrappedLine {
  const rows: string[] = [];
  let current = "";
  let width = promptWidth;
  let cursorPos = { row: 0, column: promptWidth };
  let index = 0;
  const place = () => { cursorPos = { row: rows.length, column: width }; };
  for (const ch of Array.from(line.text)) {
    if (index === line.cursor) place();
    if (width >= columns) { rows.push(current); current = ""; width = 0; if (index === line.cursor) place(); }
    current += ch; width += 1; index += ch.length;
  }
  if (index === line.cursor) place();
  rows.push(current);
  return { rows, cursor: cursorPos };
}

const KEYS: Record<string, EngineAction> = {
  "\r": { type: "submit" }, "\t": { type: "tab" }, "\x7f": { type: "backspace" }, "\x03": { type: "interrupt" },
  "\x1b[A": { type: "historyPrevious" }, "\x1b[B": { type: "historyNext" }, "\x1b[C": { type: "right" }, "\x1b[D": { type: "left" },
  "\x1b[H": { type: "home" }, "\x1b[F": { type: "end" }, "\x1b[3~": { type: "delete" }, "\x01": { type: "home" }, "\x05": { type: "end" }, "\x15": { type: "clearLine" },
};

const IDLE_INPUT: InputState = { buffer: "", paste: null, overflow: false, discardingCsi: false };

function decodeInput(data: string, state: InputState = IDLE_INPUT): DecodedInput {
  const paste = /^\x1b\[200~([\s\S]*)\x1b\[201~$/.exec(data);
  const actions = ((): readonly EngineAction[] => {
    if (paste) return [{ type: "paste", text: paste[1] ?? "" }];
    const key = KEYS[data];
    if (key) return [key];
    if (data.startsWith("\x1b")) return [];
    return data.length > 1 && /[\r\n]/.test(data) ? [{ type: "paste", text: data }] : [{ type: "insert", text: data }];
  })();
  return { state, actions };
}

export const miniEngine: EngineApi = { initialInputState: () => IDLE_INPUT, initialState, step, canSubmit: gate, tokenize, quoteToken, suggestionsFor, wrapLine, decodeInput };
export type { Capabilities };
