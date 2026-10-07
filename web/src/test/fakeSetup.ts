/**
 * The guided setup and the restore of a backup, faked for Vitest on top of `FakeApi` (which enforces who may call
 * them: the setup token while no administrator exists, see fakeApi.ts). The answers have the shapes of
 * homelab_probe/server/wizard.py, backup_api.py and restore_api.py, the refusals their codes and sentences, so a page
 * reacts to them as it would to the real server. The real server's own rules are tested in Python; this is about the
 * page.
 */
import type { CheckStatus } from "../api/setup";
import { ApiRefused, type FakeAccount, type FakeApi, type RouteRequest } from "./fakeApi";

export const SETUP_TOKEN = "setup-token-for-tests-0123456789";
export const UNVERIFIED_PHRASE = "send my API key without verifying the controller";
export const FINGERPRINT = "AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89";
export const BACKUP_PASSPHRASE = "the backup passphrase";
/** What a test file holds to be a "backup" the fake opens (anything else is `backup_not_a_backup`). */
export const BACKUP_CONTENT = "HLPBACKUP-test";

const NOTIFY = new Set([
  "NOTIFY_NTFY_URL",
  "NOTIFY_NTFY_TOKEN",
  "NOTIFY_WEBHOOK_URL",
  "NOTIFY_WEBHOOK_TOKEN",
  "NOTIFY_SMTP_HOST",
  "NOTIFY_SMTP_PORT",
  "NOTIFY_SMTP_SECURITY",
  "NOTIFY_SMTP_USER",
  "NOTIFY_SMTP_PASSWORD",
  "NOTIFY_EMAIL_FROM",
  "NOTIFY_EMAIL_TO",
]);

export interface FakeSetupOptions {
  mode?: "setup" | "admin";
  /** Why the certificate the controller shows cannot be pinned (a tlsprobe reason), or "" when it can. */
  certificateProblem?: string;
  /** Whether the connection test passes. */
  connectionOk?: boolean;
  /** A passing test that has a warning among its checks (a partial result). */
  connectionWarning?: boolean;
  sites?: { name: string; ref: string; id: string }[];
  /** Answer `finish` with the fallback for this reason instead of saving. */
  fallback?: "environment" | "env_file_named" | "not_writable";
  /** Whether a restore has a present state to keep (and so needs a recovery passphrase). */
  recoveryRequired?: boolean;
  /** The accounts the fake backup holds. */
  backupAccounts?: FakeAccount[];
  /** Settings of the backup that this server's environment keeps overriding (names only). */
  environmentOverrides?: string[];
}

interface Draft {
  url: string;
  site: string;
  apiKey: string;
  verify: "true" | "pin" | "false";
  certificate: boolean;
  connectionOk: boolean | null;
  notify: Record<string, string>;
}

function body(request: RouteRequest): Record<string, unknown> {
  const value = request.body;
  if (typeof value !== "object" || value === null || Array.isArray(value)) return {};
  return value as Record<string, unknown>;
}

function check(id: string, status: CheckStatus, title: string, message: string, fix = "") {
  return { id, section: "controller", status, title, message, fix };
}

export class FakeSetup {
  draft: Draft = { url: "", site: "default", apiKey: "", verify: "true", certificate: false, connectionOk: null, notify: {} };
  /** Every body the setup routes received, for tests that check what was (or was not) sent. */
  readonly bodies: { path: string; body: unknown }[] = [];
  readonly options: Required<Omit<FakeSetupOptions, "fallback">> & { fallback?: FakeSetupOptions["fallback"] };

