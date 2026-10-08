import { lazy, Suspense, useCallback, useEffect, useId, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";

import { Icon } from "../components/ui/Icon";
import { clampPanelHeight, readPanelHeight, writePanelHeight } from "../theme/storage";
import type { TerminalServices } from "./backend";
import { useTerminalSession, type SessionStatus } from "./useTerminalSession";

// The terminal library is a separate chunk: nothing of it is fetched until the panel is first opened.
const XtermSurface = lazy(() => import("./XtermSurface"));

export type DockMode = "closed" | "open" | "collapsed" | "expanded";

const STATUS_WORDS: Record<SessionStatus, string> = {
  idle: "Closed", loading: "Loading", ready: "Ready", running: "Running", unavailable: "Unavailable",
};
const KEY_STEP = 24;

/** The tallest the open panel may be in this window: what is left under the header, with room to see the page. */
function viewportMax(): number {
  return Math.max(200, window.innerHeight - 96);
}

interface Props {
  services: TerminalServices;
  mode: DockMode;
  onMode: (mode: DockMode) => void;
  /** Where focus goes when the user leaves the terminal with Escape (the button that opened it). */
  returnFocus?: () => void;
}

/**
 * The terminal panel: a dock at the bottom of the app that can be resized, collapsed, expanded and closed. It stays
 * mounted while closed so a command that is running is never cancelled by hiding the panel (the bar says so). The
 * transcript, history and drafts live in memory only; the one thing remembered is the height.
 */
export function TerminalDock({ services, mode, onMode, returnFocus }: Props) {
  const session = useTerminalSession(services, mode !== "closed");
  // A height saved on a bigger screen is brought inside this window, and again whenever the window changes size.
  const [height, setHeight] = useState(() => clampPanelHeight(readPanelHeight(), viewportMax()));
  const [opened, setOpened] = useState(false);
  const titleId = useId();
  const dock = useRef<HTMLElement>(null);
  const dragging = useRef(false);
  const visible = mode !== "closed";
  if (visible && !opened) setOpened(true);

  const resizeTo = useCallback((value: number, persist: boolean) => {
    const next = clampPanelHeight(value, viewportMax());
    setHeight(next);
    if (persist) writePanelHeight(next);
  }, []);

  const onPointerMove = (event: PointerEvent<HTMLDivElement>) => {
    if (dragging.current) resizeTo(window.innerHeight - event.clientY, false);
  };
  const onPointerUp = (event: PointerEvent<HTMLDivElement>) => {
    if (!dragging.current) return;
    dragging.current = false;
    event.currentTarget.releasePointerCapture(event.pointerId);
    resizeTo(window.innerHeight - event.clientY, true);
  };
  const onHandleKey = (event: KeyboardEvent<HTMLDivElement>) => {
    const delta = event.key === "ArrowUp" ? KEY_STEP : event.key === "ArrowDown" ? -KEY_STEP : 0;
    if (event.key === "Home") resizeTo(0, true);
    else if (event.key === "End") resizeTo(viewportMax(), true);
    else if (delta !== 0) resizeTo(height + delta, true);
    else return;
    event.preventDefault();
  };

  useEffect(() => {
    const onResize = () => {
      setHeight((current) => Math.min(current, viewportMax()));
    };
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
    };
  }, []);

  // The page keeps room for the open dock so it never covers the end of a page.
  useEffect(() => {
    const root = document.documentElement;
    const space = mode === "open" ? height : mode === "collapsed" ? 48 : 0;
    root.style.setProperty("--dock-space", `${space}px`);
    return () => {
      root.style.removeProperty("--dock-space");
    };
  }, [mode, height]);

  const leave = useCallback(() => {
    if (returnFocus) returnFocus();
    else dock.current?.focus();
  }, [returnFocus]);

  const running = session.status === "running";
  const suggestions = session.state.suggestions.items.slice(0, 12);

  return (
    <>
      <p className="visually-hidden" role="status" aria-live="polite" aria-atomic="true">
        {session.announcement}
      </p>
      {visible && (
        <section
          ref={dock}
          className="terminal-dock"
          data-mode={mode}
          aria-labelledby={titleId}
          tabIndex={-1}
          style={mode === "open" ? { height } : undefined}
        >
          {mode === "open" && (
            <div
              className="terminal-dock__handle"
              role="slider"
              aria-orientation="vertical"
              aria-label="Resize the terminal"
              aria-valuemin={160}
              aria-valuemax={viewportMax()}
              aria-valuenow={height}
              aria-valuetext={`${height} pixels high`}
              tabIndex={0}
              onPointerDown={(event) => {
                dragging.current = true;
                event.currentTarget.setPointerCapture(event.pointerId);
              }}
              onPointerMove={onPointerMove}
              onPointerUp={onPointerUp}
              onPointerCancel={() => {
                dragging.current = false;
              }}
              onKeyDown={onHandleKey}
            />
          )}
          <header className="terminal-dock__bar">
            <h2 id={titleId} className="terminal-dock__title">
              <Icon name="terminal" />
              Terminal
            </h2>
            <span className={`pill pill--${session.status === "unavailable" ? "danger" : running ? "warning" : "info"}`}>
              {STATUS_WORDS[session.status]}
            </span>
            {mode === "collapsed" && running && <span className="terminal-dock__note">A command is still running.</span>}
            <span className="terminal-dock__spacer" />
            <button type="button" className="icon-button" aria-label="Clear the terminal output" onClick={session.clear}>
              <Icon name="clear" />
            </button>
            {mode === "collapsed" ? (
              <button type="button" className="icon-button" aria-label="Expand the terminal panel" onClick={() => { onMode("open"); }}>
                <Icon name="chevronUp" />
              </button>
            ) : (
              <button type="button" className="icon-button" aria-label="Collapse the terminal panel" onClick={() => { onMode("collapsed"); }}>
                <Icon name="chevronDown" />
              </button>
            )}
            <button
              type="button"
              className="icon-button"
              aria-label={mode === "expanded" ? "Restore the terminal panel size" : "Fill the page with the terminal"}
              aria-pressed={mode === "expanded"}
              onClick={() => { onMode(mode === "expanded" ? "open" : "expanded"); }}
            >
              <Icon name={mode === "expanded" ? "shrink" : "expand"} />
            </button>
            <button type="button" className="icon-button" aria-label={running ? "Close the terminal panel (the command keeps running)" : "Close the terminal panel"} onClick={() => { onMode("closed"); }}>
              <Icon name="close" />
            </button>
          </header>
          <div className="terminal-dock__body" hidden={mode === "collapsed"}>
            {opened && (
              <Suspense fallback={<p className="terminal-dock__loading muted">Loading the terminal…</p>}>
                <XtermSurface entries={session.entries} state={session.state} onInput={session.input} onLeave={leave} label="Terminal input" />
              </Suspense>
            )}
            <div className="terminal-dock__toolbar" role="toolbar" aria-label="Shortcuts" aria-describedby={`${titleId}-hint`}>
              {suggestions.map((item, index) => (
                <button
                  key={`${item.kind}:${item.label}`}
                  type="button"
                  className="terminal-chip"
                  data-kind={item.kind}
                  title={item.description}
                  onClick={() => { session.dispatch({ type: "selectSuggestion", index }); }}
                >
                  {item.label}
                </button>
              ))}
              {suggestions.length === 0 && <span className="terminal-dock__hint muted">{session.status === "unavailable" ? "Unavailable." : "Type a command."}</span>}
            </div>
            <p id={`${titleId}-hint`} className="visually-hidden">
              Press Escape to leave the terminal and Tab to move on. Shortcut buttons add text to the command line; they never run it.
            </p>
            <div role="log" aria-live="off" aria-label="Terminal transcript" className="visually-hidden">
              {session.entries.map((entry) => (
                <p key={entry.id}>{entry.lines.join("\n")}</p>
              ))}
            </div>
          </div>
        </section>
      )}
    </>
  );
}
