/** A moment as the reader's locale writes it (`ms` since the epoch, as `dataUpdatedAt` is), or "" when it is unknown. */
export function formatDateTime(ms: number | undefined): string {
  if (ms === undefined || !Number.isFinite(ms) || ms <= 0) return "";
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "medium" }).format(new Date(ms));
}

/** "1 hour 5 minutes", "30 seconds": a duration for a person, rounded down to the two largest units. */
export function formatDuration(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  const units: [string, number][] = [
    ["day", 86_400],
    ["hour", 3_600],
    ["minute", 60],
    ["second", 1],
  ];
  const parts: string[] = [];
  let rest = total;
  for (const [name, size] of units) {
    const count = Math.floor(rest / size);
    rest -= count * size;
    if (count > 0 && parts.length < 2) parts.push(`${count} ${name}${count === 1 ? "" : "s"}`);
  }
  return parts.length > 0 ? parts.join(" ") : "0 seconds";
}

/**
 * How long ago `ms` (since the epoch) was, for a person: "just now", "5 minutes ago", "3 hours ago", "2 days ago"
 * (the largest unit only). A moment in the future (clock skew) is "just now"; an unknown one is "".
 */
export function formatAgo(ms: number | null | undefined, now: number = Date.now()): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms) || ms <= 0) return "";
  const seconds = Math.floor((now - ms) / 1000);
  if (seconds < 60) return "just now";
  const units: [string, number][] = [
    ["day", 86_400],
    ["hour", 3_600],
    ["minute", 60],
  ];
  for (const [name, size] of units) {
    const count = Math.floor(seconds / size);
    if (count > 0) return `${count} ${name}${count === 1 ? "" : "s"} ago`;
  }
  return "just now";
}