  constructor(
    readonly fake: FakeApi,
    options: FakeSetupOptions = {},
  ) {
    this.options = {
      mode: "setup",
      certificateProblem: "",
      connectionOk: true,
      connectionWarning: false,
      sites: [
        { name: "Default", ref: "default", id: "site-1" },
        { name: "Cabin", ref: "cabin", id: "site-2" },
      ],
      recoveryRequired: false,
      backupAccounts: [{ username: "restored", password: "restored password", role: "admin" }],
      environmentOverrides: [],
      ...options,
    };
    fake.setupToken = SETUP_TOKEN;
    fake.meta.needs_setup = true;
    fake.meta.setup_mode = this.options.mode;

    const setup = { setup: true };
    fake.route("GET", "/setup/status", () => this.status(), setup);
    fake.route("POST", "/setup/draft", (request) => this.change(request), setup);
    fake.route("POST", "/setup/certificate", (request) => this.certificate(request), setup);
    fake.route("POST", "/setup/connection", (request) => this.connection(request), setup);
    fake.route("POST", "/setup/preview", (request) => this.preview(request), setup);
    fake.route("POST", "/setup/notifications", (request) => this.notifications(request), setup);
    fake.route("POST", "/setup/finish", (request) => this.finish(request), setup);
    fake.route("POST", "/backup/preview", (request) => this.backupPreview(request), setup);
    fake.route("POST", "/backup/restore", (request) => this.restore(request), setup);
  }

  private record(request: RouteRequest): void {
    this.bodies.push({ path: request.path, body: request.body });
  }

  private mode(): "setup" | "admin" | null {
    const mode = this.fake.meta.setup_mode;
    return mode === "setup" || mode === "admin" ? mode : null;
  }

  private step(name: string): void {
    if (this.mode() === "admin" && name !== "status" && name !== "finish") {
      throw new ApiRefused(409, "step_unavailable", "This server has its settings: only the first administrator is missing, and that is the one step available.");
    }
  }

  status(): object {
    const draft = this.draft;
    return {
      mode: this.mode(),
      reason: this.mode() === "admin" ? "no_admin" : "no_config",
      unverified_phrase: UNVERIFIED_PHRASE,
      draft: {
        url: draft.url,
        site: draft.site,
        api_key_set: draft.apiKey !== "",
        verify: draft.verify,
        certificate: draft.certificate
          ? {
              fingerprint: FINGERPRINT,
              usable: this.options.certificateProblem === "",
              problem: this.options.certificateProblem,
              problem_message: this.options.certificateProblem === "" ? "" : "The certificate is not valid for this address.",
            }
          : null,
        connection_ok: draft.connectionOk,
        notify: Object.keys(draft.notify).sort(),
      },
    };
  }

  private change(request: RouteRequest): object {
    this.record(request);
    this.step("draft");
    const given = body(request);
    const next: Draft = { ...this.draft, notify: { ...this.draft.notify } };
    const changed: string[] = [];
    const invalid = (setting: string, message: string) => new ApiRefused(422, "invalid_setting", message, { setting });
    if (typeof given["url"] === "string") {
      const url = given["url"].trim().replace(/\/+$/, "");
      if (!url.startsWith("https://") || url.length <= "https://".length) throw invalid("UNIFI_URL", "UNIFI_URL must be an https:// address.");
      if (url !== next.url) {
        Object.assign(next, { url, certificate: false, connectionOk: null, verify: "true" });
        changed.push("UNIFI_URL");
      }
    }
    if (typeof given["site"] === "string" && given["site"] !== next.site) {
      Object.assign(next, { site: given["site"], connectionOk: null });
      changed.push("UNIFI_SITE_ID");
    }
    if (typeof given["api_key"] === "string") {
      const key = given["api_key"].trim();
      if (key.length < 8) throw invalid("UNIFI_API_KEY", "UNIFI_API_KEY looks too short to be an API key.");
      if (key !== next.apiKey) {
        Object.assign(next, { apiKey: key, connectionOk: null });
        changed.push("UNIFI_API_KEY");
      }
    }
    const verify = given["verify"];
    if (verify === "false" && given["confirm"] !== UNVERIFIED_PHRASE) {
      throw new ApiRefused(422, "confirmation_required", "Turning certificate checking off needs the confirmation sentence, typed exactly.", {
        setting: "UNIFI_VERIFY_SSL",
      });
    }
    if (verify === "pin") {
      if (!next.certificate) throw new ApiRefused(422, "certificate_required", "Fetch the controller's certificate first.", { setting: "UNIFI_VERIFY_SSL" });
      if (given["fingerprint"] !== FINGERPRINT) {
        throw new ApiRefused(422, "fingerprint_mismatch", "The fingerprint does not match the certificate that was fetched.", { setting: "UNIFI_VERIFY_SSL" });
      }
    }
    if ((verify === "true" || verify === "pin" || verify === "false") && verify !== next.verify) {
      Object.assign(next, { verify, connectionOk: null });
      changed.push("UNIFI_VERIFY_SSL");
    }
    const notify = given["notify"];
    if (typeof notify === "object" && notify !== null) {
      for (const [name, value] of Object.entries(notify as Record<string, unknown>)) {
        if (!NOTIFY.has(name)) throw invalid("NOTIFY_*", "That is not a notification setting, or its value is too long.");
        if (typeof value === "string" && value.trim() !== "") next.notify[name] = value.trim();
        else delete next.notify[name];
      }
      changed.push("NOTIFY_*");
    }
    this.draft = next;
    return { ...this.status(), changed };
  }

