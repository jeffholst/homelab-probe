import { useQuery } from "@tanstack/react-query";

import { useApi } from "../app/services";
import { useMeta } from "../app/meta";
import { useSession } from "../auth/session";
import { DataView } from "../components/DataView";
import { Text } from "../components/Text";
import { usePageTitle } from "../lib/usePageTitle";

/**
 * The first page after the login. It is deliberately small: who you are, what the server is, and the platforms it can
 * show (a real read, so the data states are exercised end to end). The dashboard and the other pages come later.
 */
export function HomePage() {
  usePageTitle("Home");
  const api = useApi();
  const session = useSession();
  const meta = useMeta();
  const platforms = useQuery({ queryKey: ["platforms"], queryFn: ({ signal }) => api.platforms(signal) });

  return (
    <>
      <h1>Home</h1>
      <p>
        Welcome{session.data ? <>, <Text value={session.data.username} /></> : ""}. This is the start of the Homelab Probe web
        interface; more pages arrive in later versions.
      </p>

      <section className="card" aria-labelledby="platforms-heading">
        <h2 id="platforms-heading">Platforms</h2>
        <DataView
          query={platforms}
          label="platforms"
          isEmpty={(items) => items.length === 0}
          empty={{ title: "No platforms", text: "The server does not report any platform." }}
        >
          {(items) => (
            <ul className="plain-list">
              {items.map((platform) => (
                <li key={platform.id}>
                  <Text value={platform.name} /> <span className="muted">{platform.configured ? "configured" : "not configured"}</span>
                </li>
              ))}
            </ul>
          )}
        </DataView>
        <p className="card-actions">
          <button type="button" className="button button--secondary" onClick={() => void platforms.refetch()}>
            Refresh
          </button>
        </p>
      </section>

      {meta.data && (
        <section className="card" aria-labelledby="server-heading">
          <h2 id="server-heading">This server</h2>
          <dl className="facts">
            <dt>Version</dt>
            <dd>
              <Text value={meta.data.version} />
            </dd>
            <dt>Data</dt>
            <dd>{meta.data.demo ? "Synthetic demo data" : "Your controller"}</dd>
            <dt>Mode</dt>
            <dd>{meta.data.read_only ? "Read-only server (it writes no files)" : "Normal"}</dd>
          </dl>
        </section>
      )}
    </>
  );
}
