import type { Candidate, CommandMeta, EngineState, OptionMeta } from './types';

/** Uses the same prefix-only context as the API; tokens after the cursor never affect suggestions. */
export function suggestionsFor(state: EngineState): { items: readonly Candidate[]; truncated: boolean } {
  const metadata = state.capabilities;
  if (!metadata || state.pendingPaste) return { items: [], truncated: false };
  const { tokens, active } = state.tokenized;
  const prefix = Array.from(tokens[active.index]?.value ?? '').slice(0, active.prefixCodepoints).join('');
  const commands = metadata.commands.filter(c => c.status !== 'unavailable');
  let options = metadata.globalOptions, command: CommandMeta | undefined, pending: OptionMeta | undefined;
  let positional = false, help = false;
  const used = new Set<OptionMeta>();
  const allowed = (option: OptionMeta) => option.status !== 'unavailable';
  const valid = (option: OptionMeta, value: string) => !option.choices.length
    || (option.commaList ? value.split(',') : [value]).every(v => option.choices.includes(v));
  for (let i = 0; i < active.index; i++) {
    const token = tokens[i]?.value ?? '';
    if (pending) {
      if (!valid(pending, token)) return { items: [], truncated: false };
      pending = undefined; continue;
    }
    if (i === 0 && token === 'hlp') continue;
    if (help) return { items: [], truncated: false };
    if (!command && (token === 'help' || token === '-h' || token === '--help')
      && metadata.staticOperations.includes('help')) { help = true; continue; }
    if (token.startsWith('-')) {
      const equals = token.indexOf('='), flag = equals < 0 ? token : token.slice(0, equals);
      const option = options.find(o => o.flags.includes(flag));
      if (!option || !allowed(option) || (used.has(option) && !option.repeatable)) return { items: [], truncated: false };
      used.add(option);
      if (!option.takesValue) {
        if (equals >= 0 || flag === '-h' || flag === '--help' || flag === '--version') return { items: [], truncated: false };
      } else if (equals >= 0) {
        if (!valid(option, token.slice(equals + 1))) return { items: [], truncated: false };
      } else pending = option;
    } else if (!command) {
      command = commands.find(c => c.name === token);
      if (!command) return { items: [], truncated: false };
      options = command.options; used.clear();
    } else {
      if (positional || (command.choices.length && !command.choices.includes(token))) return { items: [], truncated: false };
      positional = true;
    }
  }
  const candidates: Candidate[] = [];
  function add(label: string, description: string, kind: Candidate['kind']) {
    if (label.startsWith(prefix)) candidates.push({ label, description, kind });
  }
  function values(option: OptionMeta, valuePrefix: string, leader = '') {
    const selected = option.commaList ? valuePrefix.split(',').slice(0, -1) : [];
    if (selected.some(v => !option.choices.includes(v))) return;
    const head = leader + (selected.length ? selected.join(',') + ',' : '');
    for (const value of option.choices) if (!selected.includes(value)) add(head + value, option.description, 'choice');
  }
  if (pending) values(pending, prefix);
  else if (prefix.startsWith('-') && prefix.includes('=')) {
    const equals = prefix.indexOf('='), flag = prefix.slice(0, equals);
    const option = options.find(o => o.flags.includes(flag));
    if (option && allowed(option) && option.takesValue && (!used.has(option) || option.repeatable)) {
      values(option, prefix.slice(equals + 1), flag + '=');
    }
  } else {
    if (!command) {
      for (const item of commands) add(item.name, item.description, 'command');
      if (!help) for (const operation of metadata.staticOperations) add(operation, operation, 'command');
    } else if (!positional) for (const value of command.choices) add(value, command.description, 'choice');
    if (!help) for (const option of options) {
      if (allowed(option) && (!used.has(option) || option.repeatable)) {
        for (const flag of option.flags) add(flag, option.description, 'option');
      }
    }
  }
  const unique = [...new Map(candidates.map(c => [c.label, c])).values()]
    .sort((a, b) => a.label < b.label ? -1 : a.label > b.label ? 1 : 0);
  const limit = Math.min(state.options.suggestionLimit, metadata.limits.completionCandidates);
  return { items: unique.slice(0, limit), truncated: unique.length > limit };
}
