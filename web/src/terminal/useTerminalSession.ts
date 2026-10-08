import { useCallback, useEffect, useRef, useState } from "react";

import type { TerminalServices } from "./backend";
import { cleanLines } from "./clean";
import type { EngineAction, EngineState, Effect } from "./engine/types";
import type { Entry, EntryKind } from "./render";
import { noticeText } from "./render";

/** The most lines of one report shown, and of the whole transcript kept (older entries are dropped first). */
export const MAX_OUTPUT_LINES = 2000;
export const MAX_TRANSCRIPT_LINES = 5000;

export type SessionStatus = "idle" | "loading" | "ready" | "running" | "unavailable";

export interface TerminalSession {
  readonly state: EngineState;
  readonly entries: readonly Entry[];
  readonly status: SessionStatus;
  /** A short sentence for the polite live region: what just happened, never the transcript. */
  readonly announcement: string;
  dispatch: (action: EngineAction) => void;
  /** Feed what xterm delivered to `onData`. */
  input: (data: string) => void;
  clear: () => void;
}

function bounded(entries: Entry[]): Entry[] {
  let total = entries.reduce((sum, entry) => sum + entry.lines.length, 0);
  let start = 0;
  while (total > MAX_TRANSCRIPT_LINES && start < entries.length - 1) total -= entries[start++]?.lines.length ?? 0;
  return start > 0 ? entries.slice(start) : entries;
}

/**
 * Connects the engine (the source of truth for the line), the backend and the transcript. Every transition goes
 * through `dispatch`, which reads the CURRENT state from a ref, so two keys in one tick never act on stale state; the
 * effects the engine returns are the only way anything leaves (an `execute` is started exactly when the engine says
 * so, and the engine says so once until it is finished). Nothing is stored anywhere but in memory.
 */
