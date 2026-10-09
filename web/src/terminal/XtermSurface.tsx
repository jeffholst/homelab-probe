import { FitAddon } from "@xterm/addon-fit";
import { Terminal, type IDisposable, type IMarker } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import { useEffect, useLayoutEffect, useRef, type RefObject } from "react";

import type { EngineState } from "./engine/types";
import { renderEntry, renderErase, renderLive, type Entry, type Palette } from "./render";

/** The scrollback kept by the terminal itself; the transcript behind it is bounded separately. */
export const SCROLLBACK = 5000;

interface Props {
  entries: readonly Entry[];
  state: EngineState;
  /** What xterm delivered to `onData` (typed keys, paste). Terminal answers to queries cannot occur: nothing written
   * here contains a query. */
  onInput: (data: string) => void;
  /** Escape was pressed: the way out of the terminal for keyboard users. */
  onLeave: () => void;
  label: string;
  focusRef?: RefObject<(() => void) | null>;
}

function token(name: string, fallback: string): string {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
}

/** The colours of the current theme, read from the CSS custom properties (dark is the default, light and system follow). */
function readPalette(): Palette {
  return {
    text: token("--color-text", "#e8eefb"), muted: token("--color-text-muted", "#9fb0cf"), accent: token("--color-accent", "#3b9bff"),
    brand: token("--color-brand", "#ff7a1a"), warning: token("--color-warning-text", "#ffd27a"),
    danger: token("--color-danger-text", "#ff9b8f"), success: token("--color-success-text", "#7be3a8"),
  };
}

/**
 * The xterm.js surface. It only DRAWS: the line, the cursor and the history live in the engine, and every key goes
 * to it through `onInput`. One terminal per mount; everything it registers is released in the cleanup, so React's
 * strict-mode double mount leaves exactly one live terminal. No addon but "fit" is loaded: no links, no clipboard
 * writing, no title handling, no answers to terminal queries.
 */
export default function XtermSurface({ entries, state, onInput, onLeave, label, focusRef }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const latest = useRef({ entries, state, onInput, onLeave });
  // The long-lived handlers read the newest props through this ref, which is updated after every render.
  useLayoutEffect(() => {
    latest.current = { entries, state, onInput, onLeave };
  });
  const drawer = useRef<{ sync(): void; redraw(): void } | null>(null);

  useEffect(() => {
    const element = host.current;
    if (!element) return;
    const palette = { current: readPalette() };
    const terminal = new Terminal({
      scrollback: SCROLLBACK,
      cursorBlink: false, // a blinking cursor never stops (WCAG 2.2.2) and is the animation reduced-motion users turn off
      cursorStyle: "bar",
      fontFamily: token("--font-mono", "ui-monospace, Menlo, Consolas, monospace"),
      fontSize: 14,
      lineHeight: 1.25,
      allowProposedApi: false,
      convertEol: false,
      disableStdin: false,
      macOptionIsMeta: true,
    });
    const fit = new FitAddon();
    terminal.loadAddon(fit);
    terminal.open(element);
    if (focusRef) focusRef.current = () => { terminal.focus(); };
    terminal.textarea?.setAttribute("aria-label", label);

    const applyTheme = () => {
      palette.current = readPalette();
      const p = palette.current;
      terminal.options.theme = {
        background: token("--color-surface-sunken", "#0a1020"), foreground: p.text, cursor: p.accent, cursorAccent: token("--color-surface-sunken", "#0a1020"),
        selectionBackground: `${p.accent}55`,
      };
    };
    applyTheme();

    let marker: IMarker | null = null; // the first row of the live region (xterm keeps it right through reflow)
    let written = 0; // the id of the last entry written
    let shown = ""; // what the live region was last drawn from
    let painting = false;
    let again = false;
    const rowsFromMarker = () => {
      if (!marker || marker.line < 0) return 0;
      const buffer = terminal.buffer.active;
      return Math.max(0, buffer.baseY + buffer.cursorY - marker.line);
    };
    // Paints in two steps (erase and entries, then the live region at the new first row), one paint at a time: the
    // cursor row is read from the parsed buffer, so a second paint must wait until the first has been parsed.
    const paint = (prefix: string) => {
      painting = true;
      terminal.write(prefix, () => {
        marker?.dispose();
        marker = terminal.registerMarker(0) ?? null;
        terminal.write(renderLive(latest.current.state, palette.current), () => {
          painting = false;
          if (again) {
            again = false;
            sync();
          }
        });
      });
    };
    function sync() {
      if (painting) {
        again = true;
        return;
      }
      const { entries: all, state: current } = latest.current;
      let prefix = "";
      if (all.length === 0 && written !== 0) {
        written = 0;
        terminal.reset();
        marker = null;
        shown = "";
      }
      const fresh = all.filter((entry) => entry.id > written);
      const signature = JSON.stringify([current.line, current.notice, current.running, current.suggestions.items.map((i) => i.label),
        current.suggestions.truncated, current.suggestions.selected]);
      if (fresh.length === 0 && signature === shown) return;
      shown = signature;
      prefix = renderErase(rowsFromMarker()) + fresh.map((entry) => renderEntry(entry, palette.current)).join("");
      for (const entry of fresh) written = Math.max(written, entry.id);
      paint(prefix);
    }
    const redraw = () => {
      shown = "";
      sync();
    };
    drawer.current = { sync, redraw };

    const subscriptions: IDisposable[] = [
      terminal.onData((data) => {
        latest.current.onInput(data);
      }),
      terminal.onResize(() => {
        redraw();
      }),
    ];
    terminal.attachCustomKeyEventHandler((event) => {
      if (event.type !== "keydown") return true;
      if (event.key === "Escape") {
        latest.current.onLeave();
        return false;
      }
      // Ctrl/Cmd+C with a selection copies it (the browser does that); without one it reaches the engine as Ctrl+C.
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "c" && terminal.hasSelection()) return false;
      return true;
    });

    let frame = 0;
    const observer = new ResizeObserver(() => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        try {
          fit.fit();
        } catch {
          // The element has no size yet (hidden or being laid out): the next resize fits it.
        }
      });
    });
    observer.observe(element);
    const themeWatcher = new MutationObserver(() => {
      applyTheme();
      redraw();
    });
    themeWatcher.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    const systemTheme = typeof window.matchMedia === "function" ? window.matchMedia("(prefers-color-scheme: light)") : null;
    const onSystemTheme = () => {
      applyTheme();
      redraw();
    };
    systemTheme?.addEventListener("change", onSystemTheme);

    try {
      fit.fit();
    } catch {
      // See above.
    }
    // Replay the whole transcript into the fresh terminal (the effect after this one draws the line).
    const replay = latest.current.entries;
    written = replay.reduce((max, entry) => Math.max(max, entry.id), 0);
    paint(replay.map((entry) => renderEntry(entry, palette.current)).join(""));
    terminal.focus();

    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
      themeWatcher.disconnect();
      systemTheme?.removeEventListener("change", onSystemTheme);
      for (const subscription of subscriptions) subscription.dispose();
      marker?.dispose();
      drawer.current = null;
      if (focusRef) focusRef.current = null;
      fit.dispose();
      terminal.dispose();
    };
  }, [label, focusRef]);

  useEffect(() => {
    drawer.current?.sync();
  });

  return <div className="terminal-surface" data-testid="terminal-surface">
    <div className="terminal-surface__viewport" ref={host} />
  </div>;
}
