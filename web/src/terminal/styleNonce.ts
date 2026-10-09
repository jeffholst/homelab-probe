/** xterm 6.0 compatibility adapter. Only xterm receives this document; global DOM methods stay unchanged. */
export function terminalDocument(document: Document): Document | undefined {
  if (import.meta.env.VITE_TERMINAL_API_PREVIEW !== "1") return undefined;
  const nonce = document.querySelector<HTMLScriptElement>("script[data-terminal-style-nonce]")?.nonce;
  if (!nonce || !/^[A-Za-z0-9_-]{32}$/.test(nonce)) return undefined;
  return new Proxy(document, {
    get(target, key) {
      if (key === "createElement") return (tag: string, options?: ElementCreationOptions) => {
        const element = target.createElement(tag, options);
        if (element instanceof HTMLStyleElement) element.nonce = nonce;
        if (tag.toLowerCase() === "div") {
          const append = element.appendChild.bind(element);
          element.appendChild = <T extends Node>(child: T): T => {
            // The 6.0 scrollbar creates its style through the global document, bypassing documentOverride.
            if (element.classList.contains("xterm-screen") && child instanceof HTMLStyleElement) child.nonce = nonce;
            return append(child);
          };
        }
        return element;
      };
      const value: unknown = Reflect.get(target, key, target);
      const bound: unknown = typeof value === "function" ? value.bind(target) : value;
      return bound;
    },
  });
}
