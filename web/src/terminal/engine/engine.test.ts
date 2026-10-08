// @vitest-environment node
import { describe, expect, it } from 'vitest';
import fixture from '../../test/fixtures/terminal.v1.json';
import { engine } from './index';
import type { Candidate, Capabilities, EngineAction, EngineState, OptionMeta } from './types';

const capabilities = fixture.capabilities as Capabilities;
const apply = (state: EngineState, action: EngineAction) => engine.step(state, action).state;
const finish = (state: EngineState) => apply(state, { type: 'executionFinished', seq: state.executionSeq });
function ready(text = '') {
  let state = apply(engine.initialState(), { type: 'setCapabilities', capabilities });
  state = apply(state, { type: 'insert', text });
  return state;
}

describe('terminal engine contract', () => {
  it('keeps the draft and walks graphemes without splitting emoji or combining marks', () => {
    let state = ready('A\u{1f469}\u200d\u{1f4bb}e\u0301\u754c');
    const before = structuredClone(state);
    state = apply(state, { type: 'left' });
    expect(state.line.cursor).toBe(before.line.text.length - 1);
    state = apply(state, { type: 'backspace' });
    expect(state.line.text).toBe('A\u{1f469}\u200d\u{1f4bb}\u754c');
    state = apply(state, { type: 'backspace' });
    expect(state.line.text).toBe('A\u754c');
    expect(before.line.text).toBe('A\u{1f469}\u200d\u{1f4bb}e\u0301\u754c');
  });

  it('tokenizes quoted literals without evaluating shell syntax', () => {
    const text = 'hlp client "a b" \'c d\' x\\ y $(id);|>~*';
    expect(engine.tokenize(text, text.length).tokens.map(t => t.value))
      .toEqual(['hlp', 'client', 'a b', 'c d', 'x y', '$(id);|>~*']);
    expect(engine.tokenize('client "bad', 11).problem?.kind).toBe('unterminated_quote');
    expect(engine.tokenize('client bad\\', 11).problem?.kind).toBe('dangling_escape');
    expect(engine.canSubmit(ready('query "clients'))).toEqual({ allowed: false, reason: 'malformed' });
  });

  it.each(['', 'a b', 'a\'b"c\\d', '$(id);|&>~*', '\u{1f469}\u200d\u{1f4bb}'])('round trips token %j', value => {
    const quoted = engine.quoteToken(value);
    const parsed = engine.tokenize(quoted, quoted.length);
    expect(parsed.tokens.map(t => t.value)).toEqual([value]);
    expect(parsed.problem).toBeNull();
  });

  it('emits one execute effect, strips only the optional hlp prefix and keeps running on interrupt', () => {
    const result = engine.step(ready('hlp query clients'), { type: 'submit' });
    expect(result.effects).toEqual([{ type: 'execute', seq: 1, argv: ['query', 'clients'], line: 'hlp query clients' }]);
    expect(engine.step(result.state, { type: 'submit' }).effects).toEqual([]);
    const stopped = engine.step(result.state, { type: 'interrupt' });
    expect(stopped.effects).toEqual([{ type: 'interrupted', running: true }]);
    expect(stopped.state.running).toBe(true);
    expect(engine.canSubmit(finish(result.state)).allowed).toBe(false);
  });

  it('restores history and the unfinished draft without executing', () => {
    let state = engine.step(ready('query clients'), { type: 'submit' }).state;
    state = finish(state);
    state = apply(state, { type: 'insert', text: 'unfinished' });
    const result = engine.step(state, { type: 'historyPrevious' });
    expect(result.effects).toEqual([]);
    expect(result.state.line.text).toBe('query clients');
    expect(apply(result.state, { type: 'historyNext' }).line.text).toBe('unfinished');
  });

  it('paste waits for confirmation and never emits execution', () => {
    const pasted = engine.step(ready(), { type: 'paste', text: 'query\r\nclients' });
    expect(pasted.effects).toEqual([]);
    expect(pasted.state.line.text).toBe('');
    expect(engine.canSubmit(pasted.state)).toEqual({ allowed: false, reason: 'pending_paste' });
    const confirmed = engine.step(pasted.state, { type: 'confirmPaste' });
    expect(confirmed.effects).toEqual([]);
    expect(confirmed.state.line.text).toBe('query clients');
    expect(apply(ready(), { type: 'paste', text: 'query\u202e' }).notice?.kind).toBe('paste_refused');
  });

  it('uses only capability metadata, omits unavailable and already used options', () => {
    expect(engine.suggestionsFor(engine.initialState()).items).toEqual([]);
    expect(engine.canSubmit(engine.initialState())).toEqual({ allowed: false, reason: 'no_capabilities' });
    expect(engine.suggestionsFor(ready('query cl')).items.map(c => c.label)).toEqual(['clients']);
    expect(engine.suggestionsFor(ready('query clients --json ')).items.map(c => c.label)).not.toContain('--json');
    const chosen = engine.step(ready('query cl'), { type: 'tab' });
    expect(chosen.state.line.text).toBe('query clients ');
    expect(chosen.effects).toEqual([]);
  });

  it('drops completion responses after cursor movement, reset or a later request', () => {
    const asked = engine.step(ready('query '), { type: 'tab' });
    const effect = asked.effects[0];
    if (effect?.type !== 'complete') throw new Error('expected completion request');
    const arrival: EngineAction = { type: 'suggestionsArrived', seq: effect.seq, revision: effect.revision,
      candidates: fixture.completionResponse.candidates as Candidate[], truncated: false };
    const moved = apply(asked.state, { type: 'left' });
    expect(apply(moved, arrival)).toBe(moved);
    const reset = apply(asked.state, { type: 'reset' });
    expect(apply(reset, arrival)).toBe(reset);
    expect(reset.capabilities).toBeNull();
    expect(reset.history.entries).toEqual([]);
    expect(reset.line.text).toBe('');
    const later = engine.step(asked.state, { type: 'tab' }).state;
    expect(apply(later, arrival)).toBe(later);
  });

  it('preserves paste safety at every possible stream split', () => {
    const input = '\u001b[200~query\nclients\u001b[201~';
    for (let split = 0; split <= input.length; split++) {
      const first = engine.decodeInput(input.slice(0, split), engine.initialInputState());
      const second = engine.decodeInput(input.slice(split), first.state);
      expect([...first.actions, ...second.actions]).toEqual([{ type: 'paste', text: 'query\nclients' }]);
    }
  });

  it('drains oversized paste without treating its newline as submit', () => {
    const first = engine.decodeInput('\u001b[200~123456', engine.initialInputState(), 4);
    const second = engine.decodeInput('\nquery\u001b[201~', first.state, 4);
    expect([...first.actions, ...second.actions]).toEqual([{ type: 'inputRejected', reason: 'too_long' }]);
    expect(second.state).toEqual(engine.initialInputState());
  });

  it('wraps by grapheme cell width and maps cursor positions after the prompt', () => {
    expect(engine.wrapLine({ text: 'a\u754ce\u0301', cursor: 4 }, 4, 1)).toEqual({
      rows: ['a\u754c', 'e\u0301'], cursor: { row: 1, column: 1 },
    });
    expect(engine.wrapLine({ text: '\u{1f469}\u200d\u{1f4bb}x', cursor: 5 }, 3, 0)).toEqual({
      rows: ['\u{1f469}\u200d\u{1f4bb}x', ''], cursor: { row: 0, column: 2 },
    });
  });

  it.each(['\u0000', '\u001b', '\u007f', '\u009b', '\u200b', '\u202e', '\u2066', '\ufeff'])('refuses control/hidden input %j without changing the line', character => {
    const state = apply(ready('query'), { type: 'insert', text: character });
    expect(state.line.text).toBe('query');
    expect(state.notice).toEqual({ kind: 'paste_refused', reason: 'control_characters' });
  });

  it.each([
    ['home', 0], ['end', 10], ['wordLeft', 6], ['left', 9],
  ] as const)('moves %s with immutable input', (type, cursor) => {
    const state = ready('alpha beta');
    Object.freeze(state.line);
    Object.freeze(state);
    expect(apply(state, { type }).line.cursor).toBe(cursor);
    expect(state.line.cursor).toBe(10);
  });

  it('handles forward word/grapheme movement and deletion at either edge', () => {
    let state = apply(ready('alpha beta'), { type: 'home' });
    expect(apply(state, { type: 'backspace' })).toBe(state);
    expect(apply(state, { type: 'left' })).toBe(state);
    state = apply(state, { type: 'wordRight' });
    expect(state.line.cursor).toBe(5);
    expect(apply(state, { type: 'right' }).line.cursor).toBe(6);
    expect(apply(state, { type: 'deleteWordForward' }).line.text).toBe('alpha');
    expect(apply(state, { type: 'deleteToStart' }).line.text).toBe(' beta');
    expect(apply(state, { type: 'deleteToEnd' }).line.text).toBe('alpha');
    expect(apply(state, { type: 'deleteWordBack' }).line.text).toBe(' beta');
    expect(apply(state, { type: 'delete' }).line.text).toBe('alphabeta');
    state = apply(state, { type: 'end' });
    expect(apply(state, { type: 'delete' })).toBe(state);
    expect(apply(state, { type: 'right' })).toBe(state);
    expect(apply(state, { type: 'clearLine' }).line).toEqual({ text: '', cursor: 0 });
  });

  it('snaps cursor to a boundary when insertion/deletion merges grapheme clusters', () => {
    let state = ready('\u0301');
    state = apply(state, { type: 'home' });
    state = apply(state, { type: 'insert', text: 'e' });
    expect(state.line).toEqual({ text: 'e\u0301', cursor: 2 });
    expect(apply(state, { type: 'left' }).line.cursor).toBe(0);
  });

  it('caps history, ignores adjacent duplicates, supports zero retention and clears it', () => {
    let state = engine.initialState({ historyLimit: 2 });
    state = apply(state, { type: 'setCapabilities', capabilities });
    for (const text of ['query clients', 'query clients', 'query devices', 'query ports']) {
      state = apply(state, { type: 'insert', text });
      state = apply(state, { type: 'submit' });
      state = finish(state);
    }
    expect(state.history.entries).toEqual(['query devices', 'query ports']);
    expect(apply(state, { type: 'historyNext' })).toBe(state);
    state = apply(state, { type: 'historyPrevious' });
    state = apply(state, { type: 'historyPrevious' });
    expect(apply(state, { type: 'historyPrevious' }).history.position).toBe(0);
    state = apply(state, { type: 'insert', text: ' --json' });
    expect(state.history.position).toBeNull();
    expect(apply(state, { type: 'clearHistory' }).history.entries).toEqual([]);
    let none = engine.initialState({ historyLimit: 0 });
    none = apply(none, { type: 'setCapabilities', capabilities });
    none = apply(none, { type: 'insert', text: 'query clients' });
    expect(apply(none, { type: 'submit' }).history.entries).toEqual([]);
  });

  it('enforces line, codepoint token, token count and serialized body limits', () => {
    const tiny = apply(engine.initialState({ maxLineLength: 4 }), { type: 'insert', text: '12345' });
    expect(tiny.line.text).toBe('');
    expect(tiny.notice?.kind).toBe('line_too_long');
    for (const [limits, text] of [
      [{ tokenCount: 2 }, 'query clients extra'],
      [{ tokenChars: 5 }, 'query clients'],
      [{ bodyBytes: 80 }, 'query clients'],
    ] as const) {
      const caps = structuredClone(capabilities);
      Object.assign(caps.limits, limits);
      const state = apply(apply(engine.initialState(), { type: 'setCapabilities', capabilities: caps }), { type: 'insert', text });
      expect(state.line.text).toBe('');
    }
    const unicode = structuredClone(capabilities);
    unicode.limits.tokenChars = 1;
    let state = apply(engine.initialState(), { type: 'setCapabilities', capabilities: unicode });
    state = apply(state, { type: 'insert', text: '\u{1f600}' });
    expect(state.line.text).toBe('\u{1f600}');
  });

  it('does not edit or select from history while running; idle interrupt clears only the line', () => {
    const running = apply(ready('query clients'), { type: 'submit' });
    for (const action of [{ type: 'insert', text: 'x' }, { type: 'paste', text: 'x' },
      { type: 'historyPrevious' }, { type: 'left' }, { type: 'tab' }] satisfies EngineAction[]) {
      expect(apply(running, action)).toBe(running);
    }
    const idle = finish(running);
    expect(finish(idle)).toBe(idle);
    const state = apply(idle, { type: 'insert', text: 'draft' });
    const result = engine.step(state, { type: 'interrupt' });
    expect(result.effects).toEqual([{ type: 'interrupted', running: false }]);
    expect(result.state.history.entries).toEqual(['query clients']);
    expect(result.state.line.text).toBe('');
  });

  it('keeps malformed input untouched and retains empty quoted arguments', () => {
    const state = ready('client "');
    expect(engine.step(state, { type: 'submit' }).state.notice?.kind).toBe('malformed');
    expect(engine.step(state, { type: 'submit' }).state.line.text).toBe('client "');
    expect(engine.canSubmit(ready('hlp'))).toEqual({ allowed: false, reason: 'empty' });
    expect(engine.canSubmit(ready('client ""'))).toEqual({ allowed: true, argv: ['client', ''] });
    expect(engine.tokenize('query "cl junk"', 9).active.prefixCodepoints).toBe(2);
    expect(engine.tokenize('query clients', 0).active.index).toBe(0);
    expect(engine.tokenize('query  clients', 6).tokens.map(t => t.value)).toEqual(['query', '', 'clients']);
  });

  it('normalizes paste whitespace, cancels previews, and checks the resulting full line', () => {
    expect(apply(ready(), { type: 'paste', text: 'query\tclients' }).line.text).toBe('query clients');
    const pending = apply(ready('query'), { type: 'paste', text: '\nclients' });
    expect(apply(pending, { type: 'insert', text: 'ignored' })).toBe(pending);
    expect(apply(pending, { type: 'left' })).toBe(pending);
    const canceled = apply(pending, { type: 'cancelPaste' });
    expect(canceled.pendingPaste).toBeNull();
    expect(canceled.line.text).toBe('query');
    expect(apply(canceled, { type: 'confirmPaste' })).toBe(canceled);
    const small = apply(engine.initialState({ maxLineLength: 6 }), { type: 'insert', text: 'query' });
    expect(apply(small, { type: 'paste', text: ' xx' }).notice).toEqual({ kind: 'paste_refused', reason: 'too_long' });
  });

  it('handles aliases, repeatable and comma-list choices from metadata without a command table', () => {
    const caps = structuredClone(capabilities);
    const option = (flags: string[], choices: string[], extra: Partial<OptionMeta> = {}): OptionMeta => ({
      flags, choices, takesValue: true, repeatable: false, commaList: false, status: 'supported',
      description: 'Synthetic option', reason: 'test', ...extra,
    });
    caps.commands[0]!.options.push(option(['-b', '--band'], ['2g', '5g', '6g'], { commaList: true }),
      option(['--area'], ['clients', 'devices'], { repeatable: true }),
      option(['--secret'], [], { status: 'unavailable' }));
    caps.commands.push({ name: 'hidden', description: '', choices: [], options: [], status: 'unavailable', reason: '' });
    const labels = (text: string) => {
      let state = apply(engine.initialState(), { type: 'setCapabilities', capabilities: caps });
      state = apply(state, { type: 'insert', text });
      return engine.suggestionsFor(state).items.map(c => c.label);
    };
    expect(labels('query --band ')).toEqual(['2g', '5g', '6g']);
    expect(labels('query --band 2g,')).toEqual(['2g,5g', '2g,6g']);
    expect(labels('query --band=2g,')).toEqual(['--band=2g,5g', '--band=2g,6g']);
    expect(labels('query -b 5g ')).not.toContain('--band');
    expect(labels('query --area clients ')).toContain('--area');
    expect(labels('query --band bogus ')).toEqual([]);
    expect(labels('query --band=bogus ')).toEqual([]);
    expect(labels('query --band=2g --band ')).toEqual([]);
    expect(labels('query --json=x ')).toEqual([]);
    expect(labels('query --unknown ')).toEqual([]);
    expect(labels('query clients devices ')).toEqual([]);
    expect(labels('query bad ')).toEqual([]);
    expect(labels('')).not.toContain('hidden');
    expect(labels('query ')).not.toContain('--secret');
    expect(labels('help ')).toEqual(['query']);
    expect(labels('help query ')).toEqual([]);
    expect(labels('hlp --site test query cl')).toEqual(['clients']);
  });

  it('replaces the whole active token, including a suffix, and quotes metadata literals', () => {
    let state = ready('query clJUNK');
    for (let i = 0; i < 4; i++) state = apply(state, { type: 'left' });
    state = apply(state, { type: 'tab' });
    expect(state.line.text).toBe('query clients ');
    const caps = structuredClone(capabilities);
    caps.commands[0]!.choices = ['a b'];
    state = apply(ready('query a'), { type: 'setCapabilities', capabilities: caps });
    expect(apply(state, { type: 'tab' }).line.text).toBe('query "a b" ');
  });

  it('bounds suggestions, navigates selection, and ignores remote grammar not in capabilities', () => {
    let state = apply(engine.initialState({ suggestionLimit: 2 }), { type: 'setCapabilities', capabilities });
    state = apply(state, { type: 'insert', text: 'query ' });
    expect(state.suggestions.items).toHaveLength(2);
    expect(state.suggestions.truncated).toBe(true);
    state = apply(state, { type: 'suggestPrevious' });
    expect(state.suggestions.selected).toBe(1);
    state = apply(state, { type: 'suggestNext' });
    expect(state.suggestions.selected).toBe(0);
    expect(engine.step(state, { type: 'tab' }).effects).toEqual([]);
    state = apply(state, { type: 'dismissSuggestions' });
    expect(state.suggestions.items).toEqual([]);
    expect(apply(state, { type: 'selectSuggestion', index: 100 })).toBe(state);
    const asked = engine.step(ready('query '), { type: 'tab' });
    const pending = asked.state.completionPending!;
    const arrived = apply(asked.state, { type: 'suggestionsArrived', ...pending, truncated: false,
      candidates: [{ label: 'evil', kind: 'command', description: 'untrusted' },
        { label: 'clients', kind: 'command', description: 'untrusted' }] });
    expect(arrived.suggestions.items).toEqual([fixture.completionResponse.candidates[0]]);
    const moved = apply(asked.state, { type: 'setCapabilities', capabilities: null });
    expect(apply(moved, { type: 'suggestionsArrived', ...pending, candidates: [], truncated: false })).toBe(moved);
  });

  it.each([
    ['\r', 'submit'], ['\t', 'tab'], ['\u007f', 'backspace'], ['\u001b[A', 'historyPrevious'],
    ['\u001b[B', 'historyNext'], ['\u001b[C', 'right'], ['\u001b[D', 'left'],
    ['\u001b[H', 'home'], ['\u001b[F', 'end'], ['\u001b[3~', 'delete'],
    ['\u0001', 'home'], ['\u0005', 'end'], ['\u0002', 'left'], ['\u0006', 'right'],
    ['\u000b', 'deleteToEnd'], ['\u0015', 'deleteToStart'], ['\u0017', 'deleteWordBack'],
    ['\u000c', 'clearOutput'], ['\u0003', 'interrupt'], ['\u001bb', 'wordLeft'], ['\u001bf', 'wordRight'],
  ] as const)('decodes key %j', (data, type) => {
    expect(engine.decodeInput(data, engine.initialInputState()).actions).toEqual([{ type }]);
  });

  it('ignores terminal responses/OSC/DCS and unknown escape sequences across all splits', () => {
    for (const input of ['\u001b[12;5R', '\u001b]52;c;query\nclients\u0007', '\u001bPquery\nclients\u001b\\']) {
      for (let split = 0; split <= input.length; split++) {
        const a = engine.decodeInput(input.slice(0, split), engine.initialInputState());
        const b = engine.decodeInput(input.slice(split), a.state);
        expect([...a.actions, ...b.actions]).toEqual([]);
      }
    }
    let state = engine.decodeInput('\u001b[' + '1'.repeat(100), engine.initialInputState()).state;
    expect(state.buffer.length).toBeLessThanOrEqual(64);
    const end = engine.decodeInput('2R', state);
    expect(end.actions).toEqual([]);
    state = engine.decodeInput('\ud83d', engine.initialInputState()).state;
    expect(engine.decodeInput('\ude00', state).actions).toEqual([{ type: 'insert', text: '\u{1f600}' }]);
  });

  it('rejects invalid engine/wrapping/decoder limits and counts wide/zero-width clusters', () => {
    expect(() => engine.initialState({ historyLimit: -1 })).toThrow(RangeError);
    expect(() => engine.initialState({ suggestionLimit: 0 })).toThrow(RangeError);
    expect(() => engine.decodeInput('', engine.initialInputState(), 0)).toThrow(RangeError);
    expect(() => engine.wrapLine({ text: '', cursor: 0 }, 1, 0)).toThrow(RangeError);
    expect(engine.wrapLine({ text: '\u754c\u754c', cursor: 2 }, 3, 0)).toEqual({
      rows: ['\u754c', '\u754c'], cursor: { row: 1, column: 2 },
    });
    expect(engine.wrapLine({ text: '\u0301a', cursor: 2 }, 3, 0).cursor.column).toBe(1);
  });

  it('Ctrl+L requests transcript clearing without discarding the draft or releasing execution', () => {
    const state = ready('unfinished');
    expect(engine.step(state, { type: 'clearOutput' })).toEqual({ state, effects: [{ type: 'clearOutput' }] });
    const running = apply(ready('query clients'), { type: 'submit' });
    expect(engine.step(running, { type: 'clearOutput' }).state).toBe(running);
    expect(running.running).toBe(true);
  });

  it('ignores old-session and duplicate execution completions while a newer request is running', () => {
    const previous = apply(ready('query clients'), { type: 'submit' });
    const oldSeq = previous.activeExecution!;
    let state = apply(previous, { type: 'reset' });
    state = apply(state, { type: 'setCapabilities', capabilities });
    state = apply(state, { type: 'insert', text: 'query devices' });
    state = apply(state, { type: 'submit' });
    const currentSeq = state.activeExecution!;
    expect(apply(state, { type: 'executionFinished', seq: oldSeq })).toBe(state);
    expect(state.running).toBe(true);
    expect(currentSeq).toBeGreaterThan(oldSeq);
    expect(engine.step(state, { type: 'submit' }).effects).toEqual([]);
    state = apply(state, { type: 'executionFinished', seq: currentSeq });
    expect(state.running).toBe(false);
    expect(state.activeExecution).toBeNull();
    expect(apply(state, { type: 'executionFinished', seq: currentSeq })).toBe(state);
    state = apply(state, { type: 'insert', text: 'query ports' });
    state = apply(state, { type: 'submit' });
    expect(apply(state, { type: 'executionFinished', seq: currentSeq })).toBe(state);
    expect(state.running).toBe(true);
  });

  it('emits a non-empty completion argv for an empty line and after trailing whitespace', () => {
    for (const text of ['', 'query ']) {
      const result = engine.step(ready(text), { type: 'tab' });
      const effect = result.effects[0];
      if (effect?.type !== 'complete') throw new Error('Expected completion effect');
      expect(effect.argv.length).toBeGreaterThan(0);
      expect(effect.argv.at(-1)).toBe('');
      expect(effect.tokenIndex).toBe(effect.argv.length - 1);
      expect(effect.cursor).toBe(0);
    }
  });
});
