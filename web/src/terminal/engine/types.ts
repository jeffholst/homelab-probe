/**
 * The contract of the terminal command-line engine (issue #275), written before the engine so that the
 * terminal panel (#276) and the real API integration (#277) can be built against it in parallel.
 *
 * The engine is the single source of truth for the command line: what is typed, where the cursor is, the history,
 * what a paste may do, which suggestions apply and whether a submission is allowed. It is a set of PURE functions
 * over immutable state. xterm.js (#276) only renders the state and forwards keys; it never becomes the source of
 * truth, and a submitted command is never rebuilt from what the terminal displays.
 *
 * Rules for the module that implements this (`engine/index.ts` exports `engine: EngineApi`):
 *  - no DOM, no xterm, no timers, no network, no randomness, no browser storage, no `Date.now()` (callers pass
 *    what they need): every function is deterministic, so every behavior is unit-testable;
 *  - never mutate an argument; return new state (structural sharing is fine);
 *  - text positions in `LineState` are UTF-16 indexes into `text` that always sit on a grapheme-cluster boundary
 *    (use `Intl.Segmenter`); a boundary in the middle of an emoji or a combining sequence is a bug;
 *  - the engine interprets no shell syntax: `$(...)`, backticks, `;`, `&&`, `|`, `>`, `*` and `~` are ordinary
 *    characters. Only quotes (single and double) and a backslash escape have meaning, see `tokenize`.
 * Everything here is ADVISORY. The API revalidates every command; this module only makes typing pleasant and
 * stops obviously invalid input early.
 */
import type { TerminalCapabilitiesV1, TerminalCompleteResultV1 } from '../../generated';

export type Capabilities = TerminalCapabilitiesV1.CapabilitiesResult;
export type CommandMeta = TerminalCapabilitiesV1.CommandMetadata;
export type OptionMeta = TerminalCapabilitiesV1.OptionMetadata;
export type Candidate = TerminalCompleteResultV1.CompletionCandidate;
/** `[command, ...arguments]` with the optional leading `hlp` removed: exactly the `argv` of the execute request. */
export type Argv = [string, ...string[]];
/** Unquoted completion tokens, optionally including `hlp`; an empty line is [''], never []. */
export type CompletionArgv = [string, ...string[]];

// -- options ---------------------------------------------------------------------------------------------

export interface EngineOptions {
  /** History entries kept, oldest dropped first. Default 100. */
  readonly historyLimit?: number;
  /** Most suggestions kept in the state (the rest are dropped, `truncated` is set). Default 50. */
  readonly suggestionLimit?: number;
  /** Longest accepted line in UTF-16 units, an advisory cap on top of the token limits. Default 4096. */
  readonly maxLineLength?: number;
}

// -- the line --------------------------------------------------------------------------------------------

export interface LineState {
  readonly text: string;
  /** UTF-16 index into `text`, on a grapheme boundary, 0..text.length. */
  readonly cursor: number;
}

/** One token of the line. `value` is the UNQUOTED literal that goes into `argv`; the range is in `text`. */
export interface Token {
  readonly value: string;
  readonly start: number;
  readonly end: number;
  /** True when the token contained quotes or escapes (so inserting a replacement must re-quote it). */
  readonly quoted: boolean;
}

export type TokenizeProblem =
  | { readonly kind: 'unterminated_quote'; readonly at: number }
  | { readonly kind: 'dangling_escape'; readonly at: number };

export interface Tokenized {
  readonly tokens: readonly Token[];
  /** Malformed input is REPORTED, never repaired: `tokens` holds what was read, `problem` says what is wrong,
   * and the line cannot be submitted. */
  readonly problem: TokenizeProblem | null;
  /** The token the cursor is in or at the end of (a trailing space means a new empty token, `value` ''). */
  readonly active: { readonly index: number; readonly prefixCodepoints: number };
}

// -- history ---------------------------------------------------------------------------------------------

export interface HistoryState {
  /** Oldest first, no empty lines, no immediate duplicates, at most `historyLimit`. Memory only. */
  readonly entries: readonly string[];
  /** Position while browsing (index into `entries`), or null when not browsing. */
  readonly position: number | null;
  /** The unfinished line saved when browsing began, restored when moving past the newest entry. */
  readonly draft: LineState | null;
}

// -- paste -----------------------------------------------------------------------------------------------

