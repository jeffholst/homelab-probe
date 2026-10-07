import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { FakeApi, type FakeAccount } from "../test/fakeApi";
import {
  BACKUP_CONTENT,
  BACKUP_PASSPHRASE,
  FINGERPRINT,
  FakeSetup,
  SETUP_TOKEN,
  UNVERIFIED_PHRASE,
  type FakeSetupOptions,
} from "../test/fakeSetup";
import { renderApp } from "../test/render";

type User = ReturnType<typeof userEvent.setup>;
const TYPED_KEY = "a-very-secret-api-key-123";

function setupApp(options: FakeSetupOptions & { accounts?: FakeAccount[]; route?: string } = {}) {
  const fake = new FakeApi({ accounts: options.accounts ?? [] });
  const setup = new FakeSetup(fake, options);
  const user = userEvent.setup();
  const rendered = renderApp(options.route ?? "/setup", { fake });
  return { setup, user, ...rendered };
}

async function enterToken(user: User, token = SETUP_TOKEN) {
  await user.type(await screen.findByLabelText("Setup token"), token);
  await user.click(screen.getByRole("button", { name: "Start the setup" }));
}

async function startConfigure(user: User) {
  await enterToken(user);
  await user.click(await screen.findByRole("button", { name: /Set up a new installation/ }));
}

async function controller(user: User, url = "https://192.168.1.1", key = TYPED_KEY) {
  await user.type(await screen.findByLabelText("Controller address"), url);
  if (key !== "") await user.type(screen.getByLabelText("API key"), key);
  await user.click(screen.getByRole("button", { name: "Continue" }));
}

async function pin(user: User) {
  await screen.findByRole("heading", { name: "Recognise the controller" });
  await user.click(screen.getByRole("button", { name: "Fetch the certificate" }));
  await user.click(await screen.findByLabelText("I compared the fingerprint and it is my controller's"));
  await user.click(screen.getByRole("button", { name: "Save and continue" }));
}

async function testConnection(user: User) {
  await screen.findByRole("heading", { name: "Test the connection" });
  await user.click(screen.getByRole("button", { name: /^(Test the connection|Test again)$/ }));
  await screen.findByText("Connected");
}

/** Everything a page can keep: nothing of a secret may be in it. */
function stored(): string {
  return JSON.stringify([Object.entries(window.localStorage), Object.entries(window.sessionStorage), document.cookie, window.location.href]);
}

describe("the setup token", () => {
  it("is asked for first, and a wrong one is refused in the server's words", async () => {
    const { user } = setupApp();
    expect(await screen.findByRole("heading", { name: "Welcome to Homelab Probe" })).toBeInTheDocument();
    await enterToken(user, "not-the-token");
    expect(await screen.findByText("The setup token is missing or wrong.")).toBeInTheDocument();
    expect(screen.getByLabelText("Setup token")).toHaveAttribute("aria-invalid", "true");
  });

  it("is slowed down after repeated failures, with a countdown on the button", async () => {
    const { user } = setupApp();
    for (let attempt = 0; attempt < 5; attempt += 1) {
      await user.clear(await screen.findByLabelText("Setup token"));
      await enterToken(user, `wrong-${attempt}`);
      await screen.findAllByText(/missing or wrong|Too many attempts/);
    }
    expect(await screen.findByText("Too many attempts")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Wait \d+ s/ })).toBeDisabled();
  });

  it("opens the choice, and is kept in memory only: not in storage, a cookie or the address", async () => {
    const { user } = setupApp();
    await enterToken(user);
    expect(await screen.findByRole("heading", { name: "How would you like to start?" })).toBeInTheDocument();
    expect(stored()).not.toContain(SETUP_TOKEN);
  });

  it("can be shown to check what was typed", async () => {
    const { user } = setupApp();
    const field = await screen.findByLabelText("Setup token");
    expect(field).toHaveAttribute("type", "password");
    await user.click(screen.getByRole("button", { name: "Show" }));
    expect(field).toHaveAttribute("type", "text");
    await user.click(screen.getByRole("button", { name: "Hide" }));
    expect(field).toHaveAttribute("type", "password");
  });
});

