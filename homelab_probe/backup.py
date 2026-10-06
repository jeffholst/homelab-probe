"""Application backup: what is in the archive and how it is read and checked (issue #226). Standard library only; the
encryption around the archive is ``server/backup_crypto`` (the ``web`` extra).

**This is not a UniFi controller backup.** It holds the state of this application's data directory and nothing the
controller owns.

The archive is a ZIP of ``manifest.json`` and the files, under the names they have in the data directory:

=============  ========================================================  ==========
Category       Files                                                     Always?
=============  ========================================================  ==========
settings       ``hlp.toml``                                              yes
config         ``.env`` (the controller address, the API key, the        yes
               notification destinations)
accounts       ``users.json``                                            yes
certificates   ``certs/<name>.pem`` (the pinned controller certificate)  yes
notes          ``snapshots/<site key>/notes.json``                       yes
triage         ``snapshots/<site key>/triage.json``                      yes
snapshots      ``snapshots/<site key>/snapshot-*.json`` (and the older   optional
               ``snapshots/snapshot-*.json``)
audit          ``audit.log`` and its rotated files                       optional
=============  ========================================================  ==========

Never included: sessions (they live in memory), caches, lock files, the notification state. A file is read whole
(every writer replaces files atomically) while the locks of the accounts and of the per-site files are held, so the
archive holds one consistent version of each.

The manifest names the archive ``format``, the ``data_format`` (what the files mean: the compatibility range is
``SUPPORTED_DATA_FORMATS``), the ``app_version`` and ``created_at``, the ``categories`` that were chosen and, for every
file, its ``size`` and ``sha256``. An archive is checked completely before anything is used: member names must be ones
the table allows (never a path that leaves the data directory, a link or a directory), sizes are bounded, the manifest
must match the members exactly, and the contents must parse with the readers the application itself uses (and name the
site their directory is named for, so notes cannot cross sites). ``BackupError`` carries a fixed message and a code; it
never holds a passphrase, a name from the backup or a path.
"""

import contextlib
import datetime
import hashlib
import io
import json
import os
import re
import ssl
import stat
import tempfile
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Tuple

from . import __version__
from .accounts import AUDIT_FILE, USERS_FILE, AccountError, parse_users
from .config import DEFAULT_ENV_FILE, ConfigError
from .history import DEFAULT_DIR, is_snapshot_name, parse_snapshot
from .notes import FILE_NAME as NOTES_FILE
from .notes import NotesStore
from .settings import DEFAULT_FILENAME as SETTINGS_FILE
from .settings import load_settings
from .setup import existing_values
from .sitefile import StoreError
from .triage import FILE_NAME as TRIAGE_FILE
from .triage import TriageStore
from .util import LockTimeout, file_lock, site_key

FORMAT = 1                                # the layout of the archive
DATA_FORMAT = 1                           # what the files in it mean
SUPPORTED_DATA_FORMATS = (1,)             # what this version restores (a later format brings a reviewed migration here)
MANIFEST = "manifest.json"
CERT_DIR = "certs"
ALWAYS = ("settings", "config", "accounts", "certificates", "notes", "triage")
OPTIONAL = ("snapshots", "audit")
CATEGORIES = ALWAYS + OPTIONAL
MAX_FILE = 64 * 1024 * 1024               # one file, uncompressed
MAX_TOTAL = 256 * 1024 * 1024             # all of them
MAX_MEMBERS = 20000
LOCK_WAIT_SECONDS = 5.0

