import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { type Session } from "../api/types";
import { SESSION_KEY, useApi, useServices } from "../app/services";

/** Who is logged in: a `Session`, `null` for nobody, `undefined` while the first answer is on its way. */
export function useSession() {
  const api = useApi();
  return useQuery({
    queryKey: SESSION_KEY,
    queryFn: ({ signal }) => api.me(signal),
    staleTime: 30_000,
    retry: false,
  });
}

export function useLogin() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ username, password }: { username: string; password: string }) => api.login(username, password),
    onSuccess: (session: Session) => {
      queryClient.setQueryData(SESSION_KEY, session);
    },
  });
}

/**
 * Ends the session on the server, then forgets everything that was read with it (the next person to use this browser
 * must not see the previous one's data in the cache) and the CSRF token. The user is logged out here even when the
 * server could not be told: the cookie is HttpOnly and cannot be dropped by script, but the session ends by itself.
 */
export function useLogout() {
  const { api, queryClient } = useServices();
  return useMutation({
    mutationFn: () => api.logout(),
    onSettled: () => {
      queryClient.removeQueries({ predicate: (query) => query.queryKey[0] !== SESSION_KEY[0] });
      queryClient.setQueryData(SESSION_KEY, null);
    },
  });
}

/** A path inside this app that is safe to go to after logging in; anything else (another site, `//host`) is "/". */
export function safeNext(value: string | null | undefined): string {
  if (!value || !value.startsWith("/") || value.startsWith("//") || value.includes("\\")) return "/";
  // eslint-disable-next-line no-control-regex -- a control character in a path is a reason to refuse it
  if (/[\u0000-\u001f\u007f]/.test(value)) return "/";
  if (value === "/login" || value.startsWith("/login?") || value.startsWith("/login/")) return "/";
  return value;
}
