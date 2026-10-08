/**
 * What gets written into xterm, as plain strings. This is the one place that turns state into terminal bytes, and it
 * is pure so the safety property can be tested without a terminal: every character that comes from a report, a
 * command line or a suggestion goes through `cleanLine` first (no escape character survives), and the only escape
 * sequences in the result are the colours and the cursor moves written HERE. The report cannot move the cursor,
 * change the title, overwrite the prompt or draw a status line, because it never reaches the terminal as anything but
 * text on its own lines.
 */
import type { Candidate, EngineState, Notice } from "./engine/types";
import { cleanLine } from "./clean";

export type EntryKind = "command" | "output" | "notice" | "warning" | "error";
export interface Entry {
  readonly id: number;
  readonly kind: EntryKind;
  /** Clean lines (see `cleanLines`); never contain a control character. */
  readonly lines: readonly string[];
}

export interface Palette {
  readonly text: string;
  readonly muted: string;
  readonly accent: string;
  readonly brand: string;
  readonly warning: string;
  readonly danger: string;
  readonly success: string;
}

export const PROMPT = "❯ ";
const ESC = "\x1b";
const RESET = `${ESC}[0m`;

function rgb(hex: string, fallback: string): string {
  const match = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})/i.exec(hex.trim()) ?? /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})/i.exec(fallback);
  const [r, g, b] = [match?.[1], match?.[2], match?.[3]].map((part) => parseInt(part ?? "ff", 16));
  return `${ESC}[38;2;${r ?? 255};${g ?? 255};${b ?? 255}m`;
}

/** Foreground colour sequences for the theme's colours (hex values read from the CSS tokens). */
export function colours(palette: Palette): Record<keyof Palette, string> {
  return {
    text: rgb(palette.text, "#e8eefb"), muted: rgb(palette.muted, "#9fb0cf"), accent: rgb(palette.accent, "#3b9bff"),
    brand: rgb(palette.brand, "#ff7a1a"), warning: rgb(palette.warning, "#ffd27a"), danger: rgb(palette.danger, "#ff9b8f"),
    success: rgb(palette.success, "#7be3a8"),
  };
}

/** One transcript entry as terminal text, each line ending in CRLF. */
export function renderEntry(entry: Entry, palette: Palette): string {
  const c = colours(palette);
  const style: Record<EntryKind, string> = { command: c.accent, output: c.text, notice: c.muted, warning: c.warning, error: c.danger };
  const prefix = entry.kind === "command" ? `${c.brand}${PROMPT}${style.command}` : entry.kind === "output" ? "" : `${style[entry.kind]}`;
  const mark = entry.kind === "warning" ? "! " : entry.kind === "error" ? "✕ " : entry.kind === "notice" ? "· " : "";
  return entry.lines.map((line, index) => `${index === 0 ? prefix + mark : style[entry.kind] + (mark ? "  " : "")}${cleanLine(line)}${RESET}\r\n`).join("");
}

export function noticeText(notice: Notice): string {
  switch (notice.kind) {
    case "paste_refused": return notice.reason === "too_long" ? "Paste refused: too long." : "Paste refused: it contains control characters.";
    case "malformed": return notice.problem.kind === "unterminated_quote" ? "A quote is not closed." : "The line ends with a backslash.";
    case "no_capabilities": return "The terminal is unavailable: its command list could not be loaded.";
    case "busy": return "A command is still running. Wait for it to finish.";
    case "empty": return "Type a command first.";
    case "line_too_long": return "That line is too long.";
  }
}

const MOVE_UP = (rows: number) => (rows > 0 ? `${ESC}[${rows}A` : "");

/** The list of candidates for the active token as one dim line; the selected one is highlighted. Bounded. */
export function suggestionLine(items: readonly Candidate[], selected: number | null, palette: Palette, limit = 8): string {
  if (items.length === 0) return "";
  const c = colours(palette);
  const shown = items.slice(0, limit).map((item, index) => (index === selected ? `${ESC}[7m${cleanLine(item.label)}${ESC}[27m` : cleanLine(item.label)));
  const more = items.length > limit ? `  +${items.length - limit} more` : "";
  return `${c.muted}  ${shown.join("  ")}${more}${RESET}`;
}

/** Moves to the first row of the previous live region and erases it and everything below. */
export function renderErase(rowsUp: number): string {
  return `${MOVE_UP(rowsUp)}\r${ESC}[J`;
}

/**
 * The live region, drawn where the cursor is (the caller erased the old one first): the prompt and the line (or the
 * waiting message while a command runs), the notice and the suggestions below it. It ends with the cursor on the
 * character the engine says the cursor is on.
 */
export function renderLive(state: EngineState, palette: Palette): string {
  const c = colours(palette);
  const hide = `${ESC}[?25l`;
  if (state.running) {
    return `${hide}${c.muted}… waiting for the result (Ctrl+C stops waiting)${RESET}${ESC}[?25h`;
  }
  const { text, cursor } = state.line;
  const before = cleanLine(text.slice(0, cursor));
  const after = cleanLine(text.slice(cursor));
  const below: string[] = [];
  if (state.notice) below.push(`${c.warning}${noticeText(state.notice)}${RESET}`);
  // The list under the prompt is the one Tab opened (a highlighted item); the toolbar shows the same list all the time.
  const suggestions = state.suggestions.selected === null ? "" : suggestionLine(state.suggestions.items, state.suggestions.selected, palette);
  if (suggestions) below.push(suggestions);
  const tail = below.length ? `\r\n${below.join("\r\n")}${MOVE_UP(below.length)}` : "";
  // Save the cursor after the text before it, draw the rest and the lines below, then come back.
  return `${hide}${c.brand}${PROMPT}${c.text}${before}${ESC}7${after}${RESET}${tail}${ESC}8${ESC}[?25h`;
}
