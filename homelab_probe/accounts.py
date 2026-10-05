"""Web accounts: who may log in to the web interface, with which role, and the log of changes to them.

Standard library only, so ``hlp web-user`` works without the ``[web]`` extra. Two files live in the data directory
(``--data-dir``, the current directory by default), both readable by the owner only:

* ``users.json``: the accounts. Every write is atomic (a temporary file, then ``os.replace``) and happens under a lock
  file, so a crash never leaves half a file and two writers never lose each other's update; a reader notices when the
  file changed and reads it again.
* ``audit.log``: JSON lines, one per change, size-rotated and never deleted by age. Passwords never reach it.

A password is stored as ``scrypt$N$r$p$salt$hash`` (salt and hash in unpadded URL-safe base64): the parameters travel
with the hash, so they can be raised later and a user's hash is upgraded at their next login. The comparison is
constant-time, and an unknown or disabled user costs as much work as a wrong password, so the answer does not say
which usernames exist. The rule for a password is only its length (12 characters at least, 1024 at most).

The last administrator cannot be deleted, demoted or disabled, on any path through this module.
"""

import base64
import binascii
import contextlib
import hashlib
import hmac
import json
import logging
import logging.handlers
import os
import re
import secrets
import sys
import tempfile
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Protocol, Tuple

from . import logs

if sys.platform == "win32":     # pragma: no cover (the other platforms never take this branch)
    import msvcrt
else:
    import fcntl

USERS_FILE = "users.json"
AUDIT_FILE = "audit.log"
FORMAT_VERSION = 1
ROLES = ("viewer", "admin")
USERNAME_PATTERN = r"[a-z0-9][a-z0-9._@-]{2,63}"
_USERNAME = re.compile(USERNAME_PATTERN)
MIN_PASSWORD, MAX_PASSWORD = 12, 1024        # characters; there are no composition rules
DEFAULT_AUDIT_MB, DEFAULT_AUDIT_FILES = 5, 10
SALT_BYTES, HASH_BYTES = 16, 32

_audit_log = logging.getLogger("homelab_probe.audit")


class AccountError(Exception):
    """An account operation was refused or the accounts file cannot be used; the message says why and never holds a
    password. The subclasses say which kind of refusal it was, so a caller (the web API) never has to read the text."""


class PolicyError(AccountError):
    """A user name, password or role that does not meet the rules."""


class UserExistsError(AccountError):
    """The user name is taken."""


class NoSuchUserError(AccountError):
    """There is no user of that name."""


class LastAdministratorError(AccountError):
    """The change would leave no enabled administrator."""


class AdministratorExistsError(AccountError):
    """The first administrator was asked for, and an enabled administrator exists."""


class AuditWriteError(AccountError):
    """The audit log could not be written, so the account change was not made."""


# -- passwords ----------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ScryptParams:
    n: int      # CPU and memory cost (a power of two)
    r: int      # block size
    p: int      # parallelism


PARAMS = ScryptParams(n=2 ** 16, r=8, p=1)       # 64 MiB and about a tenth of a second; tests lower it
_LIMITS = ScryptParams(n=2 ** 20, r=32, p=16)    # a tampered file must not be able to ask for more than this


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _scrypt(password: str, salt: bytes, params: ScryptParams) -> bytes:
    memory = 128 * params.r * (params.n + params.p + 2) + 2 ** 20
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=params.n, r=params.r, p=params.p, dklen=HASH_BYTES,
                          maxmem=memory)


def hash_password(password: str, params: Optional[ScryptParams] = None) -> str:
    """``scrypt$N$r$p$salt$hash`` with a new random salt."""
    params = params or PARAMS
    salt = secrets.token_bytes(SALT_BYTES)
    return f"scrypt${params.n}${params.r}${params.p}${_b64(salt)}${_b64(_scrypt(password, salt, params))}"


def parse_hash(stored: str) -> Optional[Tuple[ScryptParams, bytes, bytes]]:
    """The parameters, salt and hash of a stored password, or None when it is not one this module wrote (or asks for
    more work than ``_LIMITS`` allows)."""
    parts = stored.split("$") if isinstance(stored, str) else []
    if len(parts) != 6 or parts[0] != "scrypt":
        return None
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt, digest = _unb64(parts[4]), _unb64(parts[5])
    except (ValueError, binascii.Error):
        return None
    params = ScryptParams(n, r, p)
    if (n < 2 or n & (n - 1) or not 1 <= r <= _LIMITS.r or not 1 <= p <= _LIMITS.p or n > _LIMITS.n
            or not salt or len(digest) != HASH_BYTES):
        return None
    return params, salt, digest


