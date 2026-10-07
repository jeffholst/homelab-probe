import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createContext, useContext, type ReactNode } from "react";

import { createApi, type Api } from "../api/endpoints";
import { createApiClient, type ApiClient } from "../api/client";
import { createBackupApi, type BackupApi } from "../api/backup";
import { isApiError } from "../api/errors";
import { createSetupApi, type SetupApi } from "../api/setup";
import { ThemeProvider } from "../theme/ThemeProvider";

/** Query key of the logged-in user (a `Session`, or null when nobody is logged in). */
export const SESSION_KEY = ["session"] as const;

/**
 * Marks nobody as logged in and forgets everything that was read with the session, so the next person to log in on this
 * page cannot see the previous one's data (and, while it is fresh, cached data would not even be read again).
 */
export function endSession(queryClient: QueryClient): void {
  // The session itself and `meta` (what the server says about itself before anyone logs in) are not the session's data:
  // the login page needs `meta`, and the first 401 of a visit (`/auth/me`) arrives while it is being read.
  const sessionData = (query: { queryKey: readonly unknown[] }) => query.queryKey[0] !== SESSION_KEY[0] && query.queryKey[0] !== "meta";
  void queryClient.cancelQueries({ predicate: sessionData });
  queryClient.removeQueries({ predicate: sessionData });
  queryClient.setQueryData(SESSION_KEY, null);
}

export interface Services {
  client: ApiClient;
  api: Api;
  /** The guided setup and the restore of a backup (the setup token is given to each call). */
  setup: SetupApi;
  backup: BackupApi;
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
      endSession(queryClient);
    },
  });
  return { client, api: createApi(client), setup: createSetupApi(client), backup: createBackupApi(client), queryClient };
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
