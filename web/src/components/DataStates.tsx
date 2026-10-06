import { type ReactNode } from "react";

import { isApiError } from "../api/errors";
import { formatDateTime } from "../lib/format";
import { Text } from "./Text";

/**
 * The states every page shows while it reads the server: Loading, Refreshing, Empty, Error, and the Stale and Partial
 * banners. Use `DataView` (DataView.tsx) to pick between them for a query; these are the pieces.
 *
 * Wording rules: say what is happening and what the person can do; a stale or partial view says so and never implies
 * the data is current or complete. A message that came from the server is shown through `<Text>`.
 */

type Tone = "danger" | "warning" | "info" | "success";

interface BannerProps {
  tone: Tone;
  title: string;
  children?: ReactNode;
  /** `alert` interrupts a screen reader and suits an error; `status` waits its turn. */
  role?: "alert" | "status";
  action?: ReactNode;
}

/** A message above the content, tinted by its tone. The title carries the meaning too: colour is never the only cue. */
export function Banner({ tone, title, children, role = "status", action }: BannerProps) {
  return (
    <div className={`banner banner--${tone}`} role={role}>
      <div className="banner__body">
        <p className="banner__title">{title}</p>
        {children}
      </div>
      {action}
    </div>
  );
}

export function Loading({ label }: { label: string }) {
  return (
    <div className="state" role="status" aria-live="polite">
      <div className="spinner" aria-hidden="true" />
      <p className="state__title">Loading {label}…</p>
    </div>
  );
}

/** Shown beside data that is already on screen while it is read again: the old data stays. */
export function Refreshing({ label }: { label: string }) {
  return (
    <p className="refreshing" role="status">
      <span className="spinner" aria-hidden="true" />
      Refreshing {label}…
    </p>
  );
}

export function Empty({ title, children, action }: { title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="state">
      <p className="state__title">{title}</p>
      {children !== undefined && <p className="state__text">{children}</p>}
      {action}
    </div>
  );
}

/** Why a read failed, in the server's fixed sentence, with a way to try again. */
export function ErrorState({ error, label, onRetry }: { error: unknown; label: string; onRetry?: () => void }) {
  const message = isApiError(error) ? error.message : "Something went wrong.";
  return (
    <div className="state" role="alert">
      <p className="state__title">Could not load {label}</p>
      <p className="state__text">
        <Text value={message} />
      </p>
      {isApiError(error) && error.code !== "bad_response" && (
        <p className="state__text">
          Error code: <code>{error.code}</code>
        </p>
      )}
      {onRetry && (
        <button type="button" className="button" onClick={onRetry}>
          Try again
        </button>
      )}
    </div>
  );
}

interface StatusBannerProps {
  kind: "stale" | "partial";
  label: string;
  /** When the data on screen was read (ms since the epoch), if known. */
  since?: number | undefined;
  /** What could not be read, for a partial view (the `warnings` of a document); shown as text. */
  warnings?: readonly string[] | undefined;
  /** Stale: why the refresh failed. */
  error?: unknown;
  onRetry?: (() => void) | undefined;
}

/**
 * Stale: the last refresh failed, so the data on screen is older than it looks. Partial: the data is current but
 * something could not be read, so it is incomplete. Both keep the data and say what is limited.
 */
export function StatusBanner({ kind, label, since, warnings = [], error, onRetry }: StatusBannerProps) {
  const when = formatDateTime(since);
  const retry = onRetry && (
    <button type="button" className="button button--secondary" onClick={onRetry}>
      Try again
    </button>
  );
  if (kind === "stale") {
    return (
      <Banner tone="warning" title={`Showing older ${label}`} action={retry}>
        <p className="banner__text">
          The latest refresh failed
          {isApiError(error) ? (
            <>
              : <Text value={error.message} />
            </>
          ) : (
            "."
          )}
          {when !== "" && <> The data on screen is from {when}.</>}
        </p>
      </Banner>
    );
  }
  return (
    <Banner tone="warning" title={`Some ${label} could not be read`}>
      <p className="banner__text">What you see may be incomplete{when !== "" ? ` (read ${when})` : ""}.</p>
      {warnings.length > 0 && (
        <ul>
          {warnings.map((warning, index) => (
            <li key={index}>
              <Text value={warning} />
            </li>
          ))}
        </ul>
      )}
    </Banner>
  );
}
