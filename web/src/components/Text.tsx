import { safeText } from "../lib/safeText";

interface TextProps {
  /** A controller string, a name typed by someone, a number: anything that is not this app's own wording. */
  value: unknown;
  /** Cut a long value to this many characters, with an ellipsis. */
  limit?: number;
  /** Shown when the value is empty after cleaning. */
  fallback?: string;
}

/**
 * Renders a value as text and nothing else: cleaned by `safeText` (control, invisible and bidirectional-override
 * characters removed, one line) and placed as a React text child, so it is never parsed as markup. Isolated, so a
 * right-to-left name cannot reorder the text around it. Use this for every string that came from the controller, an
 * account or a note; this app's own wording can be plain JSX.
 */
export function Text({ value, limit = 0, fallback = "" }: TextProps) {
  const text = safeText(value, limit);
  return (
    <span className="text" dir="auto">
      {text === "" ? fallback : text}
    </span>
  );
}