export type PasteRefusal =
  | 'control_characters' // anything below U+0020 except tab and the newline kinds, DEL, C1 controls, bidi controls
  | 'too_long'; // over the line cap or the published token limits

/** A multi-line paste is never inserted silently: it waits here for an explicit confirmation. */
export interface PendingPaste {
  /** The lines as pasted, line breaks normalised; shown to the user for review. */
  readonly lines: readonly string[];
  /** What confirming would insert: the lines joined with single spaces (never submitted by itself). */
  readonly joined: string;
}

// -- suggestions -----------------------------------------------------------------------------------------

export interface SuggestionState {
  readonly items: readonly Candidate[];
  readonly truncated: boolean;
  /** Which request the items answer: the effect `complete`'s `seq`, or 0 for local, metadata-based items. */
  readonly seq: number;
  /** The line revision they were computed for; a result for another revision is ignored. */
  readonly revision: number;
  /** Index of the highlighted item, or null. */
  readonly selected: number | null;
}

// -- the whole state -------------------------------------------------------------------------------------

export interface EngineState {
  readonly options: Required<EngineOptions>;
  readonly line: LineState;
  /** Incremented on text, cursor, metadata and session changes; invalidates completion context. */
  readonly revision: number;
  /** Monotonic across reset; never reuse a request identity for a later session. */
  readonly completionSeq: number;
  readonly completionPending: { readonly seq: number; readonly revision: number } | null;
  readonly tokenized: Tokenized;
  readonly history: HistoryState;
  readonly pendingPaste: PendingPaste | null;
  readonly suggestions: SuggestionState;
  /** Metadata from `GET /api/v1/terminal/capabilities`; null until loaded or when it cannot be loaded. */
  readonly capabilities: Capabilities | null;
  /** True exactly while activeExecution is non-null. Only its matching completion can clear it. */
  readonly running: boolean;
  /** Incremented for each execute and reset; reset must never rewind it. */
  readonly executionSeq: number;
  /** The execute effect's seq, or null when idle; stale executionFinished actions are ignored. */
  readonly activeExecution: number | null;
  /** Last problem worth telling the user (a refused paste, a malformed line), cleared by the next edit. */
  readonly notice: Notice | null;
}

export type Notice =
  | { readonly kind: 'paste_refused'; readonly reason: PasteRefusal }
  | { readonly kind: 'malformed'; readonly problem: TokenizeProblem }
  | { readonly kind: 'no_capabilities' }
  | { readonly kind: 'busy' }
  | { readonly kind: 'empty' }
  | { readonly kind: 'line_too_long' };

// -- actions and effects ---------------------------------------------------------------------------------

export type EngineAction =
  // editing
  | { readonly type: 'insert'; readonly text: string } // typed text; control characters and newlines are refused
  | { readonly type: 'left' | 'right' | 'home' | 'end' | 'wordLeft' | 'wordRight' }
  | { readonly type: 'backspace' | 'delete' | 'deleteWordBack' | 'deleteWordForward' }
  | { readonly type: 'deleteToStart' | 'deleteToEnd' | 'clearLine' }
  // history
  | { readonly type: 'historyPrevious' | 'historyNext' | 'clearHistory' }
  // paste
  | { readonly type: 'paste'; readonly text: string }
  | { readonly type: 'inputRejected'; readonly reason: PasteRefusal }
  | { readonly type: 'confirmPaste' | 'cancelPaste' }
  // suggestions
  | { readonly type: 'tab' } // complete the unique candidate, else show candidates; may emit a `complete` effect
  | { readonly type: 'suggestNext' | 'suggestPrevious' | 'dismissSuggestions' }
  | { readonly type: 'selectSuggestion'; readonly index: number } // insert it; never executes
  | {
      readonly type: 'suggestionsArrived';
      readonly seq: number;
      readonly revision: number;
      readonly candidates: readonly Candidate[];
      readonly truncated: boolean;
    }
  // lifecycle
  | { readonly type: 'setCapabilities'; readonly capabilities: Capabilities | null }
  | { readonly type: 'submit' }
  | { readonly type: 'interrupt' } // Ctrl+C
  /** Echo the execute effect's seq; ignore it unless it equals state.activeExecution. The caller must
   * separately drop old-session output/errors and must not reconstruct state with initialState on logout. */
  | { readonly type: 'executionFinished'; readonly seq: number }
  | { readonly type: 'reset' }; // session ended: forget the line, history, drafts, suggestions and capabilities

