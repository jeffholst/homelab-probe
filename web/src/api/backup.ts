/**
 * Restoring an application backup (`.hlpbackup`, made with `hlp backup` or the web interface): what it would do
 * (`POST /api/v1/backup/preview`, changes nothing) and the restore itself (`POST /api/v1/backup/restore`). A
 * fresh installation uses them with the setup token, like the setup.
 *
 * The passphrases and the file travel in the body of a POST (never a URL) and are forgotten by the caller as soon as
 * the restore is done or abandoned. The preview holds no secret: account names and roles, never a password hash;
 * setting names, never a value.
 */
import { type ApiClient } from "./client";
import { tokenHeaders } from "./setup";
import { bad, flag, record, text } from "./types";

/** The largest backup the server opens (a sealed file, in bytes). */
export const MAX_BACKUP_BYTES = 64 * 1024 * 1024;
/** The passphrase rules of the server (`backup_crypto.MIN_PASSPHRASE`, `MAX_PASSPHRASE`). */
export const MIN_PASSPHRASE = 12;
export const MAX_PASSPHRASE = 1024;

export interface BackupCategory {
  id: string;
  included: boolean;
  files: number;
  present_files: number;
  /** What a restore does to it, in a sentence. */
  restore: string;
}

export interface BackupAccount {
  username: string;
  role: string;
  disabled: boolean;
}

export interface BackupPreview {
  created_at: string;
  app_version: string;
  compatible: boolean;
  running_version: string;
  categories: BackupCategory[];
  accounts: { total: number; administrators: number; users: BackupAccount[]; message: string };
  notes: number;
  triage: number;
  sites: number;
  environment_overrides: string[];
  warnings: { code: string; message: string }[];
  /** A recovery backup of the present state is written first, which needs a passphrase of its own. */
  recovery: { required: boolean; keep: number; folder: string };
}

export interface RestoreResult {
  created_at: string;
  recovery_backup: string | null;
  message: string;
}

const WHAT = "the backup";

function number(fields: Record<string, unknown>, key: string): number {
  const value = fields[key];
  if (typeof value !== "number" || !Number.isFinite(value)) throw bad(WHAT);
  return value;
}

function list(value: unknown): unknown[] {
  if (!Array.isArray(value)) throw bad(WHAT);
  return value;
}

export function parseBackupPreview(value: unknown): BackupPreview {
  const fields = record(value, WHAT);
  const compatibility = record(fields["compatibility"], WHAT);
  const accounts = record(fields["accounts"], WHAT);
  const recovery = record(fields["recovery"], WHAT);
  return {
    created_at: text(fields, "created_at", WHAT),
    app_version: text(fields, "app_version", WHAT),
    compatible: flag(compatibility, "compatible", WHAT),
    running_version: text(compatibility, "running_version", WHAT),
    categories: list(fields["categories"]).map((entry) => {
      const category = record(entry, WHAT);
      return {
        id: text(category, "id", WHAT),
        included: flag(category, "included", WHAT),
        files: number(category, "files"),
        present_files: number(category, "present_files"),
        restore: text(category, "restore", WHAT),
      };
    }),
    accounts: {
      total: number(accounts, "total"),
      administrators: number(accounts, "administrators"),
      message: text(accounts, "message", WHAT),
      users: list(accounts["users"]).map((entry) => {
        const user = record(entry, WHAT);
        return { username: text(user, "username", WHAT), role: text(user, "role", WHAT), disabled: flag(user, "disabled", WHAT) };
      }),
    },
    notes: number(fields, "notes"),
    triage: number(fields, "triage"),
    sites: number(fields, "sites"),
    environment_overrides: list(fields["environment_overrides"]).map((name) => {
      if (typeof name !== "string") throw bad(WHAT);
      return name;
    }),
    warnings: list(fields["warnings"]).map((entry) => {
      const warning = record(entry, WHAT);
      return { code: text(warning, "code", WHAT), message: text(warning, "message", WHAT) };
    }),
    recovery: {
      required: flag(recovery, "required", WHAT),
      keep: number(recovery, "keep"),
      folder: text(recovery, "folder", WHAT),
    },
  };
}

export function parseRestore(value: unknown): RestoreResult {
  const fields = record(value, "the restore");
  if (fields["restored"] !== true) throw bad("the restore");
  const recovery = fields["recovery_backup"] ?? null;
  if (recovery !== null && typeof recovery !== "string") throw bad("the restore");
  return { created_at: text(fields, "created_at", "the restore"), recovery_backup: recovery, message: text(fields, "message", "the restore") };
}

/** The bytes of a file as base64, read in slices so a large backup does not overflow the call stack. */
export async function fileToBase64(file: Blob): Promise<string> {
  const bytes = new Uint8Array(await file.arrayBuffer());
  const parts: string[] = [];
  const slice = 0x8000;
  for (let start = 0; start < bytes.length; start += slice) {
    parts.push(String.fromCharCode(...bytes.subarray(start, start + slice)));
  }
  return btoa(parts.join(""));
}

export interface RestoreRequest {
  archive: string;
  passphrase: string;
  recoveryPassphrase?: string;
  recoveryConfirm?: string;
}

export interface BackupApi {
  preview(token: string | null, archive: string, passphrase: string): Promise<BackupPreview>;
  restore(token: string | null, request: RestoreRequest): Promise<RestoreResult>;
}

export function createBackupApi(client: ApiClient): BackupApi {
  return {
    preview: (token, archive, passphrase) =>
      client.post("/backup/preview", { body: { archive, passphrase }, headers: tokenHeaders(token), parse: parseBackupPreview }),
    restore: (token, request) =>
      client.post("/backup/restore", {
        body: {
          archive: request.archive,
          passphrase: request.passphrase,
          ...(request.recoveryPassphrase === undefined
            ? {}
            : { recovery_passphrase: request.recoveryPassphrase, recovery_confirm: request.recoveryConfirm ?? "" }),
          confirm: true,
        },
        headers: tokenHeaders(token),
        parse: parseRestore,
      }),
  };
}
