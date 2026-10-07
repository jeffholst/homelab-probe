import { useId } from "react";

import { THEME_CHOICES, type ThemePreference } from "../theme/storage";
import { useTheme } from "../theme/ThemeProvider";
import { Icon, type IconName } from "./ui/Icon";

const CHOICES: Record<ThemePreference, { label: string; icon: IconName }> = {
  dark: { label: "Dark", icon: "moon" },
  light: { label: "Light", icon: "sun" },
  system: { label: "System", icon: "monitor" },
};

/**
 * Dark (the brand's default), light or follow the system, as a segmented control: real radio buttons, so the keyboard
 * (arrow keys) and screen readers work as they do for any radio group. Applied at once and remembered in this browser.
 */
export function ThemeSwitch({ compact = false }: { compact?: boolean }) {
  const { preference, setPreference } = useTheme();
  const name = useId();
  return (
    <fieldset className={`segmented${compact ? " segmented--compact" : ""}`}>
      <legend className={compact ? "visually-hidden" : "segmented__legend"}>Theme</legend>
      {THEME_CHOICES.map((choice) => (
        <label key={choice} className="segmented__option">
          <input
            type="radio"
            name={name}
            value={choice}
            checked={preference === choice}
            onChange={() => {
              setPreference(choice);
            }}
          />
          <span className="segmented__face">
            <Icon name={CHOICES[choice].icon} />
            <span>{CHOICES[choice].label}</span>
          </span>
        </label>
      ))}
    </fieldset>
  );
}
