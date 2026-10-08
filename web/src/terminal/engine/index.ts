/** Pure terminal model. The caller renders state and performs effects; no I/O occurs here. */
export type * from './types';
import type { EngineAction, EngineApi, EngineOptions, EngineState, LineState, Notice, StepResult, SubmitGate } from './types';
import { forbidden, quoteToken, tokenize } from './lexical';
import { boundaries, wordBoundary, wrapLine } from './line';
import { decodeInput, initialInputState } from './input';
import { suggestionsFor } from './suggestions';

function initialState(options: EngineOptions = {}): EngineState {
  const resolved = { historyLimit: 100, suggestionLimit: 50, maxLineLength: 4096, ...options };
  for (const [name, value] of Object.entries(resolved)) {
    if (!Number.isInteger(value) || value < (name === 'historyLimit' ? 0 : 1)) throw new RangeError('Invalid engine limit.');
  }
  return {
    options: resolved, line: { text: '', cursor: 0 }, revision: 0, completionSeq: 0, completionPending: null,
    tokenized: tokenize('', 0), history: { entries: [], position: null, draft: null }, pendingPaste: null,
    suggestions: { items: [], truncated: false, seq: 0, revision: 0, selected: null },
    capabilities: null, running: false, notice: null,
  };
}

function limitExceeded(state: EngineState, text: string): boolean {
  if (text.length > state.options.maxLineLength) return true;
  const limits = state.capabilities?.limits;
  const tokens = tokenize(text, text.length).tokens.filter(t => t.start !== t.end).map(t => t.value);
  if (tokens.length > (limits?.tokenCount ?? 64) || tokens.some(t => Array.from(t).length > (limits?.tokenChars ?? 256))) return true;
  return !!limits && new TextEncoder().encode(JSON.stringify({ argv: tokens, requestId: 'x'.repeat(64) })).length > limits.bodyBytes;
}

function canSubmit(state: EngineState): SubmitGate {
  if (!state.capabilities) return { allowed: false, reason: 'no_capabilities' };
  if (state.running) return { allowed: false, reason: 'running' };
  if (state.pendingPaste) return { allowed: false, reason: 'pending_paste' };
  if (state.tokenized.problem) return { allowed: false, reason: 'malformed' };
  const tokens = state.tokenized.tokens.filter(t => t.start !== t.end).map(t => t.value);
  if (tokens[0] === 'hlp') tokens.shift();
  const [first, ...rest] = tokens;
  if (!first) return { allowed: false, reason: 'empty' };
  if (forbidden(state.line.text) || limitExceeded(state, state.line.text)) return { allowed: false, reason: 'too_long' };
  return { allowed: true, argv: [first, ...rest] };
}

function context(state: EngineState, line: LineState = state.line): EngineState {
  const revision = state.revision + 1;
  const next: EngineState = { ...state, line, revision, tokenized: tokenize(line.text, line.cursor),
    completionPending: null, notice: null,
    suggestions: { items: [], truncated: false, seq: 0, revision, selected: null } };
  return { ...next, suggestions: { ...next.suggestions, ...suggestionsFor(next) } };
}

function notice(state: EngineState, value: Notice): StepResult { return { state: { ...state, notice: value }, effects: [] }; }

function insert(state: EngineState, text: string): EngineState {
  if (forbidden(text)) return { ...state, notice: { kind: 'paste_refused', reason: 'control_characters' } };
  const joined = state.line.text.slice(0, state.line.cursor) + text + state.line.text.slice(state.line.cursor);
  if (limitExceeded(state, joined)) return { ...state, notice: { kind: 'line_too_long' } };
  const desired = state.line.cursor + text.length;
  // Inserting a base before an existing combining mark may merge clusters; snap forward to the new boundary.
  const cursor = boundaries(joined).find(n => n >= desired) ?? joined.length;
  return context({ ...state, history: { ...state.history, position: null, draft: null } }, { text: joined, cursor });
}

