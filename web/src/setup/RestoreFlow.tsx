import { useId, useState, type DragEvent } from "react";

import { MAX_BACKUP_BYTES, MIN_PASSPHRASE, fileToBase64, type BackupPreview, type RestoreResult } from "../api/backup";
import { isApiError } from "../api/errors";
import { useServices } from "../app/services";
import { Banner } from "../components/DataStates";
import { Text } from "../components/Text";
import { Icon } from "../components/ui/Icon";
import { SecretInput } from "../components/ui/SecretInput";
import { Stepper } from "../components/ui/Stepper";
import { formatDateTime } from "../lib/format";
import { ErrorBanner, FieldError, InsecureNotice, StepFrame, UncertainOutcome, codeOf, isUncertain, messageOf, useSetupMutation } from "./common";

const STEPS = ["Backup file", "Review", "Restore"] as const;

const CATEGORY_NAMES: Record<string, string> = {
  settings: "Health check settings (hlp.toml)",
  config: "Connection settings (.env)",
  accounts: "Web accounts",
  certificates: "Pinned certificates",
  notes: "Notes",
  triage: "Finding triage",
  snapshots: "Snapshots",
  audit: "Audit log",
};

function megabytes(bytes: number): string {
  return `${(bytes / (1024 * 1024)).toFixed(bytes < 1024 * 1024 ? 2 : 1)} MB`;
}

/**
 * Restoring a backup of another installation (a `.hlpbackup` file and its passphrase) onto this one: the server opens
 * it and says what a restore would do, the owner reads that and confirms, and it is restored. The file and the
 * passphrases stay in this page's memory until the restore is done or abandoned; they are never stored.
 */
export function RestoreFlow({ token, onCancel, onRestored }: { token: string | null; onCancel: () => void; onRestored: (result: RestoreResult) => void }) {
  const { backup } = useServices();
  const [file, setFile] = useState<File | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const [passphrase, setPassphrase] = useState("");
  const [archive, setArchive] = useState<string | null>(null);
  const [preview, setPreview] = useState<BackupPreview | null>(null);
  const [step, setStep] = useState(0);
  const [passphraseMissing, setPassphraseMissing] = useState(false);

  const open = useSetupMutation({
    mutationFn: async () => {
      if (file === null) throw new Error("no file");
      const encoded = await fileToBase64(file);
      const shown = await backup.preview(token, encoded, passphrase);
      return { encoded, shown };
    },
    onSuccess: ({ encoded, shown }) => {
      setArchive(encoded);
      setPreview(shown);
      setStep(1);
    },
  });

  function choose(chosen: File | undefined) {
    open.reset();
    if (chosen === undefined) return;
    if (chosen.size > MAX_BACKUP_BYTES) {
      setFile(null);
      setFileError(`This file is ${megabytes(chosen.size)}; a backup can be at most ${megabytes(MAX_BACKUP_BYTES)}.`);
      return;
    }
    setFileError(null);
    setFile(chosen);
  }

  function forget() {
    setArchive(null);
    setPreview(null);
    setPassphrase("");
  }

  const passphraseError =
    codeOf(open.error) === "backup_decrypt" ? messageOf(open.error) : passphraseMissing && passphrase === "" ? "Enter the passphrase of the backup." : null;
  return (
    <>
      <div className="auth__intro">
        <p className="eyebrow">
          <Icon name="history" /> Restore
        </p>
        <h1>Restore from a backup</h1>
        <p>Bring back the settings, accounts, notes and history of an installation from its encrypted backup file.</p>
      </div>
      <InsecureNotice />
      <Stepper steps={STEPS} current={step} label="Restore progress" />
      {step === 0 && (
        <StepFrame
          title="Choose the backup"
          icon="upload"
          lede="A .hlpbackup file made by Homelab Probe (hlp backup, or the web interface), and the passphrase it was made with."
          onBack={onCancel}
          onSubmit={() => {
            setPassphraseMissing(passphrase === "");
            if (file === null) setFileError("Choose a backup file.");
            else if (passphrase !== "") open.mutate();
          }}
          primary={{ label: open.isPending ? "Opening…" : "Open the backup", busy: open.isPending }}
        >
          {open.isError && codeOf(open.error) !== "backup_decrypt" && <ErrorBanner error={open.error} title="The backup could not be opened" />}
          <FileDrop file={file} error={fileError} onChoose={choose} />
          <div className="field">
            <label htmlFor="restore-passphrase">Passphrase of the backup</label>
            <SecretInput
              id="restore-passphrase"
              autoComplete="off"
              value={passphrase}
              aria-invalid={passphraseError !== null}
              aria-describedby={passphraseError !== null ? "restore-passphrase-error" : undefined}
              onChange={(event) => {
                setPassphrase(event.target.value);
              }}
            />
            <FieldError id="restore-passphrase-error" message={passphraseError} />
          </div>
        </StepFrame>
      )}
      {step === 1 && preview !== null && (
        <ReviewStep
          preview={preview}
          onBack={() => {
            forget();
            setStep(0);
          }}
          onNext={() => {
            setStep(2);
          }}
        />
      )}
      {step === 2 && preview !== null && archive !== null && (
        <ConfirmStep
          token={token}
          preview={preview}
          archive={archive}
          passphrase={passphrase}
          onBack={() => {
            setStep(1);
          }}
          onRestored={(result) => {
            forget();
            onRestored(result);
          }}
        />
      )}
    </>
  );
}

