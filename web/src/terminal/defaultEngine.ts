import { engine } from "./engine";
import type { EngineApi } from "./engine/types";

/** The engine the panel uses: the real command-line engine (#275). `testing/miniEngine.ts` is a stand-in for tests only. */
export const defaultEngine: EngineApi = engine;
