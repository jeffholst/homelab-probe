import { useCallback, useEffect, useRef, useState } from "react";

/**
 * A call to the server that carries something secret (a password, the API key, a passphrase, a backup file): like
 * TanStack's `useMutation`, but **nothing is kept in a shared cache**. `useMutation` stores each call's variables in the
 * query client's mutation cache, where they stay (readable by any code of the page) for minutes after the component is
 * gone; this hook keeps only the outcome (`data` or `error`) in the component's own state, and the variables only for
 * the time the call is on its way.
 *
 * It is not for reads (use `useQuery`) and it never retries: a call that may have happened is never sent twice. A
 * second `mutate` while one is running is ignored, so a double click cannot send twice either.
 *
 * `reset` **abandons** a call that is still on its way (it cannot be cancelled): its result is ignored, none of the
 * callbacks run for it, and a new `mutate` may start at once. A call whose component is removed meanwhile still ends
 * normally: `onSuccess` and `onError` run (the parent usually moves on in them) and only the component's own state and
 * per-call callbacks are skipped. With `keepResult: false` the result is passed to `onSuccess` and not kept at all
 * (for a large secret buffer that the caller keeps itself and must be able to drop).
 */
export interface ActionOptions<T, V> {
  mutationFn: (variables: V) => Promise<T>;
  onSuccess?: (data: T, variables: V) => unknown;
  onError?: (error: unknown, variables: V) => unknown;
  /** Keep the result in `data` (default). `false`: hand it to `onSuccess` only. */
  keepResult?: boolean;
}

export interface ActionCallbacks<T> {
  onSuccess?: (data: T) => void;
  onError?: (error: unknown) => void;
}

export interface Action<T, V> {
  mutate: (variables: V, callbacks?: ActionCallbacks<T>) => void;
  reset: () => void;
  isPending: boolean;
  isError: boolean;
  isSuccess: boolean;
  error: unknown;
  data: T | undefined;
}

interface Outcome<T> {
  status: "idle" | "pending" | "success" | "error";
  data?: T;
  error?: unknown;
}

export function useAction<T, V = void>(options: ActionOptions<T, V>): Action<T, V> {
  const [outcome, setOutcome] = useState<Outcome<T>>({ status: "idle" });
  const latest = useRef(options);
  const mounted = useRef(true);
  const running = useRef(false);
  const generation = useRef(0);

  useEffect(() => {
    latest.current = options;
  });
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const mutate = useCallback((variables: V, callbacks?: ActionCallbacks<T>) => {
    if (running.current) return;
    running.current = true;
    generation.current += 1;
    const mine = generation.current;
    // False once `reset` (or another call) has taken over: this call's outcome is then nobody's business.
    const live = () => generation.current === mine;
    setOutcome({ status: "pending" });
    void (async () => {
      let data: T;
      try {
        data = await latest.current.mutationFn(variables);
        if (live()) await latest.current.onSuccess?.(data, variables);
      } catch (error) {
        if (!live()) return;
        running.current = false;
        await latest.current.onError?.(error, variables);
        if (live() && mounted.current) {
          setOutcome({ status: "error", error });
          callbacks?.onError?.(error);
        }
        return;
      }
      if (!live()) return;
      running.current = false;
      if (mounted.current) {
        setOutcome(latest.current.keepResult === false ? { status: "success" } : { status: "success", data });
        callbacks?.onSuccess?.(data);
      }
    })();
  }, []);

  const reset = useCallback(() => {
    generation.current += 1;
    running.current = false;
    setOutcome({ status: "idle" });
  }, []);

  return {
    mutate,
    reset,
    isPending: outcome.status === "pending",
    isError: outcome.status === "error",
    isSuccess: outcome.status === "success",
    error: outcome.error,
    data: outcome.data,
  };
}