  private requireDraft(key = true): void {
    if (this.draft.url === "" || (key && this.draft.apiKey === "")) {
      throw new ApiRefused(409, "draft_incomplete", "Enter the controller's address and an API key first.");
    }
  }

  private certificate(request: RouteRequest): object {
    this.record(request);
    this.step("certificate");
    this.requireDraft(false);
    this.draft = { ...this.draft, certificate: true };
    return this.status();
  }

  private connection(request: RouteRequest): object {
    this.record(request);
    this.step("connection");
    this.requireDraft();
    const ok = this.options.connectionOk;
    this.draft = { ...this.draft, connectionOk: ok };
    return {
      ok,
      sites: ok ? this.options.sites : [],
      checks: ok
        ? [
            check("controller.reachable", "ok", "Controller", "answered"),
            check("controller.api_key", "ok", "API key", "accepted"),
            ...(this.options.connectionWarning ? [check("endpoint.stat_alluser", "warn", "Client history", "could not be read", "Check the key's permissions.")] : []),
          ]
        : [check("controller.reachable", "ok", "Controller", "answered"), check("controller.api_key", "fail", "API key", "refused", "Create a new key and paste it again.")],
    };
  }

  private preview(request: RouteRequest): object {
    this.record(request);
    this.step("preview");
    this.requireDraft();
    return {
      areas: ["devices", "health"],
      summary: { critical: 1, warning: 1, info: 0 },
      findings: [
        { severity: "critical", code: "device.offline", subject: "Garage switch", message: "is offline" },
        { severity: "warning", code: "wifi.weak_signal", subject: "Phone", message: "has a weak signal" },
      ],
      total: 2,
      warnings: [],
    };
  }

  private notifications(request: RouteRequest): object {
    this.record(request);
    this.step("notifications");
    const names = Object.keys(this.draft.notify);
    return { checks: names.length === 0 ? [check("notify.none", "info", "Notifications", "none configured")] : [check("notify.ntfy", "ok", "ntfy", "configured")] };
  }

  private finish(request: RouteRequest): object {
    this.record(request);
    if (this.mode() === null) throw new ApiRefused(409, "already_set_up", "This server is set up already.");
    const given = body(request);
    const needsAdmin = !this.fake.hasAdministrator();
    let admin: FakeAccount | null = null;
    if (needsAdmin) {
      const username = typeof given["username"] === "string" ? given["username"].trim().toLowerCase() : "";
      const password = typeof given["password"] === "string" ? given["password"] : "";
      if (username === "" || password === "") throw new ApiRefused(422, "admin_required", "Choose a user name and a password for the first administrator.");
      if (username === "taken") throw new ApiRefused(422, "invalid_admin", "That user name is taken.");
      admin = { username, password, role: "admin" };
    }
    if (this.mode() === "setup") {
      this.requireDraft();
      if (this.draft.connectionOk !== true) throw new ApiRefused(409, "not_tested", "Test the connection with these settings first.");
      if (this.options.fallback !== undefined) {
        const shown = `UNIFI_URL=${this.draft.url}\nUNIFI_API_KEY=your-api-key-here\nUNIFI_SITE_ID=${this.draft.site}\n`;
        return {
          finished: false,
          written: false,
          reason: this.options.fallback,
          detail: "",
          environment_names: this.options.fallback === "environment" ? ["UNIFI_URL"] : [],
          placeholders: ["UNIFI_API_KEY"],
          env: shown,
          compose: `services:\n  hlp:\n    environment:\n      UNIFI_URL: "${this.draft.url}"\n`,
          certificate: this.draft.verify === "pin" ? "-----BEGIN CERTIFICATE-----\nTEST\n-----END CERTIFICATE-----\n" : null,
        };
      }
    }
    if (admin !== null) this.fake.addAccount(admin);
    this.leave();
    return { finished: true, written: [{ id: "setup.env", status: "written", message: ".env written" }], admin_created: admin !== null };
  }