def verify_password(password: str, stored: str) -> bool:
    """True when ``password`` is the one ``stored`` was made from. A stored value that cannot be read gives False
    after the same work a good one would cost."""
    parsed = parse_hash(stored)
    if parsed is None:
        _scrypt(password, b"\0" * SALT_BYTES, PARAMS)
        return False
    params, salt, digest = parsed
    return hmac.compare_digest(_scrypt(password, salt, params), digest)


def needs_upgrade(stored: str) -> bool:
    """True when ``stored`` was made with other parameters than the current ones (or cannot be read)."""
    parsed = parse_hash(stored)
    return parsed is None or parsed[0] != PARAMS


def check_password_policy(password: str) -> None:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        raise PolicyError(f"the password must have at least {MIN_PASSWORD} characters")
    if len(password) > MAX_PASSWORD:
        raise PolicyError(f"the password must have at most {MAX_PASSWORD} characters")


# -- the accounts -------------------------------------------------------------------------------------------------

def normalize_username(text: str) -> str:
    """The form in which a username is stored and compared: trimmed and lower case."""
    return str(text).strip().lower()


def check_username(text: str) -> str:
    """The normalized username, or an ``AccountError`` when it is not 3 to 64 letters, digits or ``. _ @ -``."""
    name = normalize_username(text)
    if not _USERNAME.fullmatch(name):
        raise PolicyError("a username has 3 to 64 characters: letters, digits and . _ @ - (it starts with a letter "
                          "or digit), and capitals do not matter")
    return name


def check_role(role: str) -> str:
    if role not in ROLES:
        raise PolicyError(f"the role must be one of {', '.join(ROLES)}")
    return role


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text(data: Dict[str, Any], key: str) -> str:
    return data[key] if isinstance(data.get(key), str) else ""


@dataclass(frozen=True)
class User:
    username: str
    role: str
    password_hash: str = ""
    created_at: str = ""
    last_login: Optional[str] = None
    disabled: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {"username": self.username, "role": self.role, "password": self.password_hash,
                "created_at": self.created_at, "last_login": self.last_login, "disabled": self.disabled}

    @classmethod
    def from_dict(cls, data: Any) -> "User":
        if not isinstance(data, dict):
            raise AccountError("an entry of the accounts file is not an object")
        username, role = data.get("username"), data.get("role")
        if (not isinstance(username, str) or not _USERNAME.fullmatch(username) or role not in ROLES
                or not isinstance(data.get("disabled", False), bool) or not _text(data, "password")):
            raise AccountError("an entry of the accounts file is damaged")
        last_login = data.get("last_login")
        return cls(username, role, _text(data, "password"), _text(data, "created_at"),
                   last_login if isinstance(last_login, str) else None, bool(data.get("disabled", False)))


@dataclass(frozen=True)
class Principal:
    """Who a request acts for: the username, the role and where the identity came from."""

    username: str
    role: str
    source: str


class Authenticator(Protocol):
    """Anything that can say who a username and password belong to. ``LocalAccounts`` is the first; a trusted-proxy
    or OIDC mode would be another, without touching the routes that take a ``Principal``."""

    def authenticate(self, username: str, password: str) -> Optional[Principal]: ...