function select(state: EngineState, index: number): EngineState {
  const candidate = state.suggestions.items[index];
  const token = state.tokenized.tokens[state.tokenized.active.index];
  if (!candidate || !token) return state;
  const replacement = quoteToken(candidate.label);
  const suffix = state.line.text.slice(token.end);
  const space = suffix.startsWith(' ') || suffix.startsWith('\t') ? '' : ' ';
  const text = state.line.text.slice(0, token.start) + replacement + space + suffix;
  if (forbidden(candidate.label) || limitExceeded(state, text)) return state;
  return context(state, { text, cursor: token.start + replacement.length + (space.length || 1) });
}

function step(state: EngineState, action: EngineAction): StepResult {
  const done = (next = state): StepResult => ({ state: next, effects: [] });
  switch (action.type) {
    case 'clearOutput': return { state, effects: [{ type: 'clearOutput' }] };
    case 'reset': {
      const fresh = initialState(state.options), revision = state.revision + 1;
      return done({ ...fresh, revision, completionSeq: state.completionSeq + 1,
        suggestions: { ...fresh.suggestions, revision } });
    }
    case 'setCapabilities': return done(context({ ...state, capabilities: action.capabilities ? structuredClone(action.capabilities) : null }));
    case 'executionFinished': return done(state.running ? { ...state, running: false } : state);
    case 'interrupt': return { state: state.running ? state : context({ ...state, pendingPaste: null }, { text: '', cursor: 0 }),
      effects: [{ type: 'interrupted', running: state.running }] };
    case 'inputRejected': return notice(state, { kind: 'paste_refused', reason: action.reason });
    case 'suggestionsArrived': {
      const pending = state.completionPending;
      if (!pending || pending.seq !== action.seq || pending.revision !== action.revision || state.revision !== action.revision) return done();
      // Remote text is never a new source of grammar: intersect labels with the current metadata suggestions.
      const local = suggestionsFor(state);
      const labels = new Set(action.candidates.map(c => c.label));
      return done({ ...state, completionPending: null, suggestions: { items: local.items.filter(c => labels.has(c.label)),
        truncated: local.truncated || action.truncated, selected: null, seq: action.seq, revision: action.revision } });
    }
    case 'submit': {
      const gate = canSubmit(state);
      if (!gate.allowed) {
        if (gate.reason === 'running') return notice(state, { kind: 'busy' });
        if (gate.reason === 'malformed' && state.tokenized.problem) return notice(state, { kind: 'malformed', problem: state.tokenized.problem });
        if (gate.reason === 'no_capabilities' || gate.reason === 'empty') return notice(state, { kind: gate.reason });
        return done();
      }
      const entries = [...state.history.entries];
      if (entries.at(-1) !== state.line.text) entries.push(state.line.text);
      const next = context({ ...state, running: true, history: {
        entries: state.options.historyLimit ? entries.slice(-state.options.historyLimit) : [], position: null, draft: null },
      }, { text: '', cursor: 0 });
      return { state: next, effects: [{ type: 'execute', argv: gate.argv, line: state.line.text }] };
    }
  }
  if (state.running) return done();
  switch (action.type) {
    case 'insert': return done(state.pendingPaste ? state : insert(state, action.text));
    case 'paste': {
      const normalized = action.text.replace(/\r\n?|\u0085|\u2028|\u2029/g, '\n').replace(/\t/g, ' ');
      if (forbidden(normalized.replace(/\n/g, ''))) return notice(state, { kind: 'paste_refused', reason: 'control_characters' });
      const lines = normalized.split('\n'), joined = lines.join(' ');
      const text = state.line.text.slice(0, state.line.cursor) + joined + state.line.text.slice(state.line.cursor);
      if (limitExceeded(state, text)) return notice(state, { kind: 'paste_refused', reason: 'too_long' });
      if (lines.length === 1) return done(insert(state, joined));
      return done(context({ ...state, pendingPaste: { lines, joined } }));
    }
    case 'confirmPaste': return done(state.pendingPaste ? insert({ ...state, pendingPaste: null }, state.pendingPaste.joined) : state);
    case 'cancelPaste': return done(state.pendingPaste ? context({ ...state, pendingPaste: null }) : state);
    case 'clearHistory': return done({ ...state, history: { entries: [], position: null, draft: null } });
    case 'historyPrevious': case 'historyNext': {
      if (state.pendingPaste || !state.history.entries.length) return done();
      const { entries, position, draft } = state.history;
      if (action.type === 'historyNext' && position === null) return done();
      const at = action.type === 'historyPrevious' ? Math.max(0, (position ?? entries.length) - 1) : (position ?? 0) + 1;
      if (at >= entries.length) return done(context({ ...state, history: { entries, position: null, draft: null } }, draft ?? { text: '', cursor: 0 }));
      const text = entries[at] ?? '';
      return done(context({ ...state, history: { entries, position: at, draft: draft ?? state.line } }, { text, cursor: text.length }));
    }
    case 'tab': {
      if (!state.capabilities || state.pendingPaste) return done();
      if (state.suggestions.selected !== null) return done(select(state, state.suggestions.selected));
      const local = suggestionsFor(state);
      const next = { ...state, suggestions: { ...state.suggestions, ...local, selected: null } };
      if (local.items.length === 1 && !local.truncated) return done(select(next, 0));
      const seq = state.completionSeq + 1;
      const argv = state.tokenized.tokens.map(t => t.value);
      if (argv.some(forbidden) || limitExceeded(state, state.line.text)) return done(next);
      return { state: { ...next, completionSeq: seq, completionPending: { seq, revision: state.revision } },
        effects: [{ type: 'complete', seq, revision: state.revision, argv,
          tokenIndex: state.tokenized.active.index, cursor: state.tokenized.active.prefixCodepoints }] };
    }
    case 'selectSuggestion': return done(state.pendingPaste ? state : select(state, action.index));
    case 'suggestNext': case 'suggestPrevious': {
      const count = state.suggestions.items.length;
      if (!count) return done();
      const selected = state.suggestions.selected;
      const at = selected === null ? (action.type === 'suggestNext' ? 0 : count - 1)
        : (selected + (action.type === 'suggestNext' ? 1 : -1) + count) % count;
      return done({ ...state, suggestions: { ...state.suggestions, selected: at } });
    }
    case 'dismissSuggestions': return done({ ...state, completionPending: null,
      suggestions: { ...state.suggestions, items: [], truncated: false, selected: null } });
  }
  if (state.pendingPaste) return done();
  const { text, cursor } = state.line;
  const points = boundaries(text);
  const previous = [...points].reverse().find(n => n < cursor) ?? 0;
  const next = points.find(n => n > cursor) ?? text.length;
  let target = cursor, start = cursor, end = cursor;
  switch (action.type) {
    case 'left': target = previous; break;
    case 'right': target = next; break;
    case 'home': target = 0; break;
    case 'end': target = text.length; break;
    case 'wordLeft': target = wordBoundary(state.line, false); break;
    case 'wordRight': target = wordBoundary(state.line, true); break;
    case 'backspace': start = previous; break;
    case 'delete': end = next; break;
    case 'deleteWordBack': start = wordBoundary(state.line, false); break;
    case 'deleteWordForward': end = wordBoundary(state.line, true); break;
    case 'deleteToStart': start = 0; break;
    case 'deleteToEnd': end = text.length; break;
    case 'clearLine': start = 0; end = text.length; break;
    default: return done();
  }
  if (start !== end) {
    const joined = text.slice(0, start) + text.slice(end);
    const snapped = [...boundaries(joined)].reverse().find(n => n <= start) ?? 0;
    return done(context({ ...state, history: { ...state.history, position: null, draft: null } }, { text: joined, cursor: snapped }));
  }
  return done(target === cursor ? state : context(state, { text, cursor: target }));
}

export const engine: EngineApi = { initialState, step, canSubmit, tokenize, quoteToken, suggestionsFor,
  wrapLine, initialInputState, decodeInput };
