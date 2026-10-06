import { type ReactNode } from "react";

import { Empty, ErrorState, Loading, Refreshing, StatusBanner } from "./DataStates";

/** The part of a TanStack Query result that `DataView` reads (so a test, or a page that combines queries, can pass its own). */
export interface QueryLike<T> {
  data: T | undefined;
  error: unknown;
  isPending: boolean;
  isFetching: boolean;
  dataUpdatedAt: number;
  refetch: () => unknown;
}

interface DataViewProps<T> {
  query: QueryLike<T>;
  /** What the page shows, as a plural noun for the messages: "platforms", "findings". */
  label: string;
  /** True when `data` holds nothing to list; then `empty` is shown instead of `children`. */
  isEmpty?: (data: T) => boolean;
  /** What to say when there is nothing; the title, and optionally an explanation. */
  empty?: { title: string; text?: ReactNode; action?: ReactNode };
  /** Warnings of a document that was read only in part: the view says so and keeps the data. */
  warnings?: readonly string[];
  children: (data: T) => ReactNode;
}

/**
 * Chooses what a page shows for a query, so every page treats the same situation the same way:
 *
 * - no data yet, reading: **Loading**;
 * - no data, and the read failed: **Error** with a retry (nothing to keep);
 * - data on screen and it is read again: the data stays, with a **Refreshing** line (a manual refresh never blanks the page);
 * - data on screen and the refresh failed: the data stays under a **stale** banner that says what failed and from when the data is;
 * - data with `warnings`: the data under a **partial** banner;
 * - data that is an empty list: **Empty**.
 */
export function DataView<T>({ query, label, isEmpty, empty, warnings = [], children }: DataViewProps<T>) {
  const retry = () => {
    void query.refetch();
  };
  const { data } = query;
  if (data === undefined) {
    if (query.isPending) return <Loading label={label} />;
    return <ErrorState error={query.error} label={label} onRetry={retry} />;
  }
  const failed = query.error !== null && query.error !== undefined;
  return (
    <>
      {failed && !query.isFetching && (
        <StatusBanner kind="stale" label={label} since={query.dataUpdatedAt} error={query.error} onRetry={retry} />
      )}
      {warnings.length > 0 && <StatusBanner kind="partial" label={label} since={query.dataUpdatedAt} warnings={warnings} />}
      {query.isFetching && <Refreshing label={label} />}
      {isEmpty?.(data) ? <Empty title={empty?.title ?? `No ${label}`} action={empty?.action}>{empty?.text}</Empty> : children(data)}
    </>
  );
}
