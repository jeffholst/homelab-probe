/**
 * Text that is safe to show, a port of `printable` in homelab_probe/util.py.
 *
 * Names on a network (client hostnames, device names, SSIDs, event text, notes) are chosen by whoever owns the
 * device, so they are untrusted. React already escapes markup, so what is left to defend against is text that
 * *looks* different from what it is: a bidirectional override that reorders the characters around it, a zero-width
 * character that makes two different names look the same, a control character, or a line break that forges a second
 * line. `safeText` turns one value into one line without any of them.
 *
 * The same rules as the Python function, on purpose (`printable-vectors.json` holds inputs and the answers the Python
 * function gives, and a test on each side checks them):
 *
 * - tabs and line breaks (including NEL, LS and PS) become one space;
 * - C0 controls, DEL and C1 controls are removed;
 * - bidirectional embeddings, overrides and isolates, the zero-width space, the word joiner, invisible math operators
 *   and the byte-order mark are removed;
 * - kept: the zero-width joiner and non-joiner (emoji and Persian need them), the weak direction marks LRM, RLM and
 *   ALM, and the letters of right-to-left scripts;
 * - the result is trimmed, and `limit` (when above zero) cuts it, counted in characters rather than UTF-16 units,
 *   with an ellipsis.
 *
 * The one difference: Python's `str(value)` has an answer for everything, while a JavaScript object has none worth
 * showing, so only strings, numbers, booleans and bigints are text (`true` is "true", not "True"); `null`,
 * `undefined` and everything else give "".
 */

const LINE_BREAKS = /[\t\r\n\u0085  ]+/g;
// The control characters are the point of this expression.
// eslint-disable-next-line no-control-regex
const CONTROLS =/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f]/g;
const HIDDEN = /[​‪-‮⁠-⁤⁦-⁩﻿]/g;

export function safeText(value: unknown, limit = 0): string {
  let text: string;
  switch (typeof value) {
    case "string":
      text = value;
      break;
    case "number":
    case "boolean":
    case "bigint":
      text = String(value);
      break;
    default:
      return "";
  }
  text = text.replace(LINE_BREAKS, " ").replace(CONTROLS, "").replace(HIDDEN, "").trim();
  if (limit > 0) {
    const characters = Array.from(text);
    if (characters.length > limit) text = characters.slice(0, limit - 1).join("") + "…";
  }
  return text;
}
