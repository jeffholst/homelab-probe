import type { Check, CheckStatus } from "../../api/setup";
import { Text } from "../Text";
import { Icon, type IconName } from "./Icon";

const STATUS: Record<CheckStatus, { icon: IconName; word: string }> = {
  ok: { icon: "checkCircle", word: "Passed" },
  warn: { icon: "alert", word: "Warning" },
  fail: { icon: "xCircle", word: "Failed" },
  skip: { icon: "minusCircle", word: "Skipped" },
  info: { icon: "info", word: "Note" },
};

/** The checks of `hlp doctor` as the server answered them: a word and an icon per status, the server's own sentences as text. */
export function ChecksList({ checks, label }: { checks: readonly Check[]; label: string }) {
  return (
    <ul className="checklist" aria-label={label}>
      {checks.map((check) => (
        <li key={check.id} className="checklist__item">
          <span className={`status-${check.status}`}>
            <Icon name={STATUS[check.status].icon} />
          </span>
          <span className="checklist__title">
            <span className="visually-hidden">{STATUS[check.status].word}: </span>
            <Text value={check.title} />
          </span>
          {check.message !== "" && (
            <p>
              <Text value={check.message} />
            </p>
          )}
          {check.fix !== "" && check.status !== "ok" && (
            <p className="muted small">
              <Text value={check.fix} />
            </p>
          )}
        </li>
      ))}
    </ul>
  );
}