MESSAGES = {
    "not_a_backup": "This is not a Homelab Probe backup.",
    "unsupported_format": "This backup was made in a format this version cannot read.",
    "decrypt": "The passphrase is wrong, or the backup was modified or damaged.",
    "not_an_archive": "The backup does not contain a valid archive.",
    "unsafe_member": "The backup holds a file that is not allowed in it.",
    "too_large": "The backup is larger than this version restores.",
    "bad_manifest": "The manifest of the backup is damaged.",
    "mismatch": "A file in the backup does not match its manifest.",
    "unsupported_data_format": "This backup holds data of a version this version cannot restore.",
    "invalid_content": "A file inside the backup is damaged or is not of the expected kind",
    "no_administrator": "The backup has no enabled administrator, so restoring it would lock everybody out.",
    "unsafe": "A file of the data directory is a symbolic link or not a regular file, so no backup was made.",
    "busy": "Another change is in progress; try again in a moment.",
    "recovery_failed": "The recovery backup of the present state could not be made, so nothing was changed.",
    "restore_failed": "The restore did not complete; the previous state was put back.",
    "restore_pending": "The restore did not complete and could not be undone yet; it is finished or undone when the "
                       "server starts.",
    "journal_damaged": "An unfinished restore left a journal that cannot be read; look at restore-journal.json in the "
                       "data directory.",
    "restore_in_progress": "A restore is in progress; try again when it has finished.",
}


class BackupError(Exception):
    """A backup that cannot be made or used; ``code`` is a key of ``MESSAGES`` and the text is fixed. ``category``
    names the category of a file that is not valid (one of ``CATEGORIES``, never a name from the backup)."""

    def __init__(self, code: str, category: str = "") -> None:
        super().__init__(MESSAGES[code] + (f": {category}." if category else ""))
        self.code, self.category = code, category


_SITE = r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,63}"
_RULES: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("settings", re.compile(re.escape(SETTINGS_FILE))),
    ("config", re.compile(re.escape(DEFAULT_ENV_FILE))),
    ("accounts", re.compile(re.escape(USERS_FILE))),
    ("certificates", re.compile(rf"{CERT_DIR}/[A-Za-z0-9][A-Za-z0-9._-]{{0,63}}\.pem")),
    ("notes", re.compile(rf"{DEFAULT_DIR}/{_SITE}/{re.escape(NOTES_FILE)}")),
    ("triage", re.compile(rf"{DEFAULT_DIR}/{_SITE}/{re.escape(TRIAGE_FILE)}")),
    ("snapshots", re.compile(rf"{DEFAULT_DIR}/(?:{_SITE}/)?[A-Za-z0-9._-]{{1,96}}\.json")),
    ("audit", re.compile(rf"{re.escape(AUDIT_FILE)}(?:\.[0-9]{{1,4}})?")),
)


def classify(name: str) -> Optional[str]:
    """The category of an archive member name, or ``None`` when no category allows that name (so a path with ``..``, a
    leading slash, a backslash or any other shape never gets in)."""
    for category, pattern in _RULES:
        if pattern.fullmatch(name):
            if category == "snapshots" and not is_snapshot_name(name.rsplit("/", 1)[-1]):
                return None
            return category
    return None


# -- reading the data directory ------------------------------------------------------------------------------

def _mode(path: Path) -> Optional[int]:
    """The file type and permission bits of ``path`` without following a link, or ``None`` when it is missing;
    ``BackupError("unsafe")`` when it cannot be examined."""
    try:
        return os.lstat(path).st_mode
    except FileNotFoundError:
        return None
    except OSError:
        raise BackupError("unsafe") from None


def _regular(path: Path) -> bool:
    """Is ``path`` a regular file? ``False`` when it is missing; ``BackupError("unsafe")`` for a link or anything else
    (a backup that quietly skipped it would be incomplete)."""
    mode = _mode(path)
    if mode is not None and not stat.S_ISREG(mode):
        raise BackupError("unsafe")
    return mode is not None


def _listed(path: Path) -> Path:
    """``path`` of a directory listing, which must be a regular file (``BackupError("unsafe")`` for a link, a directory
    or one that vanished since the listing)."""
    if not _regular(path):
        raise BackupError("unsafe")
    return path


def _directory(path: Path) -> bool:
    """Is ``path`` a directory? ``False`` when it is missing; ``BackupError("unsafe")`` for a link or a file."""
    mode = _mode(path)
    if mode is not None and not stat.S_ISDIR(mode):
        raise BackupError("unsafe")
    return mode is not None


