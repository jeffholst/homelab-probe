import { type DraftInput, type Finish, type SetupStatus } from "../api/setup";
import { ApiRefused, FakeApi, type FakeAccount, type FakeApiOptions } from "./fakeApi";

export const SETUP_TOKEN = "synthetic-setup-token";
export const UNVERIFIED_PHRASE = "send my API key without verifying the controller";
export function emptySetup(mode: "setup" | "admin" = "setup"): SetupStatus {
  return { mode, unverified_phrase: UNVERIFIED_PHRASE,
    draft: { url: "", site: "default", api_key_set: false, verify: "true", certificate: null, connection_ok: null, notify: [] } };
}
export const CONNECTION_CHECK = { id: "controller.connect", status: "ok", title: "Connection", message: "The controller answered.", fix: "" };

/** Synthetic setup server; authorization/order is enforced by FakeApi, not by a public route shortcut. */
export class FakeSetup {
  readonly fake: FakeApi;
  readonly status: SetupStatus;
  readonly accounts: FakeAccount[];
  finishResult: Finish | null = null;
  connectionOk = true;
  certificateUsable = true;
  previewWarnings: string[] = [];
  previewTotal = 0;
  lastFinish: { username?: string; password?: string } | null = null;

  constructor(mode: "setup" | "admin" = "setup", options: FakeApiOptions = {}) {
    this.accounts = options.accounts ?? [];
    this.fake = new FakeApi({ ...options, accounts: this.accounts, setupToken: SETUP_TOKEN,
      meta: { ...options.meta, needs_setup: true, setup_mode: mode } });
    this.status = emptySetup(mode);
    const route = this.fake.route.bind(this.fake);
    route("GET", "/setup/status", () => this.status, { setup: true });
    route("POST", "/setup/draft", ({ body }) => {
      this.fullMode();
      const given = body as DraftInput;
      if (given.url !== undefined && (!given.url.startsWith("https://") || given.url.includes("@"))) {
        throw new ApiRefused(422, "invalid_setting", "The address is not valid.");
      }
      if (given.verify === "false" && given.confirm !== UNVERIFIED_PHRASE) throw new ApiRefused(422, "confirmation_required", "Confirmation required.");
      if (given.verify === "pin" && (!this.status.draft.certificate?.usable || given.fingerprint !== this.status.draft.certificate.fingerprint)) {
        throw new ApiRefused(422, "fingerprint_mismatch", "The fingerprint does not match.");
      }
      const draft = this.status.draft;
      if (given.url !== undefined && given.url !== draft.url) { draft.certificate = null; draft.verify = "true"; }
      if (given.url !== undefined || given.site !== undefined || given.api_key !== undefined || given.verify !== undefined) draft.connection_ok = null;
      draft.url = given.url ?? draft.url;
      draft.site = given.site ?? draft.site;
      draft.verify = given.verify ?? draft.verify;
      if (given.api_key !== undefined) draft.api_key_set = given.api_key !== "";
      if (given.notify) draft.notify = [...new Set([...draft.notify, ...Object.keys(given.notify)])];
      return this.status;
    }, { setup: true });
    route("POST", "/setup/certificate", () => {
      this.fullMode();
      this.status.draft.certificate = { fingerprint: "AA:BB:CC:DD", usable: this.certificateUsable,
        problem_message: this.certificateUsable ? "" : "The certificate is not valid for this address." };
      return this.status;
    }, { setup: true });
    route("POST", "/setup/connection", () => {
      this.fullMode();
      this.status.draft.connection_ok = this.connectionOk;
      return { ok: this.connectionOk, sites: this.connectionOk ? [{ name: "Synthetic Lab", ref: "lab", id: "fake-site" }] : [],
        checks: [{ ...CONNECTION_CHECK, status: this.connectionOk ? "ok" : "fail" }] };
    }, { setup: true });
    route("POST", "/setup/preview", () => {
      this.fullMode();
      return { total: this.previewTotal, warnings: this.previewWarnings, areas: [], summary: {}, findings: [] };
    }, { setup: true });
    route("POST", "/setup/notifications", () => {
      this.fullMode();
      return { checks: [{ ...CONNECTION_CHECK, id: "notify.config", title: "Notifications", message: "Nothing was sent." }] };
    }, { setup: true });
    route("POST", "/setup/finish", ({ body }) => {
      this.lastFinish = body as { username?: string; password?: string };
      if (this.status.mode === "setup" && this.status.draft.connection_ok !== true) throw new ApiRefused(409, "not_tested", "Test first.");
      if (this.finishResult) return this.finishResult;
      if (!this.accounts.some((account) => account.role === "admin" && !account.disabled)) {
        const { username, password } = this.lastFinish;
        if (!username || !password || username.length < 3 || password.length < 12) throw new ApiRefused(422, "invalid_admin", "Invalid administrator.");
        this.accounts.push({ username, password, role: "admin" });
      }
      this.fake.meta.needs_setup = false;
      this.fake.meta.setup_mode = null;
      this.status.mode = null;
      return { finished: true, written: [], admin_created: true };
    }, { setup: true });
  }
  private fullMode() {
    if (this.status.mode === "admin") throw new ApiRefused(409, "step_unavailable", "Only the first administrator is missing.");
  }
}
