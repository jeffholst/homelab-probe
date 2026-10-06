"""Restoring a backup (issue #226): what is replaced, the replacement as one recoverable set, and the recovery after a
crash. Standard library only; the encryption and the API are in ``server/``.

**What a restore does.** The settings, the configuration (``.env``), the accounts, the certificates, the notes and the
triage state of the data directory are **replaced as a complete set** by the ones in the backup: a file the backup does
not have is removed (a file outside the data directory, the settings file named with ``--config``, is only ever
written, never removed). The optional ``snapshots`` are added (a file of the same name is replaced, the others stay),
and an absent optional category leaves what is there alone. The ``audit`` history of a backup is written to
``audit-imports/<date of the backup>/`` and never touches the present ``audit.log``, so it stays distinguishable.

**How it is made safe.** Independent atomic file replacements are not an atomic restore, so there is a journal
(``restore-journal.json`` in the data directory):

1. ``plan`` lists every change (an ``Entry`` per file) from the checked backup.
2. The journal is written in the state ``staging`` and every new file is written next to its target as
   ``<name>.restore-new`` (owner-only, flushed to disk). A crash now changes nothing that matters: ``recover``
   deletes the staged files.
3. The journal is rewritten in the state ``applying``. This is the commit point: from here the restore is **rolled
   forward**. For each entry the present file is moved to ``<name>.restore-old`` and the staged file is renamed into
   its place (a rename in one directory is atomic). Each step can be repeated, so a crash at any point leaves a state
   from which ``recover`` finishes the job.
4. The ``.restore-old`` files and the journal are deleted.

``recover`` runs when the server starts, before the settings are read: a journal in ``staging`` is discarded, one in
``applying`` is completed, and when it cannot be completed (a staged file is missing or does not match its checksum, or
a file cannot be written) it is **undone** (each ``.restore-old`` is moved back, each new file that was installed over
nothing is removed). When even that fails the server does not start, with a fixed message, and the journal stays for the
next try. A failure during a live restore is undone the same way before the request answers.

Nothing here puts a name from a backup, a note or a secret in a message: they are the fixed texts of
``backup.MESSAGES``.
"""

import contextlib
import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .backup import BackupError, Package, classify, live_files
from .settings import DEFAULT_FILENAME as SETTINGS_FILE

JOURNAL = "restore-journal.json"
NEW, OLD = ".restore-new", ".restore-old"
SETTINGS = "@settings"                      # the target of the settings file, wherever the server keeps it
IMPORT_DIR = "audit-imports"
RECOVERY_DIR = "recovery"
RECOVERY_KEEP = 3
JOURNAL_VERSION = 1
STATES = ("staging", "applying")
FAULT: Optional[Callable[[str], None]] = None       # a test hook called at every step (it may raise: a simulated crash)
_RECOVERY_NAME = re.compile(r"recovery-(\d{8}-\d{6}Z)(?:-(\d+))?\.hlpbackup")
_IMPORT_NAME = re.compile(rf"{IMPORT_DIR}/[0-9TZ]{{1,20}}/audit\.log(?:\.[0-9]{{1,4}})?")


@dataclass(frozen=True)
class Entry:
    """One change: the ``target`` (a name under the data directory, or ``SETTINGS``), the new content (``None`` removes
    the file) and whether the target exists now."""

    target: str
    data: Optional[bytes]
    existed: bool


def _fault(step: str) -> None:
    if FAULT is not None:
        FAULT(step)


def _target_ok(target: Any) -> bool:
    """Is ``target`` a name a restore may touch? (Checked again when a journal is read back from disk.)"""
    if not isinstance(target, str):
        return False
    return target == SETTINGS or classify(target) not in (None, "audit") or bool(_IMPORT_NAME.fullmatch(target))


def target_path(directory: Path, settings_path: Path, target: str) -> Path:
    """The file a journal ``target`` names."""
    return settings_path if target == SETTINGS else directory / target


def _refuse_links(directory: Path, path: Path) -> None:
    """``BackupError("unsafe")`` when a directory between the data directory and ``path`` is a link (a write there would
    go somewhere else) or ``path`` itself is a directory."""
    if path.is_relative_to(directory):
        walk = path.parent
        while walk != directory:
            if walk.is_symlink():
                raise BackupError("unsafe")
            walk = walk.parent
    if path.is_dir() and not path.is_symlink():
        raise BackupError("unsafe")


def plan(package: Package, directory: Path, settings_path: Path) -> List[Entry]:
    """The changes that restore ``package`` over ``directory`` (see the module text), in a fixed order.
    ``BackupError("unsafe")`` for a link where a file or a directory would be written."""
    live = live_files(directory, settings_path, ())
    external = settings_path != directory / SETTINGS_FILE
    stamp = re.sub(r"[^0-9TZ]", "", str(package.manifest.get("created_at", ""))) or "unknown"
    entries: List[Entry] = []
    for name, data in sorted(package.files.items()):
        category = classify(name)
        if category == "settings":
            target, existed = SETTINGS, name in live
        elif category == "audit":
            target = f"{IMPORT_DIR}/{stamp}/{name}"
            existed = (directory / target).is_file()
        else:
            target = name
            existed = name in live or (category == "snapshots" and (directory / name).is_file())
        _refuse_links(directory, target_path(directory, settings_path, target))
        entries.append(Entry(target, data, existed))
    for name in sorted(live):
        if name in package.files or (classify(name) == "settings" and external):
            continue
        entries.append(Entry(SETTINGS if classify(name) == "settings" else name, None, True))
    return entries


