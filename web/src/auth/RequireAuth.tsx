import { Navigate, Outlet, useLocation } from "react-router-dom";

import { useMeta } from "../app/meta";
import { ErrorState, Loading } from "../components/DataStates";
import { useSession } from "./session";

/**
 * The route guard: nobody logged in goes to the login page (remembering where they were going, as a path inside this
 * app only), and while the first answer is on its way, or if it could not be had, the page says so instead of
 * flashing a login form at someone who is logged in. The server enforces access; this only decides what to show.
 */
export function RequireAuth() {
  const session = useSession();
  const location = useLocation();
  const meta = useMeta();

  // A server waiting for its setup answers every page's data with 503: the setup is where to go.
  if (meta.data?.needs_setup === true) return <Navigate to="/setup" replace />;
  if (session.isPending || (meta.isPending && !session.data)) {
    return (
      <main className="main">
        <Loading label="your session" />
      </main>
    );
  }
  if (session.isError) {
    return (
      <main className="main">
        <ErrorState error={session.error} label="your session" onRetry={() => void session.refetch()} />
      </main>
    );
  }
  if (!session.data) {
    const target = `${location.pathname}${location.search}`;
    const search = target === "/" ? "" : `?next=${encodeURIComponent(target)}`;
    return <Navigate to={`/login${search}`} replace />;
  }
  return <Outlet />;
}
