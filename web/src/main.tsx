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
const services = createServices();

function render(terminal: TerminalServices | null) {
  root.render(
    <StrictMode>
      <Providers services={services}>
        <TerminalContext.Provider value={terminal}>
          <BrowserRouter>
            <App />
          </BrowserRouter>
        </TerminalContext.Provider>
      </Providers>
    </StrictMode>,
  );
}

// Both development gates default off. The mock takes precedence; normal builds have no terminal provider.
if (import.meta.env.VITE_TERMINAL_MOCK === "1") {
  void import("./terminal/mockServices").then((module) => { render(module.mockServices()); });
} else {
  // Explicit preview gate only. Production enablement awaits the CSP decision recorded in #277.
  if (import.meta.env.VITE_TERMINAL_API_PREVIEW === "1") {
    void Promise.all([import("./terminal/api"), import("./terminal/defaultEngine")]).then(([api, module]) => {
      render({ backend: api.createTerminalBackend(services.client), engine: module.defaultEngine });
    });
  } else render(null);
}
