import { useSyncExternalStore } from "react";

/** Whether a CSS media query matches now, and again whenever it changes. False where `matchMedia` does not exist. */
export function useMediaQuery(query: string): boolean {
  return useSyncExternalStore(
    (onChange) => {
      if (typeof window.matchMedia !== "function") return () => undefined;
      const list = window.matchMedia(query);
      list.addEventListener("change", onChange);
      return () => {
        list.removeEventListener("change", onChange);
      };
    },
    () => typeof window.matchMedia === "function" && window.matchMedia(query).matches,
    () => false,
  );
}

/** The breakpoint where the navigation stops being a drawer and becomes a sidebar (the same 56rem as app.css). */
export const DESKTOP_QUERY = "(min-width: 56rem)";
