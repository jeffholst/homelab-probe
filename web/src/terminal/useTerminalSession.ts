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

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

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
        push("command", effect.line);
        setStatus("running");
        setAnnouncement("Running the command.");
        const seq = effect.seq;
        void backend.execute(effect.argv).then(
          (outcome) => {
            if (!alive.current) return;
            if (outcome.kind === "output") {
              push("output", outcome.text);
              if (outcome.truncated) push("warning", "The server cut the output: it is incomplete.");
              setAnnouncement(outcome.truncated ? "The command finished; its output was truncated." : "The command finished.");
            } else {
              push("error", outcome.ran === "no" ? `Did not run: ${outcome.message}` : `Outcome unknown: ${outcome.message}`);
              setAnnouncement(outcome.ran === "no" ? "The command did not run." : "The outcome of the command is unknown.");
            }
            finish(seq);
          },
          () => {
            if (!alive.current) return;
            push("error", "Outcome unknown: the request failed.");
            setAnnouncement("The outcome of the command is unknown.");
            finish(seq);
          },
        );
      } else if (effect.type === "interrupted") {
        push("notice", effect.running ? "Stopped waiting. The command may still be running on the server." : "^C");
        setAnnouncement(effect.running ? "Stopped waiting for the command." : "Line cleared.");
      }
    }
  }, [backend, push, finish]);

  const dispatch = useCallback((action: EngineAction) => {
    const previous = stateRef.current;
    const result = engine.step(previous, action);
    stateRef.current = result.state;
    setState(result.state);
    if (result.state.notice && result.state.notice !== previous.notice) setAnnouncement(noticeText(result.state.notice));
    apply(result.effects);
  }, [engine, apply]);

  // Load the command list the first time the panel opens (React runs this twice in strict mode; the ref makes it one).
  useEffect(() => {
    if (!active || loaded.current) return;
    loaded.current = true;
    setStatus("loading");
    backend.loadCapabilities().then(
      (capabilities) => {
        if (!alive.current) return;
        dispatch({ type: "setCapabilities", capabilities });
        setStatus("ready");
        setAnnouncement("Terminal ready.");
      },
      () => {
        if (!alive.current) return;
        dispatch({ type: "setCapabilities", capabilities: null });
        push("error", "The terminal is unavailable: its command list could not be loaded.");
        setStatus("unavailable");
        setAnnouncement("The terminal is unavailable.");
      },
    );
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