# -- the journal --------------------------------------------------------------------------------------------

def _sync_directory(path: Path) -> None:
    with contextlib.suppress(OSError):
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _write_journal(directory: Path, state: str, records: List[Dict[str, Any]]) -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=JOURNAL + ".", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"version": JOURNAL_VERSION, "state": state, "entries": records}, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, directory / JOURNAL)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    _sync_directory(directory)


def _read_journal(directory: Path) -> Optional[Dict[str, Any]]:
    """The journal, ``None`` when there is none; ``BackupError("journal_damaged")`` when it is not what we wrote."""
    path = directory / JOURNAL
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError):
        raise BackupError("journal_damaged") from None
    try:
        journal = json.loads(text)
        entries = journal["entries"]
        ok = (journal["version"] == JOURNAL_VERSION and journal["state"] in STATES and isinstance(entries, list)
              and all(isinstance(e, dict) and _target_ok(e.get("target")) and isinstance(e.get("install"), bool)
                      and isinstance(e.get("existed"), bool)
                      and (e["sha256"] is None) == (not e["install"]) and (e["sha256"] is None
                                                                            or isinstance(e["sha256"], str))
                      for e in entries))
    except (ValueError, KeyError, TypeError):
        raise BackupError("journal_damaged") from None
    if not ok:
        raise BackupError("journal_damaged")
    result: Dict[str, Any] = journal
    return result


def _sibling(path: Path, suffix: str) -> Path:
    return path.with_name(path.name + suffix)


# -- the steps ----------------------------------------------------------------------------------------------

def _stage(path: Path, data: bytes) -> None:
    """Write ``data`` next to ``path`` as ``<name>.restore-new``: owner-only and on disk before it returns."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staged = _sibling(path, NEW)
    with contextlib.suppress(FileNotFoundError):
        os.unlink(staged)                                       # a leftover (a link is removed, not followed)
    descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _discard_staged(directory: Path, settings_path: Path, records: List[Dict[str, Any]]) -> None:
    for record in records:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(_sibling(target_path(directory, settings_path, record["target"]), NEW))


def _sha(path: Path) -> str:
    """The SHA-256 of a regular file, read without following a link: ``OSError`` for anything else (a staged file that
    was replaced by a link must never be moved into place)."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))   # never wait on a pipe
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OSError("not a regular file")
        return hashlib.sha256(handle.read()).hexdigest()


def _forward(directory: Path, settings_path: Path, records: List[Dict[str, Any]]) -> None:
    """Install every entry that is not installed yet. Each step can be repeated after a crash."""
    for index, record in enumerate(records):
        target = target_path(directory, settings_path, record["target"])
        new, old = _sibling(target, NEW), _sibling(target, OLD)
        if record["install"]:
            if not os.path.lexists(new):
                if not os.path.lexists(target) or _sha(target) != record["sha256"]:
                    raise OSError("a staged file is missing")   # neither staged nor installed: it cannot be completed
                continue                                        # installed before a crash
            if _sha(new) != record["sha256"]:
                raise OSError("a staged file does not match its checksum")
            if os.path.lexists(target) and not old.exists():
                os.replace(target, old)
            _fault(f"moved:{index}")
            os.replace(new, target)
        elif os.path.lexists(target) and not old.exists():
            os.replace(target, old)
        _fault(f"installed:{index}")
    _sync_all(directory, settings_path, records)


def _sync_all(directory: Path, settings_path: Path, records: List[Dict[str, Any]]) -> None:
    for parent in {target_path(directory, settings_path, r["target"]).parent for r in records}:
        _sync_directory(parent)


def _rollback(directory: Path, settings_path: Path, records: List[Dict[str, Any]]) -> None:
    """Put every entry back as it was: an old file returns to its place, a file that was new is removed, a staged file
    that was never installed is deleted."""
    for record in reversed(records):
        target = target_path(directory, settings_path, record["target"])
        new, old = _sibling(target, NEW), _sibling(target, OLD)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(new)
        if old.exists():
            os.replace(old, target)
        elif record["install"] and not record["existed"]:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(target)
    _sync_all(directory, settings_path, records)


def _cleanup(directory: Path, settings_path: Path, records: List[Dict[str, Any]]) -> None:
    for record in records:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(_sibling(target_path(directory, settings_path, record["target"]), OLD))
    _fault("cleaned")
    with contextlib.suppress(FileNotFoundError):
        os.unlink(directory / JOURNAL)
    _sync_directory(directory)