function FileDrop({ file, error, onChoose }: { file: File | null; error: string | null; onChoose: (file: File | undefined) => void }) {
  const id = useId();
  const [dragging, setDragging] = useState(false);
  function drop(event: DragEvent<HTMLLabelElement>) {
    event.preventDefault();
    setDragging(false);
    onChoose(event.dataTransfer.files[0]);
  }
  return (
    <div className="field">
      <label
        className="dropzone"
        htmlFor={id}
        data-dragging={dragging}
        onDragOver={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => {
          setDragging(false);
        }}
        onDrop={drop}
      >
        <Icon name={file === null ? "upload" : "file"} />
        {file === null ? (
          <>
            <strong>Choose a backup file</strong>
            <span className="muted small">or drop it here (.hlpbackup, at most {megabytes(MAX_BACKUP_BYTES)})</span>
          </>
        ) : (
          <>
            <strong>
              <Text value={file.name} />
            </strong>
            <span className="muted small">{megabytes(file.size)}. Choose another file to replace it.</span>
          </>
        )}
        <input
          id={id}
          type="file"
          accept=".hlpbackup,application/octet-stream"
          aria-describedby={error !== null ? `${id}-error` : undefined}
          onChange={(event) => {
            onChoose(event.target.files?.[0]);
            event.target.value = "";
          }}
        />
      </label>
      <FieldError id={`${id}-error`} message={error} />
    </div>
  );
}

