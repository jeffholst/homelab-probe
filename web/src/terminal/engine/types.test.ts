// @vitest-environment node
import { expectTypeOf, it } from 'vitest';
import type { DecodedInput, Effect, EngineAction, EngineApi, EngineState, InputState } from './types';

it('requires execution completion to echo the active execution identity', () => {
  expectTypeOf<Extract<EngineAction, { type: 'executionFinished' }>>()
    .toEqualTypeOf<{ readonly type: 'executionFinished'; readonly seq: number }>();
  expectTypeOf<Extract<Effect, { type: 'execute' }>['seq']>().toEqualTypeOf<number>();
  expectTypeOf<EngineState['activeExecution']>().toEqualTypeOf<number | null>();
  expectTypeOf<EngineState['executionSeq']>().toEqualTypeOf<number>();
});

it('requires a non-empty completion argv while allowing the empty-line placeholder', () => {
  type CompletionArgv = Extract<Effect, { type: 'complete' }>['argv'];
  expectTypeOf<[]>().not.toExtend<CompletionArgv>();
  expectTypeOf<['']>().toExtend<CompletionArgv>();
  expectTypeOf<['query', 'cl']>().toExtend<CompletionArgv>();
});

it('makes completion request identity and context explicit', () => {
  expectTypeOf<EngineState['completionSeq']>().toEqualTypeOf<number>();
  expectTypeOf<EngineState['completionPending']>()
    .toEqualTypeOf<{ readonly seq: number; readonly revision: number } | null>();
});

it('requires caller-owned streaming decoder state on every input chunk', () => {
  expectTypeOf<EngineApi['initialInputState']>().returns.toEqualTypeOf<InputState>();
  type InputParameters = Parameters<EngineApi['decodeInput']>;
  expectTypeOf<InputParameters['length']>().toEqualTypeOf<2 | 3>();
  expectTypeOf<InputParameters[0]>().toEqualTypeOf<string>();
  expectTypeOf<InputParameters[1]>().toEqualTypeOf<InputState>();
  expectTypeOf<InputParameters[2]>().toEqualTypeOf<number | undefined>();
  expectTypeOf<ReturnType<EngineApi['decodeInput']>>().toEqualTypeOf<DecodedInput>();
  expectTypeOf<DecodedInput['state']>().toEqualTypeOf<InputState>();
  expectTypeOf<DecodedInput['actions']>().toEqualTypeOf<readonly EngineAction[]>();
});
