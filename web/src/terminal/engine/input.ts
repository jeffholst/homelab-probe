/* The protocol's control bytes are intentionally matched, never rendered or forwarded as commands. */
/* eslint-disable no-control-regex */
import type { DecodedInput, EngineAction, InputState } from './types';

const OPEN = '\u001b[200~', CLOSE = '\u001b[201~';
const keys: Readonly<Record<string, EngineAction['type']>> = {
  '\r': 'submit', '\n': 'submit', '\t': 'tab', '\u007f': 'backspace', '\b': 'backspace',
  '\u0001': 'home', '\u0005': 'end', '\u0002': 'left', '\u0006': 'right',
  '\u000b': 'deleteToEnd', '\u0015': 'deleteToStart', '\u0017': 'deleteWordBack',
  '\u000c': 'clearOutput', '\u0003': 'interrupt',
  '\u001b[A': 'historyPrevious', '\u001b[B': 'historyNext', '\u001b[C': 'right', '\u001b[D': 'left',
  '\u001b[H': 'home', '\u001b[F': 'end', '\u001bOH': 'home', '\u001bOF': 'end',
  '\u001b[1~': 'home', '\u001b[4~': 'end', '\u001b[3~': 'delete',
  '\u001b[1;5D': 'wordLeft', '\u001b[1;5C': 'wordRight', '\u001bb': 'wordLeft', '\u001bf': 'wordRight',
};

function keyAction(type: EngineAction['type']): EngineAction | null {
  switch (type) {
    case 'submit': case 'tab': case 'backspace': case 'home': case 'end': case 'left': case 'right':
    case 'deleteToEnd': case 'deleteToStart': case 'deleteWordBack': case 'clearOutput': case 'interrupt':
    case 'historyPrevious': case 'historyNext': case 'delete': case 'wordLeft': case 'wordRight':
      return { type };
    default: return null;
  }
}

export function initialInputState(): InputState { return { buffer: '', paste: null, overflow: false, discardingCsi: false }; }

/** Transport chunks are not protocol frames: retain partial markers, drain rejected paste/OSC/DCS. */
export function decodeInput(data: string, state: InputState, maxLength = 4096): DecodedInput {
  if (!Number.isInteger(maxLength) || maxLength < 1) throw new RangeError('Invalid input buffer limit.');
  let input = state.buffer + data, buffer = '', paste = state.paste, overflow = state.overflow;
  let discardingCsi = state.discardingCsi;
  const actions: EngineAction[] = [];
  while (input) {
    if (discardingCsi) {
      const final = /[@-~]/u.exec(input);
      if (!final) break;
      input = input.slice(final.index + 1); discardingCsi = false;
      continue;
    }
    if (paste !== null) {
      const end = input.indexOf(CLOSE);
      let keep = 0;
      if (end < 0) {
        for (let n = 1; n < CLOSE.length; n++) if (input.endsWith(CLOSE.slice(0, n))) keep = n;
      }
      const content = end >= 0 ? input.slice(0, end) : input.slice(0, input.length - keep);
      if (!overflow) {
        if (paste.length + content.length > maxLength) { paste = ''; overflow = true; }
        else paste += content;
      }
      if (end < 0) { buffer = keep ? input.slice(-keep) : ''; break; }
      actions.push(overflow ? { type: 'inputRejected', reason: 'too_long' } : { type: 'paste', text: paste });
      paste = null; overflow = false; input = input.slice(end + CLOSE.length);
      continue;
    }
    if (input.startsWith(OPEN)) { paste = ''; input = input.slice(OPEN.length); continue; }
    if (input.startsWith('\u001b]') || input.startsWith('\u001bP')) {
      const match = /\u0007|\u001b\\/u.exec(input.slice(2));
      if (!match) {
        buffer = input.slice(0, 2) + (input.endsWith('\u001b') ? '\u001b' : '');
        break;
      }
      input = input.slice(2 + match.index + match[0].length);
      continue;
    }
    if (input.startsWith('\u001b')) {
      const sequence = /^\u001b(?:\[[0-?]*[ -/]*[@-~]|O[@-~]|[^\x5bO\x5dP])/u.exec(input)?.[0];
      if (!sequence) {
        // Retain only incomplete CSI/SS3 sequences, with a fixed cap. Their parameters contain no commands.
        if (/^\u001b(?:\[[0-?]*[ -/]*|O)?$/u.test(input)) {
          if (input.length <= 64) buffer = input;
          else discardingCsi = true;
          break;
        }
        input = input.slice(1); continue;
      }
      const action = keyAction(keys[sequence] ?? 'reset');
      if (action) actions.push(action);
      input = input.slice(sequence.length);
      continue;
    }
    if (input.length === 1 && /[\ud800-\udbff]/u.test(input)) { buffer = input; break; }
    const character = String.fromCodePoint(input.codePointAt(0) ?? 0);
    const type = keys[character];
    if (type) {
      const action = keyAction(type);
      if (action) actions.push(action);
    } else if (character >= ' ' && character !== '\u007f') {
      const last = actions.at(-1);
      if (last?.type === 'insert') actions[actions.length - 1] = { type: 'insert', text: last.text + character };
      else actions.push({ type: 'insert', text: character });
    }
    input = input.slice(character.length);
  }
  return { state: { buffer, paste, overflow, discardingCsi }, actions };
}
