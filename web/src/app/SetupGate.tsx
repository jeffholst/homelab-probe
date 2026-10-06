import { Navigate, Outlet, useLocation } from "react-router-dom";

import { ErrorState, Loading } from "../components/DataStates";
import { useMeta } from "./meta";

/** Read public metadata before mounting any route that asks for a session. */
export function SetupGate() {
  const meta = useMeta();
  const location = useLocation();
  const setupPath = location.pathname === "/setup" || location.pathname.startsWith("/setup/");
  if (meta.isPending) return <main className="main"><Loading label="the server's details" /></main>;
  if (meta.isError) return <main className="main"><ErrorState error={meta.error} label="the server's details" onRetry={() => void meta.refetch()} /></main>;
  if (meta.data.needs_setup && !setupPath) return <Navigate to="/setup" replace />;
  if (!meta.data.needs_setup && setupPath) return <Navigate to="/login" replace />;
  return <Outlet />;
}