export function useTerminalSession(services: TerminalServices, active: boolean): TerminalSession {
  const { engine, backend } = services;
  const [state, setState] = useState(() => engine.initialState());
  const [entries, setEntries] = useState<Entry[]>([]);
  const [status, setStatus] = useState<SessionStatus>("idle");
  const [announcement, setAnnouncement] = useState("");
  const stateRef = useRef(state);
  const entriesRef = useRef(entries);
  const inputRef = useRef(engine.initialInputState());
  const nextId = useRef(1);
  const alive = useRef(true);
  const loaded = useRef(false);
  const generation = useRef(0);
  const requests = useRef(new Set<AbortController>());
  const completion = useRef<{ timer: ReturnType<typeof setTimeout>; controller: AbortController } | null>(null);

  const cancelCompletion = useCallback(() => {
    if (!completion.current) return;
    clearTimeout(completion.current.timer);
    completion.current.controller.abort();
    requests.current.delete(completion.current.controller);
    completion.current = null;
  }, []);

  useEffect(() => {
    alive.current = true;
    const pending = requests.current;
    return () => {
      alive.current = false;
      generation.current += 1;
      loaded.current = false;
      cancelCompletion();
      for (const request of pending) request.abort();
      pending.clear();
      stateRef.current = engine.step(stateRef.current, { type: "reset" }).state;
      inputRef.current = engine.initialInputState();
      entriesRef.current = [];
    };
  }, [engine, backend, cancelCompletion]);

  const push = useCallback((kind: EntryKind, text: string, limitLines = MAX_OUTPUT_LINES) => {
    const lines = cleanLines(text);
    const kept = lines.length > limitLines ? lines.slice(0, limitLines) : lines;
    const added: Entry[] = [{ id: nextId.current++, kind, lines: kept }];
    if (kept.length < lines.length) {
      added.push({ id: nextId.current++, kind: "warning", lines: [`Output truncated: showing the first ${kept.length} of ${lines.length} lines.`] });
    }
    entriesRef.current = bounded([...entriesRef.current, ...added]);
    setEntries(entriesRef.current);
  }, []);

  const finish = useCallback((seq: number) => {
    const result = engine.step(stateRef.current, { type: "executionFinished", seq });
    stateRef.current = result.state;
    setState(result.state);
    setStatus(result.state.capabilities ? "ready" : "unavailable");
  }, [engine]);

  const apply = useCallback((effects: readonly Effect[]) => {
    for (const effect of effects) {
      if (effect.type === "execute") {
        cancelCompletion();
        push("command", effect.line);
        setStatus("running");
        setAnnouncement("Running the command.");
        const seq = effect.seq;
        const epoch = generation.current;
        const controller = new AbortController();
        requests.current.add(controller);
        void backend.execute(effect.argv, controller.signal).then(
          (outcome) => {
            if (!alive.current || epoch !== generation.current || stateRef.current.activeExecution !== seq) return;
            if (outcome.kind === "output") {
              push("output", outcome.text);
              if (outcome.truncated) push("warning", "The server cut the output: it is incomplete.");
              for (const warning of outcome.warnings ?? []) push("warning", warning);
              setAnnouncement(outcome.truncated ? "The command finished; its output was truncated." : "The command finished.");
            } else {
              push("error", outcome.ran === "no" ? `Did not run: ${outcome.message}` : outcome.ran === "failed" ? `Command failed: ${outcome.message}` : `Outcome unknown: ${outcome.message}`);
              setAnnouncement(outcome.ran === "no" ? "The command did not run." : outcome.ran === "failed" ? "The command failed." : "The outcome of the command is unknown.");
            }
            finish(seq);
          },
          () => {
            if (!alive.current || epoch !== generation.current || stateRef.current.activeExecution !== seq) return;
            push("error", "Outcome unknown: the request failed.");
            setAnnouncement("The outcome of the command is unknown.");
            finish(seq);
          },
        ).finally(() => { requests.current.delete(controller); });
      } else if (effect.type === "complete" && backend.complete) {
        cancelCompletion();
        const epoch = generation.current;
        const controller = new AbortController();
        requests.current.add(controller);
        const complete = backend.complete;
        const timer = setTimeout(() => {
          const pending = stateRef.current.completionPending;
          if (!alive.current || epoch !== generation.current || pending?.seq !== effect.seq || pending.revision !== effect.revision) {
            requests.current.delete(controller);
            return;
          }
          void complete(effect, controller.signal).then((outcome) => {
            if (!alive.current || epoch !== generation.current || controller.signal.aborted) return;
            const result = engine.step(stateRef.current, { type: "suggestionsArrived", seq: effect.seq, revision: effect.revision, ...outcome });
            stateRef.current = result.state;
            setState(result.state);
          }, () => {
            if (alive.current && epoch === generation.current && !controller.signal.aborted) setAnnouncement("Completion is unavailable. You can still edit or submit the command.");
          }).finally(() => { requests.current.delete(controller); });
        }, 150);
        completion.current = { timer, controller };
      } else if (effect.type === "clearOutput") {
        entriesRef.current = [];
        setEntries([]);
      } else if (effect.type === "interrupted") {
        push("notice", effect.running ? "Stopped waiting. The command may still be running on the server." : "^C");
        setAnnouncement(effect.running ? "Stopped waiting for the command." : "Line cleared.");
      }
    }
  }, [backend, engine, push, finish, cancelCompletion]);

  const dispatch = useCallback((action: EngineAction) => {
    const previous = stateRef.current;
    const result = engine.step(previous, action);
    if (result.state.revision !== previous.revision) cancelCompletion();
    stateRef.current = result.state;
    setState(result.state);
    if (result.state.notice && result.state.notice !== previous.notice) setAnnouncement(noticeText(result.state.notice));
    apply(result.effects);
  }, [engine, apply, cancelCompletion]);

  // Load the command list the first time the panel opens (React runs this twice in strict mode; the ref makes it one).
  useEffect(() => {
    if (!active || loaded.current) return;
    const epoch = generation.current;
    // StrictMode replays effects; defer admission so the disposed pass cannot start a request.
    void Promise.resolve().then(() => {
      if (!alive.current || epoch !== generation.current || loaded.current) return;
      loaded.current = true;
      setStatus("loading");
      const controller = new AbortController();
      requests.current.add(controller);
      return backend.loadCapabilities(controller.signal).then(
        (capabilities) => {
          if (!alive.current || epoch !== generation.current) return;
          dispatch({ type: "setCapabilities", capabilities });
          setStatus("ready");
          setAnnouncement("Terminal ready.");
        },
        () => {
          if (!alive.current || epoch !== generation.current) return;
          dispatch({ type: "setCapabilities", capabilities: null });
          push("error", "The terminal is unavailable: its command list could not be loaded.");
          setStatus("unavailable");
          setAnnouncement("The terminal is unavailable.");
        },
      ).finally(() => { requests.current.delete(controller); });
    });
  }, [active, backend, dispatch, push]);

  const input = useCallback((data: string) => {
    const decoded = engine.decodeInput(data, inputRef.current);
    inputRef.current = decoded.state;
    for (const action of decoded.actions) dispatch(action);
  }, [engine, dispatch]);

  const clear = useCallback(() => {
    entriesRef.current = [];
    setEntries([]);
  }, []);

  return { state, entries, status, announcement, dispatch, input, clear };
}
