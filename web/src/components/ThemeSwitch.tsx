import { useId } from "react";

import { THEME_CHOICES, isThemePreference, type ThemePreference } from "../theme/storage";
import { useTheme } from "../theme/ThemeProvider";

const LABELS: Record<ThemePreference, string> = { system: "System", light: "Light", dark: "Dark" };

/** Light, dark or follow the system. The choice is applied at once and remembered in this browser. */
export function ThemeSwitch() {
  const { preference, setPreference } = useTheme();
  const id = useId();
  return (
    <div className="theme-switch">
      <label htmlFor={id}>Theme</label>
      <select
        id={id}
        className="select"
        value={preference}
        onChange={(event) => {
          if (isThemePreference(event.target.value)) setPreference(event.target.value);
        }}
      >
        {THEME_CHOICES.map((choice) => (
          <option key={choice} value={choice}>
            {LABELS[choice]}
          </option>
        ))}
      </select>
    </div>
  );
}
