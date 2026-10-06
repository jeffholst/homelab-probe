import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createContext, useContext, type ReactNode } from "react";

import { createApi, type Api } from "../api/endpoints";
import { createApiClient, type ApiClient } from "../api/client";
import { isApiError } from "../api/errors";
import { ThemeProvider } from "../theme/ThemeProvider";

/** Query key of the logged-in user (a `Session`, or null when nobody is logged in). */
export const SESSION_KEY = ["session"] as const;

export interface Services {
  client: ApiClient;
  api: Api;
  queryClient: QueryClient;
}

/** Retry a read once when the network or the server hiccuped; never retry an answer that says no. */
function shouldRetry(failureCount: number, error: unknown): boolean {
  if (failureCount >= 1) return false;
  return isApiError(error) && (error.status === 0 || error.status >= 500) && error.status !== 501;
}

/**
 * The API client, its calls and the query cache, wired together: a 401 anywhere marks nobody as logged in, which the
 * route guard turns into the login page. Tests pass the fake API's `fetch`.
 */
export function createServices(options: { fetch?: typeof fetch } = {}): Services {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: shouldRetry, staleTime: 15_000, refetchOnWindowFocus: true } },
  });
  const client = createApiClient({
    ...(options.fetch ? { fetch: options.fetch } : {}),
    onUnauthorized: () => {
      queryClient.setQueryData(SESSION_KEY, null);
    },
  });
  return { client, api: createApi(client), queryClient };
}

const ServicesContext = createContext<Services | null>(null);

export function Providers({ services, children }: { services: Services; children: ReactNode }) {
  return (
    <ServicesContext.Provider value={services}>
      <QueryClientProvider client={services.queryClient}>
        <ThemeProvider>{children}</ThemeProvider>
      </QueryClientProvider>
    </ServicesContext.Provider>
  );
}

export function useServices(): Services {
  const services = useContext(ServicesContext);
  if (services === null) throw new Error("useServices needs the Providers");
  return services;
}

export function useApi(): Api {
  return useServices().api;
}
