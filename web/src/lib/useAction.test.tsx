import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { useAction } from "./useAction";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

describe("useAction", () => {
  it("runs the call, keeps its outcome and reports success", async () => {
    const onSuccess = vi.fn();
    const { result } = renderHook(() => useAction({ mutationFn: (value: string) => Promise.resolve(value.toUpperCase()), onSuccess }));
    expect(result.current.isPending).toBe(false);
    act(() => {
      result.current.mutate("abc");
    });
    expect(result.current.isPending).toBe(true);
    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    expect(result.current.data).toBe("ABC");
    expect(onSuccess).toHaveBeenCalledWith("ABC", "abc");
  });

  it("reports an error, runs both error callbacks and can be reset", async () => {
    const onError = vi.fn();
    const perCall = vi.fn();
    const { result } = renderHook(() => useAction({ mutationFn: () => Promise.reject(new Error("no")), onError }));
    act(() => {
      result.current.mutate(undefined, { onError: perCall });
    });
    await waitFor(() => {
      expect(result.current.isError).toBe(true);
    });
    expect((result.current.error as Error).message).toBe("no");
    expect(onError).toHaveBeenCalledOnce();
    expect(perCall).toHaveBeenCalledOnce();
    act(() => {
      result.current.reset();
    });
    expect(result.current.isError).toBe(false);
    expect(result.current.error).toBeUndefined();
  });

  it("treats a failing onSuccess as a failure of the call", async () => {
    const { result } = renderHook(() =>
      useAction({
        mutationFn: () => Promise.resolve(1),
        onSuccess: () => {
          throw new Error("could not continue");
        },
      }),
    );
    act(() => {
      result.current.mutate(undefined);
    });
    await waitFor(() => {
      expect(result.current.isError).toBe(true);
    });
  });

  it("ignores a second call while one is running (no duplicate submissions) and allows one afterwards", async () => {
    const first = deferred<string>();
    const fn = vi.fn((value: string) => first.promise.then((done) => `${done}:${value}`));
    const { result } = renderHook(() => useAction({ mutationFn: fn }));
    act(() => {
      result.current.mutate("one");
      result.current.mutate("two");
    });
    expect(fn).toHaveBeenCalledTimes(1);
    await act(async () => {
      first.resolve("done");
      await first.promise;
    });
    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    act(() => {
      result.current.mutate("three");
    });
    expect(fn).toHaveBeenCalledTimes(2);
  });

  it("does not show the outcome of a call that was reset meanwhile, but still runs its callbacks", async () => {
    const pending = deferred<string>();
    const onSuccess = vi.fn();
    const { result } = renderHook(() => useAction({ mutationFn: () => pending.promise, onSuccess }));
    act(() => {
      result.current.mutate(undefined);
    });
    act(() => {
      result.current.reset();
    });
    await act(async () => {
      pending.resolve("late");
      await pending.promise;
    });
    expect(result.current.isSuccess).toBe(false);
    expect(result.current.data).toBeUndefined();
    expect(onSuccess).toHaveBeenCalledWith("late", undefined);
  });

  it("does not touch state after the component is gone, and runs the parent's callbacks", async () => {
    const pending = deferred<string>();
    const onSuccess = vi.fn();
    const perCall = vi.fn();
    const { result, unmount } = renderHook(() => useAction({ mutationFn: () => pending.promise, onSuccess }));
    act(() => {
      result.current.mutate(undefined, { onSuccess: perCall });
    });
    unmount();
    await act(async () => {
      pending.resolve("late");
      await pending.promise;
    });
    expect(onSuccess).toHaveBeenCalledOnce();
    expect(perCall).not.toHaveBeenCalled(); // the component's own callback only while it exists
  });

  it("keeps nothing in the query client's mutation cache", async () => {
    const client = new QueryClient();
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
    const { result } = renderHook(() => useAction({ mutationFn: (secret: string) => Promise.resolve(secret.length) }), { wrapper });
    act(() => {
      result.current.mutate("POISON-SECRET");
    });
    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    expect(client.getMutationCache().getAll()).toEqual([]);
  });
});
