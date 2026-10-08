import { createContext, useContext } from "react";

import type { TerminalServices } from "./backend";

/** Provided by main.tsx when the terminal exists; null (the default) hides the button and the panel everywhere. */
export const TerminalContext = createContext<TerminalServices | null>(null);

export function useTerminalServices(): TerminalServices | null {
  return useContext(TerminalContext);
}
