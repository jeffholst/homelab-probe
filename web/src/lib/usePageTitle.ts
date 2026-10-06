import { useEffect } from "react";

export const APP_NAME = "Homelab Probe";

/** Sets the browser tab's title to "Page - Homelab Probe" while the page is shown (it names the page for a screen reader too). */
export function usePageTitle(title: string): void {
  useEffect(() => {
    document.title = `${title} - ${APP_NAME}`;
  }, [title]);
}