function ReviewStep({ preview, onBack, onNext }: { preview: BackupPreview; onBack: () => void; onNext: () => void }) {
  const included = preview.categories.filter((category) => category.included);
  return (
    <StepFrame
      title="What the restore would do"
      icon="eye"
      lede="Read this before you restore. Nothing has changed yet. This restores Homelab Probe's own files, never your UniFi controller's configuration."
      onBack={onBack}
      onSubmit={onNext}
      primary={{ label: "Continue" }}
    >
      <dl className="facts">
        <dt>Made</dt>
        <dd>{formatDateTime(Date.parse(preview.created_at)) || <Text value={preview.created_at} />}</dd>
        <dt>By version</dt>
        <dd>
          <Text value={preview.app_version} />
          <span className="muted"> (this server runs <Text value={preview.running_version} />)</span>
        </dd>
        <dt>Contents</dt>
        <dd>
          {preview.notes} notes, {preview.triage} triage entries, {preview.sites} {preview.sites === 1 ? "site" : "sites"}
          <span className="hint">
            Notes and triage are restored as they are, also for devices and clients that are gone; nothing is matched again by name or address.
          </span>
        </dd>
        {preview.environment_overrides.length > 0 && (
          <>
            <dt>Kept from the environment</dt>
            <dd>
              <Text value={preview.environment_overrides.join(", ")} />
            </dd>
          </>
        )}
      </dl>
      {preview.warnings.map((warning) => (
        <Banner key={warning.code} tone="warning" title="Note">
          <p className="banner__text">
            <Text value={warning.message} />
          </p>
        </Banner>
      ))}
      <h3>What is replaced</h3>
      <div className="table-wrap">
        <table className="table">
          <thead>
            <tr>
              <th scope="col">Part</th>
              <th scope="col">In the backup</th>
              <th scope="col">On restore</th>
            </tr>
          </thead>
          <tbody>
            {preview.categories.map((category) => (
              <tr key={category.id}>
                <th scope="row">{CATEGORY_NAMES[category.id] ?? <Text value={category.id} />}</th>
                <td>{category.included ? `${category.files} ${category.files === 1 ? "file" : "files"}` : "No"}</td>
                <td>
                  <Text value={category.restore} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {included.some((category) => category.id === "accounts") && (
        <>
          <h3>Accounts after the restore</h3>
          <Banner tone="info" title={`${preview.accounts.total} accounts, ${preview.accounts.administrators} enabled ${preview.accounts.administrators === 1 ? "administrator" : "administrators"}`}>
            <p className="banner__text">
              <Text value={preview.accounts.message} />
            </p>
          </Banner>
          <ul className="plain-list">
            {preview.accounts.users.map((user) => (
              <li key={user.username} className="row">
                <Icon name="user" />
                <Text value={user.username} />
                <span className={user.role === "admin" ? "pill pill--info" : "pill"}>{user.role === "admin" ? "Administrator" : "Viewer"}</span>
                {user.disabled && <span className="pill pill--warning">Disabled</span>}
              </li>
            ))}
          </ul>
        </>
      )}
    </StepFrame>
  );
}

const FAILURES: Record<string, { title: string; restart?: boolean; tone?: "warning" }> = {
  backup_busy: { title: "The server is busy" },
  restore_in_progress: { title: "A restore is already running" },
  read_only: { title: "This server does not restore backups" },
  demo: { title: "This server does not restore backups" },
  backup_recovery_failed: { title: "Nothing was changed" },
  backup_restore_failed: { title: "The backup was not restored" },
  backup_restore_pending: { title: "The restore did not complete", restart: true },
  backup_journal_damaged: { title: "The restore did not complete", restart: true },
  reload_failed: { title: "Restored, but the server needs a restart", restart: true, tone: "warning" },
};

/**
 * Why a restore failed, in the server's words, told apart by its fixed code: refused before anything changed, put
 * back, left for the next start, restored but not loaded. An answer that never arrived is not a failure: it may have
 * happened, and the page says so instead of guessing.
 */
function RestoreFailure({ error }: { error: unknown }) {
  const code = codeOf(error) ?? "";
  const known = FAILURES[code];
  if (known === undefined && (isUncertain(error) || (isApiError(error) && error.status >= 500))) return <UncertainOutcome what="restore" />;
  if (known === undefined) return <ErrorBanner error={error} title="The backup was not restored" />;
  return (
    <Banner tone={known.tone ?? "danger"} role="alert" title={known.title}>
      <p className="banner__text">
        <Text value={messageOf(error)} />
      </p>
      {known.restart === true && <p className="banner__text">Restart the server, then open this page again.</p>}
    </Banner>
  );
}

function ConfirmStep({
  token,
  preview,
  archive,
  passphrase,
  onBack,
  onRestored,
}: {
  token: string | null;
  preview: BackupPreview;
  archive: string;
  passphrase: string;
  onBack: () => void;
  onRestored: (result: RestoreResult) => void;
}) {
  const { backup } = useServices();
  const [understood, setUnderstood] = useState(false);
  const [recovery, setRecovery] = useState("");
  const [recoveryAgain, setRecoveryAgain] = useState("");
  const [problem, setProblem] = useState<{ field: "understood" | "recovery" | "repeat"; message: string } | null>(null);
  const restore = useSetupMutation({
    mutationFn: () =>
      backup.restore(token, {
        archive,
        passphrase,
        ...(preview.recovery.required ? { recoveryPassphrase: recovery, recoveryConfirm: recoveryAgain } : {}),
      }),
    onSuccess: (result) => {
      setRecovery("");
      setRecoveryAgain("");
      onRestored(result);
    },
  });

  function submit() {
    if (!understood) {
      setProblem({ field: "understood", message: "Tick the box to confirm." });
      return;
    }
    if (preview.recovery.required) {
      if (Array.from(recovery).length < MIN_PASSPHRASE) {
        setProblem({ field: "recovery", message: `Use at least ${MIN_PASSPHRASE} characters.` });
        return;
      }
      if (recovery !== recoveryAgain) {
        setProblem({ field: "repeat", message: "The two passphrases are not the same." });
        return;
      }
    }
    setProblem(null);
    restore.mutate();
  }

  const serverField = codeOf(restore.error);
  const recoveryServerError =
    serverField === "invalid_recovery_passphrase" || serverField === "recovery_passphrase_required" ? messageOf(restore.error) : null;
  const repeatServerError = serverField === "recovery_passphrase_mismatch" ? messageOf(restore.error) : null;
  const errorOf = (field: "understood" | "recovery" | "repeat") =>
    problem?.field === field ? problem.message : field === "recovery" ? recoveryServerError : field === "repeat" ? repeatServerError : null;

  return (
    <StepFrame
      title="Restore"
      icon="history"
      lede="The restore replaces this installation's own files as one set. The controller itself is never changed."
      onBack={onBack}
      onSubmit={submit}
      primary={{ label: restore.isPending ? "Restoring…" : "Restore now", busy: restore.isPending, brand: true }}
    >
      {restore.isError && recoveryServerError === null && repeatServerError === null && <RestoreFailure error={restore.error} />}
      {preview.recovery.required && (
        <>
          <Banner tone="info" title="A recovery backup comes first">
            <p className="banner__text">
              This installation already has files. They are saved first as an encrypted recovery backup in the{" "}
              <code>
                <Text value={preview.recovery.folder} />
              </code>{" "}
              folder of the data directory (the newest {preview.recovery.keep} are kept). Choose a passphrase for it: keep it somewhere safe,
              apart from this machine.
            </p>
          </Banner>
          <div className="field">
            <label htmlFor="recovery-passphrase">Passphrase for the recovery backup</label>
            <p id="recovery-passphrase-hint" className="hint">
              At least {MIN_PASSPHRASE} characters, and not the passphrase of the backup you restore.
            </p>
            <SecretInput
              id="recovery-passphrase"
              autoComplete="new-password"
              value={recovery}
              aria-invalid={errorOf("recovery") !== null}
              aria-describedby={`recovery-passphrase-hint${errorOf("recovery") !== null ? " recovery-passphrase-error" : ""}`}
              onChange={(event) => {
                setRecovery(event.target.value);
              }}
            />
            <FieldError id="recovery-passphrase-error" message={errorOf("recovery")} />
          </div>
          <div className="field">
            <label htmlFor="recovery-repeat">Passphrase again</label>
            <SecretInput
              id="recovery-repeat"
              autoComplete="new-password"
              value={recoveryAgain}
              aria-invalid={errorOf("repeat") !== null}
              aria-describedby={errorOf("repeat") !== null ? "recovery-repeat-error" : undefined}
              onChange={(event) => {
                setRecoveryAgain(event.target.value);
              }}
            />
            <FieldError id="recovery-repeat-error" message={errorOf("repeat")} />
          </div>
        </>
      )}
      <div className="check">
        <input
          id="restore-understood"
          type="checkbox"
          checked={understood}
          aria-describedby={errorOf("understood") !== null ? "restore-understood-error" : undefined}
          onChange={(event) => {
            setUnderstood(event.target.checked);
          }}
        />
        <label htmlFor="restore-understood">
          I understand that the accounts and passwords of the backup replace the present ones, and that everybody has to log in again.
        </label>
      </div>
      <FieldError id="restore-understood-error" message={errorOf("understood")} />
    </StepFrame>
  );
}