def live_files(directory: Path, settings_path: Path, include: Iterable[str]) -> Dict[str, Path]:
    """The files of the data directory a backup of ``include`` (the optional categories to add) would hold, by archive
    name. ``settings_path`` is the settings file the server uses (``hlp.toml`` of the directory unless ``--config``
    named another); it goes into the archive as ``hlp.toml``."""
    wanted = set(include)
    found: Dict[str, Path] = {}
    if _regular(settings_path):
        found[SETTINGS_FILE] = settings_path
    for name in (DEFAULT_ENV_FILE, USERS_FILE):
        if _regular(directory / name):
            found[name] = directory / name
    certs = directory / CERT_DIR
    if _directory(certs):
        for path in sorted(certs.iterdir()):
            name = f"{CERT_DIR}/{path.name}"
            if classify(name) == "certificates":
                found[name] = _listed(path)
    base = directory / DEFAULT_DIR
    if _directory(base):
        for path in sorted(base.iterdir()):
            mode = _mode(path)
            if mode is not None and stat.S_ISDIR(mode):
                for inner in sorted(path.iterdir()):
                    name = f"{DEFAULT_DIR}/{path.name}/{inner.name}"
                    category = classify(name)
                    if category in ("notes", "triage") or (category == "snapshots" and "snapshots" in wanted):
                        found[name] = _listed(inner)
            elif "snapshots" in wanted and classify(f"{DEFAULT_DIR}/{path.name}") == "snapshots":
                found[f"{DEFAULT_DIR}/{path.name}"] = _listed(path)
            elif mode is not None and stat.S_ISLNK(mode) and classify(f"{DEFAULT_DIR}/{path.name}/{NOTES_FILE}"):
                raise BackupError("unsafe")             # a link where a site's directory would be: not followed
    if "audit" in wanted:
        found.update(_audit_paths(directory))
    return found


