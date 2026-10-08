import stringWidth from 'string-width';
import type { LineState, WrappedLine } from './types';

const graphemes = new Intl.Segmenter('en', { granularity: 'grapheme' });
const words = new Intl.Segmenter('en', { granularity: 'word' });

export function boundaries(text: string): number[] {
  return [...graphemes.segment(text)].map(s => s.index).concat(text.length);
}

export function wordBoundary(line: LineState, forward: boolean): number {
  const segments = [...words.segment(line.text)].filter(s => s.isWordLike);
  if (!forward) return [...segments].reverse().find(s => s.index < line.cursor)?.index ?? 0;
  const next = segments.find(s => s.index + s.segment.length > line.cursor);
  return next ? next.index + next.segment.length : line.text.length;
}

export function wrapLine(line: LineState, columns: number, promptWidth: number): WrappedLine {
  if (!Number.isInteger(columns) || columns < 2 || !Number.isInteger(promptWidth) || promptWidth < 0) {
    throw new RangeError('Wrapping requires at least two columns and a nonnegative prompt width.');
  }
  const rows: string[] = [''];
  let row = 0, column = promptWidth % columns;
  // Rows describe the editable line, not the prompt; account for a prompt that occupies whole rows.
  let cursor = { row: 0, column };
  for (const part of graphemes.segment(line.text)) {
    const width = stringWidth(part.segment);
    if (width > 0 && column + width > columns) { rows.push(''); row++; column = 0; }
    if (line.cursor === part.index) cursor = { row, column };
    rows[row] = (rows[row] ?? '') + part.segment;
    column += width;
    if (column >= columns) { rows.push(''); row++; column = 0; }
    if (line.cursor === part.index + part.segment.length) cursor = { row, column };
  }
  return { rows, cursor };
}
