import { useQuery } from "@tanstack/react-query";

import { useApi } from "./services";

/** What the server says about itself before anyone logs in: version, demo, read-only, HTTPS. Public, so it works on the login page. */
export function useMeta() {
  const api = useApi();
  return useQuery({ queryKey: ["meta"], queryFn: ({ signal }) => api.meta(signal), staleTime: 5 * 60_000 });
}