def _read(path: Path, limit: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        raise
    except OSError:
        raise BackupError("unsafe") from None
    with os.fdopen(descriptor, "rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise BackupError("too_large")
    return data


LOCKED_NAMES = (USERS_FILE, NOTES_FILE, TRIAGE_FILE, DEFAULT_ENV_FILE, SETTINGS_FILE)


def lock_paths(paths: Iterable[Path], settings: Optional[Path] = None) -> List[Path]:
    """The lock files of the files among ``paths`` that have a writer who takes one (the accounts, the per-site notes
    and triage files, ``.env`` and the settings file, which is ``settings`` when that is named otherwise), in the one
    order every taker uses (the accounts first, then the rest sorted), so two of them never wait for each other."""
    wanted = []
    for path in paths:
        if path.name in LOCKED_NAMES or (settings is not None and path == settings):
            wanted.append(path.parent / (path.name + ".lock"))
    return sorted(set(wanted), key=lambda p: (p.name != USERS_FILE + ".lock", str(p)))


@contextlib.contextmanager
def locked(paths: Iterable[Path], settings: Optional[Path] = None) -> Iterator[None]:
    """Hold the locks of ``lock_paths(paths, settings)``, the ones the writers take; ``BackupError("busy")`` when one is
    not free in ``LOCK_WAIT_SECONDS``."""
    try:
        with contextlib.ExitStack() as stack:
            for path in lock_paths(paths, settings):
                stack.enter_context(file_lock(path, LOCK_WAIT_SECONDS))
            yield
    except LockTimeout:
        raise BackupError("busy") from None


AUDIT_TRIES = 5


def _audit_paths(directory: Path) -> Dict[str, Path]:
    """The audit log and its rotated files of the data directory, by name."""
    found: Dict[str, Path] = {}
    if _directory(directory):
        for path in sorted(directory.iterdir()):
            if classify(path.name) == "audit":
                found[path.name] = _listed(path)
    return found


def _signature(paths: Dict[str, Path]) -> Tuple[Any, ...]:
    """What identifies the state of the audit files: their names, sizes, times and file numbers (a rotation or an append
    changes it)."""
    return tuple((name, info.st_size, info.st_mtime_ns, info.st_ino)
                 for name, info in ((n, os.lstat(p)) for n, p in sorted(paths.items())))


def _read_audit(directory: Path, total: int) -> Dict[str, bytes]:
    """The audit log and its rotated files as they were at one moment. The writer (``AuditLog``) appends and rotates
    without a lock the reader could share, so the files are read and then checked to be unchanged; a change (an append,
    a rotation) means a read again, and a log that keeps changing is ``busy``."""
    for _ in range(AUDIT_TRIES):
        try:
            paths = _audit_paths(directory)
            before = _signature(paths)
            files = {name: _read(path, MAX_FILE) for name, path in paths.items()}
            if sum(len(data) for data in files.values()) + total > MAX_TOTAL:
                raise BackupError("too_large")
            if _signature(_audit_paths(directory)) == before:
                return files
        except FileNotFoundError:
            continue                                           # a file went in a rotation: read again
    raise BackupError("busy")


def collect(directory: Path, settings_path: Path, include: Iterable[str] = (), lock: bool = True) -> Dict[str, bytes]:
    """The bytes of every file ``live_files`` lists, read under the locks of the files that have writers (see
    ``lock_paths``) so a writer is never half-way through one (``lock=False`` when the caller already holds them); the
    audit files, which their writer does not lock, are read until they are seen unchanged. ``BackupError``: ``busy``
    when a lock is not free in ``LOCK_WAIT_SECONDS`` or the audit log keeps changing, ``unsafe`` for a link,
    ``too_large`` over ``MAX_FILE`` or ``MAX_TOTAL``."""
    wanted = set(include)
    paths = live_files(directory, settings_path, wanted - {"audit"})
    files: Dict[str, bytes] = {}
    total = 0
    with locked(paths.values() if lock else (), settings_path):
        for name, path in paths.items():
            try:
                files[name] = _read(path, MAX_FILE)
            except FileNotFoundError:
                raise BackupError("unsafe") from None                # it was there when it was listed
            total += len(files[name])
            if total > MAX_TOTAL:
                raise BackupError("too_large")
    if "audit" in wanted:
        files.update(_read_audit(directory, total))
    return files


# -- the archive ---------------------------------------------------------------------------------------------

@dataclass
class Package:
    """An archive that passed ``unpack``: its manifest and its files by name."""

    manifest: Dict[str, Any]
    files: Dict[str, bytes]


def _now_text(now: float) -> str:
    return datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pack(files: Mapping[str, bytes], include: Iterable[str], now: float) -> bytes:
    """The archive of ``files`` (``collect``'s answer) with its manifest, for the optional categories in ``include``.
    A file whose name no category allows is a programming error (``BackupError("unsafe_member")``)."""
    chosen = [c for c in CATEGORIES if c in ALWAYS or c in set(include)]
    for name in files:
        if classify(name) not in chosen:
            raise BackupError("unsafe_member")
    manifest = {"format": FORMAT, "data_format": DATA_FORMAT, "app_version": __version__,
                "created_at": _now_text(now), "categories": chosen,
                "files": {name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                          for name, data in sorted(files.items())}}
    stamp = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).timetuple()[:6]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in [(MANIFEST, json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")),
                           *sorted(files.items())]:
            info = zipfile.ZipInfo(name, date_time=(max(stamp[0], 1980), *stamp[1:]))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            archive.writestr(info, data)
    return buffer.getvalue()


def _members(archive: zipfile.ZipFile) -> List[zipfile.ZipInfo]:
    infos = archive.infolist()
    if len(infos) > MAX_MEMBERS or len(infos) < 1:
        raise BackupError("too_large" if infos else "not_an_archive")
    seen = set()
    total = 0
    for info in infos:
        mode = info.external_attr >> 16
        if (info.filename in seen or info.is_dir() or info.flag_bits & 0x1 or stat.S_IFMT(mode) not in (0, stat.S_IFREG)
                or (info.filename != MANIFEST and classify(info.filename) is None)):
            raise BackupError("unsafe_member")
        seen.add(info.filename)
        total += info.file_size
        if info.file_size > MAX_FILE or total > MAX_TOTAL:
            raise BackupError("too_large")
    if MANIFEST not in seen:
        raise BackupError("bad_manifest")
    return infos


def _is_version(value: Any, allowed: Tuple[int, ...]) -> bool:
    """Is ``value`` exactly one of the version integers (``True`` and ``1.0`` equal ``1`` in Python and are not)?"""
    return type(value) is int and value in allowed


def _manifest(raw: bytes) -> Dict[str, Any]:
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise BackupError("bad_manifest") from None
    files, categories = (manifest.get("files"), manifest.get("categories")) if isinstance(manifest, dict) else (0, 0)
    created = manifest.get("created_at") if isinstance(manifest, dict) else None
    if (not isinstance(files, dict) or not isinstance(categories, list) or not isinstance(created, str)
            or not isinstance(manifest.get("app_version"), str)
            or any(c not in CATEGORIES for c in categories) or len(set(categories)) != len(categories)
            or any(c not in categories for c in ALWAYS)
            or not all(isinstance(v, dict) and isinstance(v.get("size"), int) and not isinstance(v.get("size"), bool)
                       and isinstance(v.get("sha256"), str) for v in files.values())):
        raise BackupError("bad_manifest")
    if not _is_version(manifest.get("format"), (FORMAT,)):
        raise BackupError("unsupported_format")
    if not _is_version(manifest.get("data_format"), SUPPORTED_DATA_FORMATS):
        raise BackupError("unsupported_data_format")
    return manifest


def unpack(data: bytes) -> Package:
    """The archive ``data`` checked completely: ``BackupError`` for anything that is not exactly what ``pack`` makes
    (see the module text), before any file is used. Nothing is written anywhere: members are read as bytes, never
    extracted by name."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = _members(archive)
            contents: Dict[str, bytes] = {}
            for info in infos:
                with archive.open(info) as member:
                    contents[info.filename] = member.read()      # never more than the size declared (and bounded) above
    except zipfile.BadZipFile:
        raise BackupError("not_an_archive") from None
    except (zlib.error, NotImplementedError, RuntimeError, EOFError):
        raise BackupError("not_an_archive") from None
    manifest = _manifest(contents.pop(MANIFEST))
    listed = manifest["files"]
    if set(listed) != set(contents):
        raise BackupError("mismatch")
    for name, entry in listed.items():
        if (entry["size"] != len(contents[name]) or entry["sha256"] != hashlib.sha256(contents[name]).hexdigest()
                or classify(name) not in manifest["categories"]):
            raise BackupError("mismatch")
    return Package(manifest, contents)



# -- what is inside, and what a restore would do -------------------------------------------------------------

def _invalid(category: str) -> BackupError:
    return BackupError("invalid_content", category)


def _text(data: bytes, category: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeError:
        raise _invalid(category) from None


def _site_parts(name: str) -> Tuple[str, str]:
    """(the site key, the file name) of a per-site archive name."""
    _, key, file_name = name.split("/")
    return key, file_name


def _check_settings(data: bytes) -> None:
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / SETTINGS_FILE
        path.write_bytes(data)
        try:
            load_settings(path)
        except (ConfigError, OSError, ValueError):
            raise _invalid("settings") from None


def inspect(package: Package) -> Dict[str, Any]:
    """Check the contents of ``package`` with the readers the application uses itself and say what is in it, without
    secrets: the account names and roles (never a hash), the names of the settings in ``.env`` (never a value) and the
    counts of the rest. ``BackupError``: ``invalid_content`` for a file that does not parse or whose per-site file
    names another site than its directory, ``no_administrator`` when no enabled administrator would remain."""
    files = package.files
    summary: Dict[str, Any] = {"users": [], "administrators": 0, "settings_names": [], "notes": 0, "triage": 0,
                               "sites": [], "counts": {c: 0 for c in CATEGORIES}}
    for name, data in files.items():
        category = classify(name) or ""
        summary["counts"][category] += 1
        if category == "settings":
            _check_settings(data)
        elif category == "config":
            summary["settings_names"] = sorted(existing_values(_text(data, category)))
        elif category == "accounts":
            try:
                users = parse_users(_text(data, category), "the accounts")
            except AccountError:
                raise _invalid(category) from None
            summary["users"] = [{"username": u.username, "role": u.role, "disabled": u.disabled} for u in users]
            summary["administrators"] = sum(1 for u in users if u.role == "admin" and not u.disabled)
        elif category == "certificates":
            try:
                der = ssl.PEM_cert_to_DER_cert(_text(data, category).strip())
            except ValueError:
                raise _invalid(category) from None
            if not der.startswith(b"\x30"):                 # a certificate is an ASN.1 SEQUENCE
                raise _invalid(category)
        elif category in ("notes", "triage"):
            key, _ = _site_parts(name)
            store = NotesStore(Path("."), "") if category == "notes" else TriageStore(Path("."), "")
            try:
                site, entries = store.parse(_text(data, category))
            except StoreError:
                raise _invalid(category) from None
            if site is None or site_key(site) != key:
                raise _invalid(category)
            summary[category] += len(entries)
            summary["sites"] = sorted({*summary["sites"], key})
        elif category == "snapshots":
            try:
                parse_snapshot(_text(data, category), "the snapshot")
            except ConfigError:
                raise _invalid(category) from None
        else:
            _text(data, category)
    if not summary["administrators"]:
        raise BackupError("no_administrator")
    return summary


def _version_tuple(text: str) -> Tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", text.split("+")[0].split("-")[0])[:4])


def preview(package: Package, directory: Path, settings_path: Path, environ: Mapping[str, str],
            env_named: bool = False) -> Dict[str, Any]:
    """What restoring ``package`` over the data directory would do, with no secret in it: the date and versions, the
    compatibility, every category with whether it is in the backup, how many files it has and what a restore does to
    the files now there; the accounts that would replace the present ones; the names (never the values) of the
    settings an environment variable keeps overriding; and warnings. ``env_named`` says the server reads its settings
    from a file named with ``--env-file`` or ``HLP_ENV``: the ``.env`` of the data directory is then not used at all, so
    every setting of the backup stays overridden. ``BackupError`` as ``inspect``."""
    summary = inspect(package)
    manifest = package.manifest
    present = live_files(directory, settings_path, OPTIONAL)
    now_counts = {c: sum(1 for n in present if classify(n) == c) for c in CATEGORIES}
    chosen = manifest["categories"]
    actions = {
        "settings": "replace", "config": "replace", "accounts": "replace", "certificates": "replace",
        "notes": "replace", "triage": "replace",
        "snapshots": "add the snapshots of the backup, replacing the files that have the same name; the others stay",
        "audit": "keep the present audit log; the history of the backup is saved beside it as a separate, marked file",
    }
    categories = []
    for category in CATEGORIES:
        included = category in chosen
        categories.append({
            "id": category, "included": included, "files": summary["counts"][category],
            "present_files": now_counts[category],
            "restore": actions[category] if included else
            ("keep what is there" if category in OPTIONAL else "not in the backup"),
        })
    overridden = sorted(name for name in summary["settings_names"]
                        if env_named or environ.get(name, "").strip())
    running = _version_tuple(__version__)
    made_by = _version_tuple(manifest["app_version"])
    warnings = []
    if env_named and summary["settings_names"]:
        warnings.append({"code": "env_file_named",
                         "message": "This server reads its settings from a file named with --env-file or HLP_ENV, so "
                                    "the .env of the backup is restored to the data directory but not used."})
    elif overridden:
        warnings.append({"code": "environment_overrides",
                         "message": "These settings are also set in the environment, which keeps winning over the "
                                    "restored file: " + ", ".join(overridden) + "."})
    if made_by > running:
        warnings.append({"code": "newer_application",
                         "message": "The backup was made by a newer version; its data format is one this version "
                                    "restores."})
    return {
        "created_at": manifest["created_at"], "app_version": manifest["app_version"], "format": manifest["format"],
        "data_format": manifest["data_format"],
        "compatibility": {"compatible": True, "supported_data_formats": list(SUPPORTED_DATA_FORMATS),
                          "running_version": __version__},
        "categories": categories,
        "accounts": {"replace": True, "total": len(summary["users"]), "administrators": summary["administrators"],
                     "users": summary["users"],
                     "message": "These accounts and passwords replace the present ones, every session ends, and "
                                "everybody logs in again with the credentials of the backup."},
        "notes": summary["notes"], "triage": summary["triage"], "sites": len(summary["sites"]),
        "environment_overrides": overridden, "warnings": warnings,
    }
