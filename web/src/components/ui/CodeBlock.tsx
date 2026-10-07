import { useEffect, useState } from "react";

import { Icon } from "./Icon";

/**
 * A file to copy or save: its text as it is (in a `<pre>`, so it is shown, never parsed), a copy button and a download
 * button. Only for text that holds no secret (the server puts placeholders where the secrets go). Copying needs the
 * clipboard, which a browser offers on HTTPS and on this machine only; elsewhere the button says to select the text.
 */
export function CodeBlock({ title, text, fileName, mediaType = "text/plain" }: { title: string; text: string; fileName: string; mediaType?: string }) {
  const [copied, setCopied] = useState<"yes" | "no" | null>(null);
  useEffect(() => {
    if (copied === null) return;
    const timer = window.setTimeout(() => {
      setCopied(null);
    }, 4000);
    return () => {
      window.clearTimeout(timer);
    };
  }, [copied]);

  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopied("yes");
    } catch {
      setCopied("no");
    }
  }

  function download() {
    const url = URL.createObjectURL(new Blob([text], { type: mediaType }));
    const link = document.createElement("a");
    link.href = url;
    link.download = fileName;
    link.click();
    window.setTimeout(() => {
      URL.revokeObjectURL(url);
    }, 0);
  }

  return (
    <figure className="codeblock" aria-label={title}>
      <figcaption className="codeblock__bar">
        <span className="codeblock__title">{title}</span>
        <span className="row">
          <button type="button" className="button button--ghost button--small" aria-label={`Copy ${title}`} onClick={() => void copy()}>
            <Icon name={copied === "yes" ? "check" : "copy"} />
            Copy
          </button>
          <button type="button" className="button button--ghost button--small" aria-label={`Download ${title}`} onClick={download}>
            <Icon name="download" />
            Download
          </button>
        </span>
      </figcaption>
      {/* A long line scrolls sideways, and a scrolling region has to be reachable by keyboard (axe: scrollable-region-focusable). */}
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex */}
      <pre tabIndex={0} aria-label={title}>
        {text}
      </pre>
      <p className="visually-hidden" role="status">
        {copied === "yes" ? `${title} copied.` : copied === "no" ? "Copying is not available here: select the text and copy it." : ""}
      </p>
      {copied === "no" && <p className="hint codeblock__note">Copying is not available here: select the text and copy it.</p>}
    </figure>
  );
}