/** What the caller must do. The engine never does I/O itself. */
export type Effect =
  /** Start exactly ONE request with this argv. The caller adds the `requestId` and records the submitted
   * command from `argv`, never from the terminal's display. Echo seq on executionFinished; it is an engine
   * generation, not an API requestId or authorization token. Emitted at most once until its matching completion. */
  | { readonly type: 'execute'; readonly seq: number; readonly argv: Argv; readonly line: string }
  /** Ask `POST /api/v1/terminal/complete` (the caller debounces and sends only on explicit Tab). `seq` and
   * `revision` come back in `suggestionsArrived`. */
  | { readonly type: 'complete'; readonly seq: number; readonly revision: number; readonly argv: CompletionArgv;
      readonly tokenIndex: number; readonly cursor: number }
  /** Ctrl+C. `running` tells the UI which message to show; while running the UI may only say that WAITING
   * stopped, never that the backend was cancelled. The line is cleared only when idle. */
  | { readonly type: 'interrupted'; readonly running: boolean };

export interface StepResult {
  readonly state: EngineState;
  /** Usually empty; at most one `execute` per call, and none while `running`. */
  readonly effects: readonly Effect[];
}

// -- gates and layout ------------------------------------------------------------------------------------

export type SubmitBlock = 'no_capabilities' | 'running' | 'empty' | 'malformed' | 'too_long' | 'pending_paste';
export type SubmitGate =
  | { readonly allowed: true; readonly argv: Argv }
  | { readonly allowed: false; readonly reason: SubmitBlock };

/** The line wrapped to a terminal `columns` wide, for drawing. Cell widths count wide (CJK, most emoji) as 2
 * and combining marks as 0; a wide character is never split across rows. Row 0 starts after the prompt, whose
 * width the caller passes as `promptWidth`. */
export interface WrappedLine {
  readonly rows: readonly string[];
  readonly cursor: { readonly row: number; readonly column: number };
}

/** Caller-owned streaming state. Clear it on session change/disposal along with EngineState. */
export interface InputState {
  readonly buffer: string;
  readonly paste: string | null;
  readonly overflow: boolean;
  readonly discardingCsi: boolean;
}

export interface DecodedInput {
  readonly state: InputState;
  readonly actions: readonly EngineAction[];
}

export interface EngineApi {
  initialState(options?: EngineOptions): EngineState;
  /** The one transition function. Unknown or impossible actions return the same state and no effects. */
  step(state: EngineState, action: EngineAction): StepResult;
  /** Whether Enter would execute, and the argv if so; the same gate `step(..., submit)` uses. */
  canSubmit(state: EngineState): SubmitGate;
  tokenize(text: string, cursor: number): Tokenized;
  /** A token value as it must be written on the line: bare when safe, otherwise quoted/escaped so that
   * `tokenize` reads back exactly `value`. */
  quoteToken(value: string): string;
  /** Metadata-based suggestions for the token under the cursor; `[]` without metadata. Context-sensitive:
   * the command (or none yet), options already used (aliases count; repeatable ones stay), options that take a
   * value or a choice, positional choices, comma lists. Bounded by `suggestionLimit`. */
  suggestionsFor(state: EngineState): { readonly items: readonly Candidate[]; readonly truncated: boolean };
  wrapLine(line: LineState, columns: number, promptWidth: number): WrappedLine;
  /** Empty decoder state; recreate on session change or disposal. */
  initialInputState(): InputState;
  /** Translate what xterm.js delivers in `onData` (one chunk) into actions: printable text, DEL/Backspace,
   * arrows, Home/End, Delete, Ctrl+A/E/B/F/K/U/W/L/C, Alt+B/F (word moves), Tab, Enter, and bracketed paste
   * (ESC[200~ ... ESC[201~) as a single `paste` action. Unknown escape sequences are DROPPED, never inserted.
   * Enter inside a paste is part of the paste, never a `submit`.
   * Carries partial escape/paste sequences across chunks. Paste buffering is capped at maxLength;
   * oversized pastes are drained through their closing marker, never reinterpreted as keystrokes. */
  decodeInput(data: string, state: InputState, maxLength?: number): DecodedInput;
}
