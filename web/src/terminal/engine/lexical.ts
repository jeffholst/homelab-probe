import type { Token, Tokenized, TokenizeProblem } from './types';

/** Decode without shell evaluation, retaining source ranges for whole-token replacement. */
export function tokenize(text: string, cursor: number): Tokenized {
  const tokens: Token[] = [];
  const prefixes: number[] = [];
  let problem: TokenizeProblem | null = null;
  let i = 0;
  while (i < text.length) {
    if (text[i] === ' ' || text[i] === '\t') { i++; continue; }
    const start = i;
    let value = '', prefix = '', quote = '', quoteAt = i, quoted = false;
    while (i < text.length) {
      const character = String.fromCodePoint(text.codePointAt(i) ?? 0);
      if (!quote && (character === ' ' || character === '\t')) break;
      if (character === '\\' && quote !== "'") {
        quoted = true;
        const at = i++;
        if (i === text.length) { problem = { kind: 'dangling_escape', at }; break; }
        const escaped = String.fromCodePoint(text.codePointAt(i) ?? 0);
        i += escaped.length;
        value += escaped;
        if (i <= cursor) prefix += escaped;
      } else if (character === quote) {
        quote = '';
        i++;
      } else if (!quote && (character === '"' || character === "'")) {
        quote = character;
        quoteAt = i++;
        quoted = true;
      } else {
        i += character.length;
        value += character;
        if (i <= cursor) prefix += character;
      }
    }
    if (quote && !problem) problem = { kind: 'unterminated_quote', at: quoteAt };
    tokens.push({ value, start, end: i, quoted });
    prefixes.push(Array.from(prefix).length);
  }
  let index = tokens.findIndex(t => cursor >= t.start && cursor <= t.end);
  if (index < 0) {
    index = tokens.findIndex(t => t.start > cursor);
    if (index < 0) index = tokens.length;
    tokens.splice(index, 0, { value: '', start: cursor, end: cursor, quoted: false });
    prefixes.splice(index, 0, 0);
  }
  return { tokens, problem, active: { index, prefixCodepoints: prefixes[index] ?? 0 } };
}

export function quoteToken(value: string): string {
  return value && !/[\s'"\\]/u.test(value) ? value : `"${value.replace(/["\\]/g, '\\$&')}"`;
}

/** Same forbidden controls/hidden direction overrides as the terminal request boundary. */
export function forbidden(text: string): boolean {
  // eslint-disable-next-line no-control-regex
  return /[\u0000-\u001f\u007f-\u009f\u200b\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff\u2028\u2029]/u.test(text);
}
