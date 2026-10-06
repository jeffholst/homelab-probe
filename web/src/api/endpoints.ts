/** The calls this version of the app makes. A page issue adds its own below, next to these. */
import { LOGIN_PATH, type ApiClient } from "./client";
import { isApiError } from "./errors";
import { parseMeta, parsePlatforms, parseSession, type Meta, type Platform, type Session } from "./types";

export interface Api {
  meta(signal?: AbortSignal): Promise<Meta>;
  /** The logged-in user, or null when nobody is (the 401 is an answer here, not a failure). */
  me(signal?: AbortSignal): Promise<Session | null>;
  login(username: string, password: string): Promise<Session>;
  logout(): Promise<void>;
  platforms(signal?: AbortSignal): Promise<Platform[]>;
}

export function createApi(client: ApiClient): Api {
  function remember(value: unknown): Session {
    const { session, csrfToken } = parseSession(value);
    client.setCsrfToken(csrfToken);
    return session;
  }

  return {
    meta: (signal) => client.get("/meta", { signal, parse: parseMeta }),
    async me(signal) {
      try {
        return await client.get("/auth/me", { signal, parse: remember });
      } catch (error) {
        if (isApiError(error) && error.status === 401) return null;
        throw error;
      }
    },
    login: (username, password) => client.post(LOGIN_PATH, { body: { username, password }, parse: remember }),
    async logout() {
      try {
        await client.post("/auth/logout");
      } finally {
        client.clearCsrfToken();
      }
    },
    platforms: (signal) => client.get("/platforms", { signal, parse: parsePlatforms }),
  };
}
