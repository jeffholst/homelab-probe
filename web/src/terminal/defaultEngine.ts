import type { EngineApi } from "./engine/types";
import { miniEngine } from "./testing/miniEngine";

/** The engine the panel uses. Replace this with the real engine's export when #275 lands (one line). */
export const defaultEngine: EngineApi = miniEngine;
