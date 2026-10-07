import { useState, type InputHTMLAttributes } from "react";

import { Icon } from "./Icon";

type Props = Omit<InputHTMLAttributes<HTMLInputElement>, "type" | "className"> & { id: string; mono?: boolean };

/**
 * A password field with a button that shows what was typed (on a phone a long token or key is easy to mistype). It is
 * hidden again when the field loses its value. The value lives in the caller's state only: never stored anywhere.
 */
export function SecretInput({ mono = false, ...props }: Props) {
  const [shown, setShown] = useState(false);
  return (
    <div className="input-group">
      <input
        {...props}
        type={shown ? "text" : "password"}
        className={mono ? "input input--mono" : "input"}
        autoCapitalize="none"
        autoCorrect="off"
        spellCheck={false}
      />
      <button
        type="button"
        className="icon-button"
        aria-label={shown ? "Hide" : "Show"}
        aria-controls={props.id}
        aria-pressed={shown}
        onClick={() => {
          setShown((value) => !value);
        }}
      >
        <Icon name={shown ? "eyeOff" : "eye"} />
      </button>
    </div>
  );
}
