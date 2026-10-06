import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";

import { applyThemePreference, readThemePreference, writeThemePreference, type ThemePreference } from "./storage";

interface ThemeContextValue {
  preference: ThemePreference;
  setPreference: (preference: ThemePreference) => void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

export function ThemeProvider({ children }: { children: ReactNode }) {
  // The attribute was already set by public/theme-init.js; this makes the state agree with it and with storage.
  const [preference, setState] = useState<ThemePreference>(readThemePreference);

  const setPreference = useCallback((next: ThemePreference) => {
    applyThemePreference(next);
    writeThemePreference(next);
    setState(next);
  }, []);

  const value = useMemo(() => ({ preference, setPreference }), [preference, setPreference]);
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeContextValue {
  const value = useContext(ThemeContext);
  if (value === null) throw new Error("useTheme needs a ThemeProvider");
  return value;
}
