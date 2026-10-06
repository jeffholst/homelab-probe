import { render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { App } from "../app/App";
import { Providers, createServices, type Services } from "../app/services";
import { FakeApi, type FakeApiOptions } from "./fakeApi";

export interface Rendered {
  fake: FakeApi;
  services: Services;
}

/** The whole app against a fake API, starting at `route`. */
export function renderApp(route = "/", options: FakeApiOptions & { fake?: FakeApi } = {}): Rendered & ReturnType<typeof render> {
  const fake = options.fake ?? new FakeApi(options);
  const services = createServices({ fetch: fake.fetch });
  // A test should not wait for a retry.
  services.queryClient.setDefaultOptions({ queries: { retry: false, staleTime: 15_000 } });
  const result = render(
    <Providers services={services}>
      <MemoryRouter initialEntries={[route]}>
        <App />
      </MemoryRouter>
    </Providers>,
  );
  return { ...result, fake, services };
}

/** Components that need only the providers, not the routes. */
export function renderWithProviders(ui: React.ReactNode, options: FakeApiOptions = {}) {
  const fake = new FakeApi(options);
  const services = createServices({ fetch: fake.fetch });
  services.queryClient.setDefaultOptions({ queries: { retry: false } });
  return { fake, services, ...render(<Providers services={services}>{ui}</Providers>) };
}
