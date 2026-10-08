import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import { App } from "./app/App";
import { Providers, createServices } from "./app/services";
import type { TerminalServices } from "./terminal/backend";
import { TerminalContext } from "./terminal/context";
import "./styles/tokens.css";
import "./styles/app.css";

const container = document.getElementById("root");
if (container === null) throw new Error("index.html has no #root element");
const root = createRoot(container);

function render(terminal: TerminalServices | null) {
  root.render(
    <StrictMode>
      <Providers services={createServices()}>
        <TerminalContext.Provider value={terminal}>
          <BrowserRouter>
            <App />
          </BrowserRouter>
        </TerminalContext.Provider>
      </Providers>
    </StrictMode>,
  );
}

// The terminal exists only in a build made with VITE_TERMINAL_MOCK=1 (development and the browser tests) until the real
// API is connected (#277). A normal build replaces this condition with `false` and leaves the mock out of the bundle.
if (import.meta.env.VITE_TERMINAL_MOCK === "1") {
  void import("./terminal/mockServices").then((module) => { render(module.mockServices()); });
} else {
  render(null);
}
