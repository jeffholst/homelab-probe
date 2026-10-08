import { act, renderHook, waitFor } from "@testing-library/react";
import { StrictMode, type ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import type { ExecuteOutcome, TerminalBackend, CompletionOutcome } from "./backend";
import { engine } from "./engine";
import { MOCK_CAPABILITIES } from "./mock";
import { useTerminalSession } from "./useTerminalSession";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

async function setup() {
  const output = deferred<ExecuteOutcome>();
  const completion = deferred<CompletionOutcome>();
  const backend: TerminalBackend = {
    loadCapabilities: vi.fn(() => Promise.resolve(MOCK_CAPABILITIES)),
    execute: vi.fn(() => output.promise), complete: vi.fn(() => completion.promise),
  };
  const services = { engine, backend };
  const hook = renderHook(() => useTerminalSession(services, true), { wrapper: ({ children }: { children: ReactNode }) => <StrictMode>{children}</StrictMode> });
  await waitFor(() => { expect(hook.result.current.status).toBe("ready"); });
  return { ...hook, backend, output, completion };
}

describe("terminal integration lifecycle", () => {
  it("drops an old execution result after engine reset without releasing a newer execution", async () => {
    const { result, backend, output } = await setup();
    const next = deferred<ExecuteOutcome>();
    vi.mocked(backend.execute).mockImplementationOnce(() => output.promise).mockImplementationOnce(() => next.promise);
    act(() => { result.current.input("info\r"); });
    act(() => {
      result.current.dispatch({ type: "reset" });
      result.current.dispatch({ type: "setCapabilities", capabilities: MOCK_CAPABILITIES });
      result.current.input("query clients\r");
    });
    await act(async () => { output.resolve({ kind: "output", text: "old secret", truncated: false }); await output.promise; });
    expect(result.current.entries.map((e) => e.lines.join("\n"))).not.toContain("old secret");
    expect(result.current.status).toBe("running");
    expect(result.current.state.running).toBe(true);
    await act(async () => { next.resolve({ kind: "output", text: "new output", truncated: false }); await next.promise; });
    expect(result.current.status).toBe("ready");
  });

  it("loads once under StrictMode; empty Enter, paste and rapid Enter never dispatch additional execution", async () => {
    const { result, backend, output } = await setup();
    expect(backend.loadCapabilities).toHaveBeenCalledTimes(1);
    act(() => { result.current.input("\r"); result.current.input("\u001b[200~info\ninfo\u001b[201~"); });
    expect(backend.execute).not.toHaveBeenCalled();
    act(() => { result.current.dispatch({ type: "cancelPaste" }); result.current.input("info\r\r"); });
    expect(backend.execute).toHaveBeenCalledTimes(1);
    await act(async () => { output.resolve({ kind: "output", text: "ok", warnings: ["partial"], truncated: true }); await output.promise; });
    expect(result.current.entries.map((e) => e.lines.join("\n"))).toEqual(["info", "ok", "The server cut the output: it is incomplete.", "partial"]);
    expect(result.current.status).toBe("ready");
  });

  it("aborts session requests and discards late output after disposal; another session starts empty", async () => {
    const { result, backend, output, unmount } = await setup();
    act(() => { result.current.input("info\r"); });
    const execute = vi.mocked(backend.execute);
    const signal = execute.mock.calls[0]?.[1];
    unmount();
    expect(signal?.aborted).toBe(true);
    const next = await setup();
    await act(async () => { output.resolve({ kind: "output", text: "previous user secret", truncated: false }); await output.promise; });
    expect(next.result.current.entries).toEqual([]);
    expect(next.result.current.state.history.entries).toEqual([]);
    expect(next.result.current.state.line.text).toBe("");
    expect(next.result.current.state.running).toBe(false);
  });

  it("only explicit Tab requests completion, coalesces repeated Tab, and ignores a response after cursor movement", async () => {
    const { result, backend, completion } = await setup();
    act(() => { result.current.input("query "); });
    expect(backend.complete).not.toHaveBeenCalled();
    act(() => { result.current.input("\t\t\t"); });
    await waitFor(() => { expect(backend.complete).toHaveBeenCalledTimes(1); });
    const signal = vi.mocked(backend.complete!).mock.calls[0]?.[1];
    act(() => { result.current.dispatch({ type: "left" }); });
    expect(signal?.aborted).toBe(true);
    const before = result.current.state.suggestions;
    await act(async () => { completion.resolve({ candidates: [], truncated: false }); await completion.promise; });
    expect(result.current.state.suggestions).toEqual(before);
    expect(backend.execute).not.toHaveBeenCalled();
  });

  it("completion failure does not corrupt the draft or block submission", async () => {
    const { result, backend } = await setup();
    backend.complete = vi.fn(() => Promise.reject(new Error("private")));
    act(() => { result.current.input("query \t"); });
    await waitFor(() => { expect(result.current.announcement).toContain("Completion is unavailable"); });
    expect(result.current.state.line.text).toBe("query ");
    act(() => { result.current.input("clients\r"); });
    expect(backend.execute).toHaveBeenCalledTimes(1);
  });

  it("Ctrl+L clears the transcript without clearing the editable draft", async () => {
    const { result, output } = await setup();
    act(() => { result.current.input("info\r"); });
    await act(async () => { output.resolve({ kind: "output", text: "ok", truncated: false }); await output.promise; });
    act(() => { result.current.input("query\u000c"); });
    expect(result.current.entries).toEqual([]);
    expect(result.current.state.line.text).toBe("query");
  });
});
