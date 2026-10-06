import { useSession, useLogout } from "../auth/session";
import { ThemeSwitch } from "../components/ThemeSwitch";
import { Text } from "../components/Text";
import { formatDuration } from "../lib/format";
import { usePageTitle } from "../lib/usePageTitle";

/** Your account. Changing the password arrives with the profile API; until then this page says so. */
export function ProfilePage() {
  usePageTitle("Profile");
  const session = useSession();
  const logout = useLogout();
  const user = session.data;

  return (
    <>
      <h1>Profile</h1>
      {user && (
        <section className="card" aria-labelledby="account-heading">
          <h2 id="account-heading">Account</h2>
          <dl className="facts">
            <dt>User name</dt>
            <dd>
              <Text value={user.username} />
            </dd>
            <dt>Role</dt>
            <dd>{user.role === "admin" ? "Administrator" : "Viewer"}</dd>
            <dt>Session ends</dt>
            <dd>
              after {formatDuration(user.idle_seconds_left)} without activity, or in {formatDuration(user.session_seconds_left)} at the latest
            </dd>
          </dl>
        </section>
      )}
      <section className="card" aria-labelledby="appearance-heading">
        <h2 id="appearance-heading">Appearance</h2>
        <p className="muted">Light, dark, or follow your device. The choice is remembered in this browser only.</p>
        <ThemeSwitch />
      </section>
      <section className="card" aria-labelledby="password-heading">
        <h2 id="password-heading">Password</h2>
        <p>Changing your password here is not available yet. Ask an administrator, or use <code>hlp web-user</code> on the server.</p>
      </section>
      <button
        type="button"
        className="button"
        disabled={logout.isPending}
        onClick={() => {
          logout.mutate();
        }}
      >
        Log out
      </button>
    </>
  );
}