describe("configuring a new installation", () => {
  it("goes from the controller to the administrator and then to the login, which the new account opens", async () => {
    const { user, setup } = setupApp();
    await startConfigure(user);
    expect(screen.getByRole("list", { name: "Setup progress" })).toBeInTheDocument();

    await controller(user);
    expect(setup.draft.apiKey).toBe(TYPED_KEY);
    await pin(user);
    expect(setup.draft.verify).toBe("pin");

    await testConnection(user);
    await user.click(screen.getByRole("radio", { name: /Cabin/ }));
    expect(await screen.findByText("The site changed")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Continue" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Test again" }));
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Continue" })).toBeEnabled();
    });
    expect(setup.draft.site).toBe("cabin");
    await user.click(screen.getByRole("button", { name: "Continue" }));

    await screen.findByRole("heading", { name: "Notifications" });
    await user.click(screen.getByRole("radio", { name: /^ntfy/ }));
    await user.type(screen.getByLabelText("Topic address"), "https://ntfy.example/alerts");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Saved on the server")).toBeInTheDocument();
    expect(screen.getByLabelText("Topic address")).toHaveValue("");
    await user.click(screen.getByRole("button", { name: "Check without sending" }));
    expect(await screen.findByRole("list", { name: "Notification check" })).toHaveTextContent("configured");
    await user.click(screen.getByRole("button", { name: "Continue" }));

    await screen.findByRole("heading", { name: "A first health check" });
    await user.click(screen.getByRole("button", { name: "Run the checks" }));
    const counts = await screen.findByRole("list", { name: "What was found" });
    expect(within(counts).getByText("Critical").previousSibling).toHaveTextContent("1");
    expect(screen.getByText("Garage switch")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Continue" }));

    await screen.findByRole("heading", { name: "Create the administrator" });
    await user.type(screen.getByLabelText("User name"), "owner");
    await user.type(screen.getByLabelText("Password"), "a long enough password");
    await user.type(screen.getByLabelText("Password again"), "a long enough password");
    await user.click(screen.getByRole("button", { name: "Finish setup" }));
    expect(await screen.findByRole("heading", { name: "You're all set" })).toHaveFocus();

    await user.click(screen.getByRole("button", { name: "Go to the login" }));
    await user.type(await screen.findByLabelText("User name"), "owner");
    await user.type(screen.getByLabelText("Password"), "a long enough password");
    await user.click(screen.getByRole("button", { name: "Log in" }));
    expect(await screen.findByRole("heading", { name: "Home" })).toBeInTheDocument();
  });

  it("never shows the API key again, nor keeps it anywhere but the server's draft", async () => {
    const { user } = setupApp();
    await startConfigure(user);
    await controller(user);
    await screen.findByRole("heading", { name: "Recognise the controller" });
    await user.click(screen.getByRole("button", { name: "Back" }));
    const key = await screen.findByLabelText("API key");
    expect(key).toHaveValue("");
    expect(screen.getByText(/A key is saved on the server already/)).toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain(TYPED_KEY);
    expect(stored()).not.toContain(TYPED_KEY);
  });

  it("puts the server's refusal of a value under its field, and asks for what is missing", async () => {
    const { user } = setupApp();
    await startConfigure(user);
    await user.click(await screen.findByRole("button", { name: "Continue" }));
    expect(screen.getByText("Enter the controller's address.")).toBeInTheDocument();
    await controller(user, "ftp://controller", "");
    expect(screen.getByText("Paste the API key.")).toBeInTheDocument();
    await user.type(screen.getByLabelText("API key"), TYPED_KEY);
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByText("UNIFI_URL must be an https:// address.")).toBeInTheDocument();
    expect(screen.getByLabelText("Controller address")).toHaveAttribute("aria-invalid", "true");
  });

  it("does not pin a certificate before the fingerprint was compared", async () => {
    const { user, setup } = setupApp();
    await startConfigure(user);
    await controller(user);
    await user.click(await screen.findByRole("button", { name: "Fetch the certificate" }));
    expect(await screen.findByText(FINGERPRINT.split(":").slice(0, 4).join(":"), { exact: false })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save and continue" }));
    expect(screen.getByText("Compare the fingerprint first, then tick the box.")).toBeInTheDocument();
    expect(setup.draft.verify).toBe("true");
  });

  it("says why a certificate cannot be pinned, and does not offer to", async () => {
    const { user } = setupApp({ certificateProblem: "hostname_mismatch" });
    await startConfigure(user);
    await controller(user);
    await user.click(await screen.findByRole("button", { name: "Fetch the certificate" }));
    expect(await screen.findByText("This certificate cannot be pinned")).toBeInTheDocument();
    expect(screen.getByText("The certificate is not valid for this address.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save and continue" })).toBeDisabled();
  });

  it("turns certificate checking off only with the sentence typed exactly", async () => {
    const { user, setup } = setupApp();
    await startConfigure(user);
    await controller(user);
    await user.click(await screen.findByRole("radio", { name: /Do not check the certificate/ }));
    await user.type(screen.getByLabelText(/to confirm/), "send my key");
    await user.click(screen.getByRole("button", { name: "Save and continue" }));
    expect(await screen.findByText(/needs the confirmation sentence, typed exactly/)).toBeInTheDocument();
    expect(setup.draft.verify).toBe("true");
    await user.clear(screen.getByLabelText(/to confirm/));
    await user.type(screen.getByLabelText(/to confirm/), UNVERIFIED_PHRASE);
    await user.click(screen.getByRole("button", { name: "Save and continue" }));
    expect(await screen.findByRole("heading", { name: "Test the connection" })).toBeInTheDocument();
    expect(setup.draft.verify).toBe("false");
  });

  it("can trust the system's authorities instead", async () => {
    const { user, setup } = setupApp();
    await startConfigure(user);
    await controller(user);
    await user.click(await screen.findByRole("radio", { name: /from a trusted authority/ }));
    await user.click(screen.getByRole("button", { name: "Save and continue" }));
    await screen.findByRole("heading", { name: "Test the connection" });
    expect(setup.bodies.at(-1)?.body).toEqual({ verify: "true" });
  });

  it("shows a failed connection test and does not go on", async () => {
    const { user } = setupApp({ connectionOk: false });
    await startConfigure(user);
    await controller(user);
    await pin(user);
    await user.click(await screen.findByRole("button", { name: "Test the connection" }));
    expect(await screen.findByText("The controller could not be used")).toBeInTheDocument();
    const checks = screen.getByRole("list", { name: "What was checked" });
    expect(checks).toHaveTextContent("Failed: API key");
    expect(checks).toHaveTextContent("Create a new key and paste it again.");
    expect(screen.getByRole("button", { name: "Continue" })).toBeDisabled();
  });

  it("removes a notification destination again", async () => {
    const { user, setup } = setupApp();
    setup.draft = { ...setup.draft, url: "https://192.168.1.1", apiKey: TYPED_KEY, connectionOk: true, notify: { NOTIFY_NTFY_URL: "https://ntfy.example/x" } };
    await startConfigure(user);
    for (const step of ["Continue", "Save and continue"]) {
      await user.click(await screen.findByRole("button", { name: step }));
      if (step === "Continue") await screen.findByRole("heading", { name: "Recognise the controller" });
    }
    await user.click(await screen.findByRole("radio", { name: /Trust this controller's own certificate/ }));
    await user.click(screen.getByRole("radio", { name: /from a trusted authority/ }));
    await user.click(screen.getByRole("button", { name: "Save and continue" }));
    await testConnection(user);
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByRole("radio", { name: /^ntfy/ })).toBeChecked();
    expect(screen.getByText(/Saved on the server: leave empty to keep it/)).toBeInTheDocument();
    await user.click(screen.getByRole("radio", { name: /No notifications/ }));
    await user.click(screen.getByRole("button", { name: "Save" }));
    await screen.findByText("No notifications will be sent.");
    expect(setup.draft.notify).toEqual({});
  });

  it("asks for a new connection test when the server says the draft was not tested", async () => {
    const { user, setup } = setupApp();
    await startConfigure(user);
    await controller(user);
    await pin(user);
    await testConnection(user);
    await user.click(screen.getByRole("button", { name: "Continue" }));
    await user.click(await screen.findByRole("button", { name: "Continue" }));
    await user.click(await screen.findByRole("button", { name: "Skip" }));
    setup.draft = { ...setup.draft, connectionOk: null }; // changed elsewhere meanwhile
    await user.type(await screen.findByLabelText("User name"), "owner");
    await user.type(screen.getByLabelText("Password"), "a long enough password");
    await user.type(screen.getByLabelText("Password again"), "a long enough password");
    await user.click(screen.getByRole("button", { name: "Finish setup" }));
    expect(await screen.findByText("Test the connection with these settings first.")).toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toHaveValue("");
    await user.click(screen.getByRole("button", { name: "Test the connection again" }));
    expect(await screen.findByRole("heading", { name: "Test the connection" })).toBeInTheDocument();
  });

  it("checks the administrator before sending it, and shows the server's refusal under the name", async () => {
    const { user, setup } = setupApp();
    setup.draft = { ...setup.draft, url: "https://192.168.1.1", apiKey: TYPED_KEY, connectionOk: true };
    await startConfigure(user);
    // Straight to the last step: Continue through the steps that are already done.
    await user.click(await screen.findByRole("button", { name: "Continue" }));
    await user.click(await screen.findByRole("radio", { name: /from a trusted authority/ }));
    await user.click(screen.getByRole("button", { name: "Save and continue" }));
    await testConnection(user);
    await user.click(screen.getByRole("button", { name: "Continue" }));
    await user.click(await screen.findByRole("button", { name: "Continue" }));
    await user.click(await screen.findByRole("button", { name: "Skip" }));
    await screen.findByRole("heading", { name: "Create the administrator" });

    await user.type(screen.getByLabelText("User name"), "-x");
    await user.click(screen.getByRole("button", { name: "Finish setup" }));
    expect(screen.getByText(/starting with a letter or a digit/)).toBeInTheDocument();
    await user.clear(screen.getByLabelText("User name"));
    await user.type(screen.getByLabelText("User name"), "taken");
    await user.type(screen.getByLabelText("Password"), "short");
    await user.click(screen.getByRole("button", { name: "Finish setup" }));
    expect(screen.getByText("Use 12 to 1024 characters.")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Password"), " but now long enough");
    await user.type(screen.getByLabelText("Password again"), "something else entirely");
    await user.click(screen.getByRole("button", { name: "Finish setup" }));
    expect(screen.getByText("The two passwords are not the same.")).toBeInTheDocument();
    await user.clear(screen.getByLabelText("Password again"));
    await user.type(screen.getByLabelText("Password again"), "short but now long enough");
    await user.click(screen.getByRole("button", { name: "Finish setup" }));
    expect(await screen.findByText("That user name is taken.")).toBeInTheDocument();
    expect(screen.getByLabelText("User name")).toHaveAttribute("aria-invalid", "true");
  });

  it("gives back the files to save by hand, without a secret, when the server cannot save them", async () => {
    const { user } = setupApp({ fallback: "environment" });
    await startConfigure(user);
    await controller(user);
    await pin(user);
    await testConnection(user);
    await user.click(screen.getByRole("button", { name: "Continue" }));
    await user.click(await screen.findByRole("button", { name: "Continue" }));
    await user.click(await screen.findByRole("button", { name: "Skip" }));
    await user.type(await screen.findByLabelText("User name"), "owner");
    await user.type(screen.getByLabelText("Password"), "a long enough password");
    await user.type(screen.getByLabelText("Password again"), "a long enough password");
    await user.click(screen.getByRole("button", { name: "Finish setup" }));
    expect(await screen.findByRole("heading", { name: "Save the settings yourself" })).toBeInTheDocument();
    expect(screen.getByText(/also set in the server's environment/)).toHaveTextContent("UNIFI_URL");
    expect(screen.getByRole("figure", { name: ".env" })).toHaveTextContent("UNIFI_API_KEY=your-api-key-here");
    expect(screen.getByRole("figure", { name: "controller.pem" })).toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain(TYPED_KEY);
    expect(document.body.innerHTML).not.toContain("a long enough password");
  });
});

describe("the other ways in", () => {
  it("offers only the administrator on a server that has its settings", async () => {
    const { user, fake } = setupApp({ mode: "admin" });
    await enterToken(user);
    expect(await screen.findByRole("heading", { name: "One step left" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Create the first administrator/ }));
    expect(await screen.findByRole("heading", { name: "Create the administrator" })).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Setup progress" })).toBeNull();
    await user.type(screen.getByLabelText("User name"), "owner");
    await user.type(screen.getByLabelText("Password"), "a long enough password");
    await user.type(screen.getByLabelText("Password again"), "a long enough password");
    await user.click(screen.getByRole("button", { name: "Finish setup" }));
    expect(await screen.findByRole("heading", { name: "You're all set" })).toBeInTheDocument();
    expect(fake.hasAdministrator()).toBe(true);
    expect(fake.meta.needs_setup).toBe(false);
  });

  it("sends somebody to the login when an administrator exists, and the login lets them in for the setup", async () => {
    const { user, setup, fake } = setupApp({ accounts: [{ username: "admin", password: "admin password", role: "admin" }] });
    await enterToken(user);
    expect(await screen.findByRole("heading", { name: "Log in to continue the setup" })).toBeInTheDocument();
    const before = fake.calls.length;
    await user.click(screen.getByRole("link", { name: "Log in" }));
    await user.type(await screen.findByLabelText("User name"), "admin");
    await user.type(screen.getByLabelText("Password"), "admin password");
    await user.click(screen.getByRole("button", { name: "Log in" }));
    expect(await screen.findByRole("heading", { name: "How would you like to start?" })).toBeInTheDocument();
    expect(fake.calls.slice(before).some((call) => call.path === "/setup/status")).toBe(true);
    await user.click(screen.getByRole("button", { name: /Set up a new installation/ }));
    await controller(user);
    expect(setup.draft.url).toBe("https://192.168.1.1"); // with the session and its CSRF token, no setup token
  });

  it("says when the server is set up already", async () => {
    const fake = new FakeApi();
    renderApp("/setup", { fake });
    expect(await screen.findByRole("heading", { name: "This server is set up" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Go to the login" })).toHaveAttribute("href", "/login");
  });

  it("asks for nothing before the token is typed: a request without it would count as a failed attempt", async () => {
    const { fake } = setupApp();
    await screen.findByLabelText("Setup token");
    expect(fake.calls.filter((call) => call.path.startsWith("/setup"))).toEqual([]);
  });

  it("shows a failure to ask the server for a logged-in administrator, with a way to try again", async () => {
    const { user, fake } = setupApp({ accounts: [{ username: "admin", password: "admin password", role: "admin" }], route: "/login?next=%2Fsetup" });
    fake.failWith("/setup/status", 500, "accounts_unreadable", "The accounts file cannot be read; see the server log.");
    await user.type(await screen.findByLabelText("User name"), "admin");
    await user.type(screen.getByLabelText("Password"), "admin password");
    await user.click(screen.getByRole("button", { name: "Log in" }));
    expect(await screen.findByText("The accounts file cannot be read; see the server log.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("heading", { name: "How would you like to start?" })).toBeInTheDocument();
  });

  it("shows a token check that failed for another reason", async () => {
    const { user, fake } = setupApp();
    fake.failWith("/setup/status", 500, "accounts_unreadable", "The accounts file cannot be read; see the server log.");
    await enterToken(user);
    expect(await screen.findByText("Could not check the token")).toBeInTheDocument();
  });
});

describe("restoring a backup on a fresh installation", () => {
  const backupFile = (content = BACKUP_CONTENT) => new File([content], "home.hlpbackup", { type: "application/octet-stream" });

  async function openBackup(user: User, passphrase = BACKUP_PASSPHRASE, file = backupFile()) {
    await enterToken(user);
    await user.click(await screen.findByRole("button", { name: /Restore from a backup/ }));
    await user.upload(await screen.findByLabelText(/Choose a backup file/), file);
    await user.type(screen.getByLabelText("Passphrase of the backup"), passphrase);
    await user.click(screen.getByRole("button", { name: "Open the backup" }));
  }

  it("shows what a restore would do, then restores, and the backup's accounts log in", async () => {
    const evil = "<b>x</b>‮";
    const { user, fake } = setupApp({ backupAccounts: [{ username: evil, password: "restored password", role: "admin" }] });
    await openBackup(user);
    expect(await screen.findByRole("heading", { name: "What the restore would do" })).toBeInTheDocument();
    expect(screen.getByText("Web accounts")).toBeInTheDocument();
    expect(screen.getByText("3 notes, 2 triage entries, 1 site")).toBeInTheDocument();
    expect(screen.getByText("<b>x</b>")).toBeInTheDocument(); // as text, the override removed
    expect(document.querySelector("b")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Continue" }));

    await user.click(await screen.findByRole("button", { name: "Restore now" }));
    expect(screen.getByText("Tick the box to confirm.")).toBeInTheDocument();
    await user.click(screen.getByLabelText(/I understand that the accounts and passwords/));
    await user.click(screen.getByRole("button", { name: "Restore now" }));
    expect(await screen.findByRole("heading", { name: "Backup restored" })).toBeInTheDocument();
    expect(screen.getByText(/Every session has ended/)).toBeInTheDocument();
    expect(fake.meta.needs_setup).toBe(false);
    expect(stored()).not.toContain(BACKUP_PASSPHRASE);

    await user.click(screen.getByRole("button", { name: "Go to the login" }));
    expect(await screen.findByRole("heading", { name: "Log in" })).toBeInTheDocument();
  });

  it("asks for a recovery passphrase, twice, when there is a present state to keep", async () => {
    const { user, setup } = setupApp({ recoveryRequired: true });
    await openBackup(user);
    await user.click(await screen.findByRole("button", { name: "Continue" }));
    expect(await screen.findByText("A recovery backup comes first")).toBeInTheDocument();
    await user.click(screen.getByLabelText(/I understand that the accounts and passwords/));
    await user.type(screen.getByLabelText("Passphrase for the recovery backup"), "short");
    await user.click(screen.getByRole("button", { name: "Restore now" }));
    expect(screen.getByText("Use at least 12 characters.")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Passphrase for the recovery backup"), " and long enough now");
    await user.type(screen.getByLabelText("Passphrase again"), "something different");
    await user.click(screen.getByRole("button", { name: "Restore now" }));
    expect(screen.getByText("The two passphrases are not the same.")).toBeInTheDocument();
    await user.clear(screen.getByLabelText("Passphrase again"));
    await user.type(screen.getByLabelText("Passphrase again"), "short and long enough now");
    await user.click(screen.getByRole("button", { name: "Restore now" }));
    expect(await screen.findByText(/recovery-20261006-101500Z\.hlpbackup/)).toBeInTheDocument();
    expect(setup.bodies.at(-1)?.body).toMatchObject({ confirm: true, recovery_passphrase: "short and long enough now", recovery_confirm: "short and long enough now" });
  });

  it("puts a wrong passphrase under its field and refuses a file that is not a backup", async () => {
    const { user } = setupApp();
    await openBackup(user, "not the passphrase");
    expect(await screen.findByText("The passphrase is wrong, or the backup was modified or damaged.")).toBeInTheDocument();
    expect(screen.getByLabelText("Passphrase of the backup")).toHaveAttribute("aria-invalid", "true");
    await user.upload(screen.getByLabelText(/home\.hlpbackup/), backupFile("something else"));
    await user.clear(screen.getByLabelText("Passphrase of the backup"));
    await user.type(screen.getByLabelText("Passphrase of the backup"), BACKUP_PASSPHRASE);
    await user.click(screen.getByRole("button", { name: "Open the backup" }));
    expect(await screen.findByText("This is not a Homelab Probe backup.")).toBeInTheDocument();
  });

  it("asks for the file and the passphrase, and refuses a file larger than the server opens", async () => {
    const { user } = setupApp();
    await enterToken(user);
    await user.click(await screen.findByRole("button", { name: /Restore from a backup/ }));
    await user.click(await screen.findByRole("button", { name: "Open the backup" }));
    expect(screen.getByText("Choose a backup file.")).toBeInTheDocument();
    expect(screen.getByText("Enter the passphrase of the backup.")).toBeInTheDocument();
    const huge = backupFile();
    Object.defineProperty(huge, "size", { value: 65 * 1024 * 1024 });
    await user.upload(screen.getByLabelText(/Choose a backup file/), huge);
    expect(screen.getByText(/a backup can be at most 64\.0 MB/)).toBeInTheDocument();
  });

  it("goes back to the choice, and forgets the opened backup when going back from the review", async () => {
    const { user } = setupApp();
    await openBackup(user);
    await user.click(await screen.findByRole("button", { name: "Back" }));
    expect(await screen.findByLabelText("Passphrase of the backup")).toHaveValue("");
    await user.click(screen.getByRole("button", { name: "Back" }));
    expect(await screen.findByRole("heading", { name: "How would you like to start?" })).toBeInTheDocument();
  });
});

describe("when the server changes under the page", () => {
  it("asks for the token again when the server stops accepting it (a restart makes a new one)", async () => {
    const { user, fake } = setupApp();
    await startConfigure(user);
    fake.setupToken = "a-new-token-after-a-restart-000";
    await controller(user);
    expect(await screen.findByText("Enter the setup token again")).toBeInTheDocument();
    expect(screen.getByText(/the server may have restarted/)).toBeInTheDocument();
    expect(screen.getByLabelText("Setup token")).toHaveValue("");
    await enterToken(user, "a-new-token-after-a-restart-000");
    expect(await screen.findByRole("heading", { name: "How would you like to start?" })).toBeInTheDocument();
  });

  it("sends the page to the login when an administrator was created elsewhere meanwhile", async () => {
    const { user, fake } = setupApp();
    await startConfigure(user);
    fake.addAccount({ username: "elsewhere", password: "elsewhere password", role: "admin" });
    await controller(user);
    expect(await screen.findByRole("heading", { name: "Log in to continue the setup" })).toBeInTheDocument();
  });

  it("warns that the token, key and passwords would cross the network unencrypted", async () => {
    const fake = new FakeApi({ accounts: [], meta: { https: false, loopback: false } });
    new FakeSetup(fake);
    renderApp("/setup", { fake });
    expect(await screen.findByText("This connection is not encrypted")).toBeInTheDocument();
    expect(screen.getByText(/The setup token, the API key and the passwords/)).toBeInTheDocument();
  });

  it("says a finish without an answer may have happened, and checks the server instead of trying again", async () => {
    const { user, fake, setup } = setupApp({ mode: "admin" });
    await enterToken(user);
    await user.click(await screen.findByRole("button", { name: /Create the first administrator/ }));
    await user.type(await screen.findByLabelText("User name"), "owner");
    await user.type(screen.getByLabelText("Password"), "a long enough password");
    await user.type(screen.getByLabelText("Password again"), "a long enough password");
    fake.failWith("/setup/finish", 504, "bad_gateway", "The proxy timed out.");
    await user.click(screen.getByRole("button", { name: "Finish setup" }));
    expect(await screen.findByText("It is not known whether the setup finished")).toBeInTheDocument();
    expect(setup.bodies.filter((entry) => entry.path === "/setup/finish")).toHaveLength(0); // refused before the route
    await user.click(screen.getByRole("button", { name: "Check the server" }));
    expect(await screen.findByText(/still waiting for its setup/)).toBeInTheDocument();
    fake.meta.needs_setup = false;
    await user.click(screen.getByRole("button", { name: "Check the server" }));
    expect(await screen.findByText(/most likely finished/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Go to the login" })).toHaveAttribute("href", "/login");
  });

  it("says plainly that a read-only server does not save the setup", async () => {
    const { user, fake } = setupApp({ mode: "admin" });
    await enterToken(user);
    await user.click(await screen.findByRole("button", { name: /Create the first administrator/ }));
    await user.type(await screen.findByLabelText("User name"), "owner");
    await user.type(screen.getByLabelText("Password"), "a long enough password");
    await user.type(screen.getByLabelText("Password again"), "a long enough password");
    fake.failWith("/setup/finish", 403, "read_only", "This server is read-only: it writes no file.");
    await user.click(screen.getByRole("button", { name: "Finish setup" }));
    expect(await screen.findByText("This server is read-only")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "You're all set" })).toBeNull();
  });
});

describe("restore failures", () => {
  const file = () => new File([BACKUP_CONTENT], "home.hlpbackup");

  async function reachConfirm(user: User) {
    await enterToken(user);
    await user.click(await screen.findByRole("button", { name: /Restore from a backup/ }));
    await user.upload(await screen.findByLabelText(/Choose a backup file/), file());
    await user.type(screen.getByLabelText("Passphrase of the backup"), BACKUP_PASSPHRASE);
    await user.click(screen.getByRole("button", { name: "Open the backup" }));
    await user.click(await screen.findByRole("button", { name: "Continue" }));
    await user.click(await screen.findByLabelText(/I understand that the accounts and passwords/));
  }

  it.each([
    [503, "backup_busy", "Another backup is being made or read; try again in a moment.", "The server is busy", false],
    [403, "read_only", "This server is read-only: it writes no file.", "This server does not restore backups", false],
    [403, "demo", "A demo does not restore backups.", "This server does not restore backups", false],
    [500, "backup_recovery_failed", "The recovery backup of the present state could not be made, so nothing was changed.", "Nothing was changed", false],
    [500, "backup_restore_failed", "The restore did not complete; the previous state was put back.", "The backup was not restored", false],
    [500, "backup_restore_pending", "The restore did not complete and could not be undone yet.", "The restore did not complete", true],
    [500, "reload_failed", "The backup was restored but its settings could not be loaded: restart the server.", "Restored, but the server needs a restart", true],
  ])("tells %s %s apart, in the server's words", async (status, code, message, title, restart) => {
    const { user, fake } = setupApp();
    await reachConfirm(user);
    fake.failWith("/backup/restore", status, code, message);
    await user.click(screen.getByRole("button", { name: "Restore now" }));
    expect(await screen.findByText(title)).toBeInTheDocument();
    expect(screen.getByText(message)).toBeInTheDocument();
    expect(screen.queryByText("Restart the server, then open this page again.") !== null).toBe(restart);
    expect(screen.queryByText(/It is not known whether/)).toBeNull();
  });

  it("does not call an unanswered restore a failure, and finds out from the server", async () => {
    const { user, fake } = setupApp();
    await reachConfirm(user);
    fake.failWith("/backup/restore", 502, "bad_gateway", "The proxy could not reach the server.");
    await user.click(screen.getByRole("button", { name: "Restore now" }));
    expect(await screen.findByText("It is not known whether the restore finished")).toBeInTheDocument();
    fake.meta.needs_setup = false;
    await user.click(screen.getByRole("button", { name: "Check the server" }));
    expect(await screen.findByText(/the restore most likely finished/)).toBeInTheDocument();
  });

  it.each([
    ["backup_no_administrator", "The backup has no enabled administrator, so restoring it would lock everybody out."],
    ["backup_unsupported_data_format", "This backup holds data of a version this version cannot restore."],
    ["backup_too_large", "The backup is larger than this version restores."],
    ["backup_mismatch", "A file in the backup does not match its manifest."],
  ])("shows a backup the server refuses to open (%s)", async (code, message) => {
    const { user, fake } = setupApp();
    await enterToken(user);
    await user.click(await screen.findByRole("button", { name: /Restore from a backup/ }));
    await user.upload(await screen.findByLabelText(/Choose a backup file/), file());
    await user.type(screen.getByLabelText("Passphrase of the backup"), BACKUP_PASSPHRASE);
    fake.failWith("/backup/preview", 422, code, message);
    await user.click(screen.getByRole("button", { name: "Open the backup" }));
    expect(await screen.findByText("The backup could not be opened")).toBeInTheDocument();
    expect(screen.getByText(message)).toBeInTheDocument();
  });

  it("names the settings the environment keeps overriding, and says what a restore is not", async () => {
    const { user } = setupApp({ environmentOverrides: ["UNIFI_URL", "UNIFI_API_KEY"] });
    await enterToken(user);
    await user.click(await screen.findByRole("button", { name: /Restore from a backup/ }));
    await user.upload(await screen.findByLabelText(/Choose a backup file/), file());
    await user.type(screen.getByLabelText("Passphrase of the backup"), BACKUP_PASSPHRASE);
    await user.click(screen.getByRole("button", { name: "Open the backup" }));
    expect(await screen.findByText("Kept from the environment")).toBeInTheDocument();
    expect(screen.getByText("UNIFI_URL, UNIFI_API_KEY")).toBeInTheDocument();
    expect(screen.getByText(/keeps winning over the restored file/)).toBeInTheDocument();
    expect(screen.getByText(/never your UniFi controller's configuration/)).toBeInTheDocument();
    expect(screen.getByText(/also for devices and clients that are gone/)).toBeInTheDocument();
  });
});