def _enabled_admins(users: List[User]) -> int:
    return sum(u.role == "admin" and not u.disabled for u in users)


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    """An exclusive lock on ``path`` across processes (and threads), released when the block ends."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if sys.platform == "win32":     # pragma: no cover (the other platforms take the else branch)
            os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)                    # closing releases the lock


class AccountStore:
    """The accounts file of one data directory. Every method that changes it reads the current file under the lock
    first, so a concurrent change by the server or another ``hlp web-user`` is never overwritten."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.path = self.directory / USERS_FILE
        self._lock_path = self.directory / (USERS_FILE + ".lock")
        self._cache: Optional[Tuple[Tuple[int, int, int], List[User]]] = None
        self._cache_lock = threading.Lock()

    # reading

    def users(self) -> List[User]:
        """The accounts, in the order they were added. The file is read again only when it changed."""
        try:
            os.chmod(self.path, 0o600)
            info = self.path.stat()
        except FileNotFoundError:
            return []
        signature = (info.st_mtime_ns, info.st_size, info.st_ino)
        with self._cache_lock:
            if self._cache is not None and self._cache[0] == signature:
                return list(self._cache[1])
        users = self._read()
        with self._cache_lock:
            self._cache = (signature, users)
        return list(users)

    def get(self, username: str) -> Optional[User]:
        name = normalize_username(username)
        return next((u for u in self.users() if u.username == name), None)

    def _read(self) -> List[User]:
        try:
            os.chmod(self.path, 0o600)
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (ValueError, UnicodeError) as e:
            raise AccountError(f"{self.path} is not valid JSON ({type(e).__name__}); restore it from a backup or "
                               "remove it") from e
        entries = data.get("users") if isinstance(data, dict) and data.get("version") == FORMAT_VERSION else None
        if not isinstance(entries, list):
            raise AccountError(f"{self.path} is not an accounts file of format {FORMAT_VERSION}")
        users = [User.from_dict(entry) for entry in entries]
        if len({u.username for u in users}) != len(users):
            raise AccountError(f"{self.path} lists a username twice")
        return users

    # writing

    def _write(self, users: List[User]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        document = {"version": FORMAT_VERSION, "users": [u.to_dict() for u in users]}
        fd, temporary = tempfile.mkstemp(dir=self.directory, prefix=USERS_FILE + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(document, f, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise

    def _save_change(self, before: List[User], after: List[User], result: Any,
                     on_change: Optional[Callable[[Any], None]]) -> Any:
        if after != before:
            self._write(after)
        if on_change is not None:
            try:
                on_change(result)
            except Exception as error:
                if after != before:
                    try:
                        self._write(before)
                    except OSError as rollback_error:
                        raise AuditWriteError(
                            "the audit log could not be written and the account change could not be rolled back"
                        ) from rollback_error
                raise AuditWriteError(
                    "the audit log could not be written; the account change was rolled back") from error
        return result

    def _change(self, change: Any, on_change: Optional[Callable[[Any], None]] = None) -> Any:
        """Run ``change(users) -> (users, result)`` on the current file under the lock and write the outcome. The
        last enabled administrator is protected here, so no operation can bypass it. An audit callback runs under
        the same lock; when it fails, the account change is rolled back."""
        with _locked(self._lock_path):
            before = self._read()
            after, result = change(list(before))
            if _enabled_admins(before) >= 1 and _enabled_admins(after) == 0:
                raise LastAdministratorError("the last administrator cannot be deleted, demoted or disabled")
            return self._save_change(before, after, result, on_change)

    # the operations

    def add(self, username: str, role: str, password: str,
            on_change: Optional[Callable[[User], None]] = None, only_if_no_admin: bool = False) -> User:
        """Add a user. With ``only_if_no_admin`` the account is refused, under the same lock, when an enabled
        administrator exists (the first administrator of the guided setup must not become the second one)."""
        name, role = check_username(username), check_role(role)
        check_password_policy(password)
        hashed = hash_password(password)

        def change(users: List[User]) -> Tuple[List[User], User]:
            if any(u.username == name for u in users):
                raise UserExistsError(f"the user {name} already exists")
            if only_if_no_admin and _enabled_admins(users):
                raise AdministratorExistsError("an administrator exists already")
            user = User(name, role, hashed, _now())
            return users + [user], user

        return self._change(change, on_change)

    def _update(self, username: str, edit: Any, on_change: Optional[Callable[[User], None]] = None) -> User:
        name = normalize_username(username)

        def change(users: List[User]) -> Tuple[List[User], User]:
            for i, user in enumerate(users):
                if user.username == name:
                    users[i] = edit(user)
                    return users, users[i]
            raise NoSuchUserError(f"there is no user {name}")

        return self._change(change, on_change)

    def set_role(self, username: str, role: str, on_change: Optional[Callable[[User], None]] = None) -> User:
        role = check_role(role)
        return self._update(username, lambda user: replace(user, role=role), on_change)

    def set_disabled(self, username: str, disabled: bool,
                     on_change: Optional[Callable[[User], None]] = None) -> User:
        return self._update(username, lambda user: replace(user, disabled=disabled), on_change)

    def update(self, username: str, role: Optional[str] = None, disabled: Optional[bool] = None,
               on_change: Optional[Callable[[User, User], None]] = None) -> User:
        """Change the role and/or the disabled flag of a user in one step (under the lock, with the last-administrator
        rule applied to the result). ``on_change(old, new)`` is the audit callback; it runs only when something
        changed, and when it fails the change is rolled back."""
        role = None if role is None else check_role(role)
        seen: List[User] = []

        def edit(user: User) -> User:
            seen.append(user)
            return replace(user, role=user.role if role is None else role,
                           disabled=user.disabled if disabled is None else disabled)

        def record(user: User) -> None:
            if on_change is not None and seen[0] != user:
                on_change(seen[0], user)

        return self._update(username, edit, record)

    def reset_password(self, username: str, password: str,
                       on_change: Optional[Callable[[User], None]] = None) -> User:
        check_password_policy(password)
        hashed = hash_password(password)
        return self._update(username, lambda user: replace(user, password_hash=hashed), on_change)

    def remove(self, username: str, on_change: Optional[Callable[[User], None]] = None) -> User:
        name = normalize_username(username)

        def change(users: List[User]) -> Tuple[List[User], User]:
            found = next((u for u in users if u.username == name), None)
            if found is None:
                raise NoSuchUserError(f"there is no user {name}")
            return [u for u in users if u is not found], found

        return self._change(change, on_change)

    def authenticate_login(self, username: str, password: str, decoy: str,
                           on_upgrade: Optional[Callable[[User], None]] = None) -> Optional[User]:
        """Verify and record a login against the current account under the file lock."""
        name = normalize_username(username)
        with _locked(self._lock_path):
            before = self._read()
            user = next((candidate for candidate in before if candidate.username == name), None)
            if user is None or user.disabled:
                verify_password(password, decoy)
                return None
            if not verify_password(password, user.password_hash):
                return None
            upgraded = needs_upgrade(user.password_hash)
            current = replace(user, last_login=_now(),
                              password_hash=hash_password(password) if upgraded else user.password_hash)
            after = [current if candidate.username == name else candidate for candidate in before]
            self._save_change(before, after, current, on_upgrade if upgraded else None)
            return current


class LocalAccounts:
    """The accounts of ``users.json`` as an ``Authenticator``."""

    source = "local"

    def __init__(self, store: AccountStore, audit: Optional["AuditLog"] = None) -> None:
        self.store = store
        self.audit = audit
        self._decoy = hash_password(secrets.token_urlsafe(16))      # what an unknown user's password is compared with

    def authenticate_user(self, username: object, password: str) -> Optional[User]:
        """The account of a correct password of an enabled user, as it is at that moment, else None. A wrong
        password, an unknown user and a disabled one take the same work and give the same answer. A correct password
        with an old hash upgrades the hash; every success updates ``last_login``."""
        password = password if isinstance(password, str) and len(password) <= MAX_PASSWORD else ""
        if not isinstance(username, str):
            verify_password(password, self._decoy)
            return None

        def on_upgrade(user: User) -> None:
            if self.audit:
                self.audit.write("user.password_upgraded", user.username, user=user.username)

        return self.store.authenticate_login(username, password, self._decoy, on_upgrade)

    def authenticate(self, username: object, password: str) -> Optional[Principal]:
        """The ``Principal`` of a correct password of an enabled user, else None (see ``authenticate_user``)."""
        user = self.authenticate_user(username, password)
        return Principal(user.username, user.role, self.source) if user is not None else None


# -- the audit log ------------------------------------------------------------------------------------------------

class _PrivateRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """A rotating log file that is created, and re-created after a rotation, readable by the owner only."""

    def _open(self) -> Any:
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        return os.fdopen(fd, self.mode, encoding=self.encoding)

    def handleError(self, record: logging.LogRecord) -> None:
        error = sys.exc_info()[1]
        raise AuditWriteError("could not write the audit log") from error


class _JsonLines(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        moment = datetime.fromtimestamp(record.created, timezone.utc)
        entry = {"time": moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(record.msecs):03d}Z",
                 "event": getattr(record, "audit_event", ""), "actor": getattr(record, "actor", ""),
                 **({"request_id": getattr(record, "request_id", "")} if getattr(record, "request_id", "") else {}),
                 **getattr(record, "fields", {})}
        return json.dumps(entry, ensure_ascii=True)


class AuditLog:
    """``audit.log`` of one data directory: one JSON line per change, rotated by size (``max_mb`` megabytes, ``files``
    files in all, the oldest dropped when a new one is needed, never by age). The same record is also logged at INFO on
    the ``homelab_probe.audit`` logger (``audit.event``). Fields pass the redaction filter, and nothing here is
    given a password."""

    def __init__(self, directory: Path, max_mb: int = DEFAULT_AUDIT_MB, files: int = DEFAULT_AUDIT_FILES) -> None:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / AUDIT_FILE
        self._handler = _PrivateRotatingFileHandler(self.path, maxBytes=max_mb * 1024 * 1024,
                                                    backupCount=max(files - 1, 1), encoding="utf-8", delay=True)
        self._handler.addFilter(logs.RedactFilter())
        self._handler.setFormatter(_JsonLines())
        if self.path.exists():
            os.chmod(self.path, 0o600)          # a file made earlier with looser rights

    def write(self, event: str, actor: str, **fields: Any) -> None:
        """Record ``event`` (``user.added``...) done by ``actor`` with its ``fields``."""
        record = _audit_log.makeRecord(_audit_log.name, logging.INFO, __file__, 0, event, (), None,
                                       extra={"audit_event": event, "actor": actor, "event": "audit.event",
                                              "fields": fields})
        self._handler.handle(record)
        shown = {("account" if key == "user" else key): value for key, value in fields.items()}     # ``user`` is taken
        logs.log_event(_audit_log, logging.INFO, "audit.event", f"Audit: {event} by {actor}.",
                       action=event, actor=actor, **shown)

    def close(self) -> None:
        self._handler.close()

    def __enter__(self) -> "AuditLog":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
