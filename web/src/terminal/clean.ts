/**
 * Text that may be written into the terminal. Report text comes from a network (client names, SSIDs, event text) and
 * from a server we only partly control, so it is untrusted: before xterm sees a character here, every control
 * character is gone. The escape character is the one that matters (it starts every cursor movement, colour, title,
 * link and clipboard sequence); removing it leaves any `[31m` or `]0;title` as plain, inert text. The same invisible
 * and direction-changing characters as `safeText` (a port of `printable` in homelab_probe/util.py) are removed too.
 * Everything that styles the terminal is written by this module's callers, never taken from the text.
 */

const LINE_BREAKS = new RegExp("\\r\\n|\\n|\\r|\\u0085|\\u2028|\\u2029", "g");
// The control characters are the point of this expression.
// Line breaks are controls here too: `cleanLines` has already split on them, and a carriage return left in a line would
// send the cursor back to column 0 and overwrite what is on it. The control characters are the point of this expression.
// eslint-disable-next-line no-control-regex
const CONTROLS = new RegExp("[\\u0000-\\u0008\\u000a-\\u001f\\u007f-\\u009f\\u2028\\u2029]", "g");
const HIDDEN = new RegExp("[\\u200b\\u202a-\\u202e\\u2060-\\u2064\\u2066-\\u2069\\ufeff]", "g");
const TABS = new RegExp("\\t", "g");

/** One line of text with nothing in it that the terminal could act on. Spaces are kept (tables are aligned with them). */
export function cleanLine(value: string): string {
  return value.replace(TABS, "    ").replace(CONTROLS, "").replace(HIDDEN, "");
}

/** A block of text as clean lines. Every kind of line break splits; nothing else can start a new line. */
export function cleanLines(value: string): string[] {
  return value.split(LINE_BREAKS).map(cleanLine);
}
