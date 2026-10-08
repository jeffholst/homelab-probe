import type { TerminalServices } from "./backend";
import { defaultEngine } from "./defaultEngine";
import { createMockBackend } from "./mock";

/** The engine and the mock backend: what a build with VITE_TERMINAL_MOCK=1 shows, for development and the browser tests. */
export function mockServices(): TerminalServices {
  return { engine: defaultEngine, backend: createMockBackend() };
}