def apply(directory: Path, settings_path: Path, entries: List[Entry]) -> None:
    """Make ``entries`` the state of the data directory, as one set (see the module text). Returns when the restore is
    complete (``recover`` runs first, so a journal an earlier crash left is dealt with, and a damaged one refuses).
    ``BackupError("restore_failed")`` when it could not be made and the previous state was put back,
    ``restore_pending`` when it could not be undone either (the journal stays: ``recover`` finishes it at the next
    start). Once every file is installed the restore has happened: a failure while tidying up is not an error."""
    recover(directory, settings_path)                           # what an earlier crash left, or a damaged journal
    records = [{"target": e.target, "install": e.data is not None, "existed": e.existed,
                "sha256": None if e.data is None else hashlib.sha256(e.data).hexdigest()} for e in entries]
    try:
        _write_journal(directory, "staging", records)
        _fault("journal:staging")
        for index, entry in enumerate(entries):
            if entry.data is not None:
                _stage(target_path(directory, settings_path, entry.target), entry.data)
            _fault(f"staged:{index}")
    except OSError:
        _abandon(directory, settings_path, records)
        raise BackupError("restore_failed") from None
    try:
        _write_journal(directory, "applying", records)
        _fault("journal:applying")
        _forward(directory, settings_path, records)
    except OSError:
        try:
            _rollback(directory, settings_path, records)
            _abandon(directory, settings_path, records)
        except OSError:
            raise BackupError("restore_pending") from None
        raise BackupError("restore_failed") from None
    with contextlib.suppress(OSError):
        _cleanup(directory, settings_path, records)             # every file is installed: what is left is tidied at the
                                                                # next start (the journal stays until then)


def _abandon(directory: Path, settings_path: Path, records: List[Dict[str, Any]]) -> None:
    """Nothing was installed: delete the staged files and the journal."""
    with contextlib.suppress(OSError):
        _discard_staged(directory, settings_path, records)
        os.unlink(directory / JOURNAL)


def recover(directory: Path, settings_path: Path) -> Optional[str]:
    """Finish or undo a restore that a crash interrupted; what happened (``discarded``, ``completed`` or
    ``rolled_back``) or ``None`` when there was nothing to do. ``BackupError``: ``journal_damaged`` for a journal that
    cannot be read, ``restore_pending`` when the state could be neither completed nor undone (the caller must not go on
    as if it were consistent)."""
    journal = _read_journal(directory)
    if journal is None:
        return None
    records = journal["entries"]
    try:
        if journal["state"] == "staging":
            _discard_staged(directory, settings_path, records)
            with contextlib.suppress(FileNotFoundError):
                os.unlink(directory / JOURNAL)
            return "discarded"
        try:
            _forward(directory, settings_path, records)
            _cleanup(directory, settings_path, records)
            return "completed"
        except OSError:
            _rollback(directory, settings_path, records)
            _abandon(directory, settings_path, records)
            return "rolled_back"
    except OSError:
        raise BackupError("restore_pending") from None


# -- the recovery backups -----------------------------------------------------------------------------------

def write_recovery(directory: Path, sealed: bytes, stamp: str) -> str:
    """Save the sealed recovery backup as ``recovery/recovery-<stamp>.hlpbackup`` (owner-only, on disk before it
    returns; a name already taken gets a number) and return its file name. ``BackupError("recovery_failed")`` when it
    cannot be written (and nothing is left behind)."""
    folder = directory / RECOVERY_DIR
    try:
        if folder.is_symlink():
            raise OSError("a link")
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(folder, 0o700)
        for number in range(0, 1000):
            name = f"recovery-{stamp}{f'-{number}' if number else ''}.hlpbackup"
            try:
                descriptor = os.open(folder / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                continue
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(sealed)
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError:
                with contextlib.suppress(OSError):
                    os.unlink(folder / name)
                raise
            _sync_directory(folder)
            return name
        raise OSError("no free name")
    except OSError:
        raise BackupError("recovery_failed") from None


def recovery_backups(directory: Path) -> List[str]:
    """The names of the recovery backups, oldest first."""
    folder = directory / RECOVERY_DIR
    if not folder.is_dir() or folder.is_symlink():
        return []
    found = [(m.group(1), int(m.group(2) or 0), p.name) for p in folder.iterdir()
             if (m := _RECOVERY_NAME.fullmatch(p.name)) and p.is_file() and not p.is_symlink()]
    return [name for _, _, name in sorted(found)]


def prune_recovery(directory: Path, keep: int = RECOVERY_KEEP) -> List[str]:
    """Delete the oldest recovery backups beyond the newest ``keep``, the names that went. Never while a restore is
    unfinished (a journal is there): its recovery backup may be what the owner needs."""
    if (directory / JOURNAL).exists():
        return []
    names = recovery_backups(directory)
    gone = names[:-keep] if keep else names
    for name in gone:
        with contextlib.suppress(OSError):
            os.unlink(directory / RECOVERY_DIR / name)
    return gone