  private leave(): void {
    this.fake.meta.needs_setup = false;
    this.fake.meta.setup_mode = null;
    this.draft = { url: "", site: this.draft.site, apiKey: "", verify: "true", certificate: false, connectionOk: null, notify: {} };
  }

  private open(given: Record<string, unknown>): void {
    let decoded = "";
    try {
      decoded = atob(typeof given["archive"] === "string" ? given["archive"] : "");
    } catch {
      decoded = "";
    }
    if (decoded !== BACKUP_CONTENT) throw new ApiRefused(422, "backup_not_a_backup", "This is not a Homelab Probe backup.");
    if (given["passphrase"] !== BACKUP_PASSPHRASE) {
      throw new ApiRefused(422, "backup_decrypt", "The passphrase is wrong, or the backup was modified or damaged.");
    }
  }

  private backupPreview(request: RouteRequest): object {
    this.record(request);
    this.open(body(request));
    const users = this.options.backupAccounts;
    return {
      created_at: "2026-09-30T08:15:00Z",
      app_version: "0.4.0",
      format: 1,
      data_format: 1,
      compatibility: { compatible: true, supported_data_formats: [1], running_version: "0.0.0-test" },
      categories: [
        { id: "settings", included: true, files: 1, present_files: 0, restore: "replace" },
        { id: "config", included: true, files: 1, present_files: 0, restore: "replace" },
        { id: "accounts", included: true, files: 1, present_files: 0, restore: "replace" },
        { id: "snapshots", included: false, files: 0, present_files: 0, restore: "keep what is there" },
      ],
      accounts: {
        replace: true,
        total: users.length,
        administrators: users.filter((user) => user.role === "admin" && user.disabled !== true).length,
        users: users.map((user) => ({ username: user.username, role: user.role, disabled: user.disabled === true })),
        message: "These accounts and passwords replace the present ones, every session ends, and everybody logs in again with the credentials of the backup.",
      },
      notes: 3,
      triage: 2,
      sites: 1,
      environment_overrides: this.options.environmentOverrides,
      warnings:
        this.options.environmentOverrides.length === 0
          ? []
          : [
              {
                code: "environment_overrides",
                message: `These settings are also set in the environment, which keeps winning over the restored file: ${this.options.environmentOverrides.join(", ")}.`,
              },
            ],
      recovery: { required: this.options.recoveryRequired, keep: 3, folder: "recovery" },
    };
  }

  private restore(request: RouteRequest): object {
    this.record(request);
    const given = body(request);
    if (given["confirm"] !== true) throw new ApiRefused(422, "invalid_parameter", "A parameter is not valid.");
    this.open(given);
    let recovery: string | null = null;
    if (this.options.recoveryRequired) {
      const passphrase = typeof given["recovery_passphrase"] === "string" ? given["recovery_passphrase"] : "";
      if (passphrase === "") throw new ApiRefused(422, "recovery_passphrase_required", "A passphrase for the recovery backup of the present state is needed.");
      if (passphrase.length < 12) throw new ApiRefused(422, "invalid_recovery_passphrase", "The recovery passphrase needs 12 to 1024 characters.");
      if (passphrase !== given["recovery_confirm"]) throw new ApiRefused(422, "recovery_passphrase_mismatch", "The two recovery passphrases are not the same.");
      recovery = "recovery-20261006-101500Z.hlpbackup";
    }
    this.fake.replaceAccounts(this.options.backupAccounts);
    this.leave();
    return {
      restored: true,
      created_at: "2026-09-30T08:15:00Z",
      recovery_backup: recovery,
      sessions_ended: true,
      message: "The backup was restored. Every session has ended: log in with the accounts of the backup.",
    };
  }
}
