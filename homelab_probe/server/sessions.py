"""Server-side login sessions.

A session is held in memory only: the browser keeps an opaque random id (32 bytes) in a cookie and the server keeps
just its SHA-256 hash, so nothing that reads the memory, a core dump or a log of the table can replay a login. A
restart ends every session; there is no "remember me".

A session also ends, on its next request, when anything it was created for has changed:

* the **idle timeout** (no request for ``idle`` seconds) or the **absolute lifetime** (``absolute`` seconds since the
  login) has passed;
* its user was deleted or **disabled**, had the **role** changed, or had the **password** changed or reset. The session
  keeps a fingerprint of the stored password hash and the role it was made for, and compares them with the current
  account on every request. The accounts file is re-read only when it changes, so this costs a stat call, and a change
  made by ``hlp web-user`` in another process counts like any other.
"""

import hashlib
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from ..accounts import AccountStore, User

DEFAULT_IDLE = 30 * 60
DEFAULT_ABSOLUTE = 12 * 3600
MAX_PER_USER = 10
MAX_TOTAL = 1000


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class Session:
    key: str                # the SHA-256 of the cookie value
    username: str
    role: str
    fingerprint: str        # of the password hash this login was made against
    csrf: str
    created: float          # on the monotonic clock
    last_seen: float
    address: str


class SessionStore:
    def __init__(self, idle: float = DEFAULT_IDLE, absolute: float = DEFAULT_ABSOLUTE, per_user: int = MAX_PER_USER,
                 total: int = MAX_TOTAL, clock: Callable[[], float] = time.monotonic) -> None:
        self.idle, self.absolute, self.per_user, self.total, self._clock = idle, absolute, per_user, total, clock
        self._sessions: Dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(self, user: User, address: str) -> Tuple[str, Session]:
        """A new session for ``user`` (who just proved their password): the cookie value, and the session. The oldest
        sessions are dropped to stay within the limits."""
        value = secrets.token_urlsafe(32)
        now = self._clock()
        session = Session(_hash(value), user.username, user.role, _hash(user.password_hash),
                          secrets.token_urlsafe(32), now, now, address)
        with self._lock:
            self._sessions[session.key] = session
            self._trim_locked(user.username)
        return value, session

    def renew(self, old: Session, user: User) -> Tuple[str, Session]:
        """After ``user`` changed their own password: end every session of that user and give the caller one new
        session (a new cookie value and CSRF token, the same address and the **same start**, so the change does not
        lengthen the login). Returns the cookie value and the session."""
        value = secrets.token_urlsafe(32)
        now = self._clock()
        session = Session(_hash(value), user.username, user.role, _hash(user.password_hash),
                          secrets.token_urlsafe(32), old.created, now, old.address)
        with self._lock:
            for key in [k for k, s in self._sessions.items() if s.username == user.username]:
                del self._sessions[key]
            self._sessions[session.key] = session
        return value, session

    def lookup(self, value: Optional[str], accounts: AccountStore) -> Optional[Session]:
        """The live session for a cookie value, or None. Touches it (the idle timer) when it is good; removes it when
        it is expired or its account no longer matches."""
        if not value:
            return None
        key = _hash(value)
        with self._lock:
            session = self._sessions.get(key)
        if session is None:
            return None
        user = accounts.get(session.username)         # the file may be read: not under the lock
        with self._lock:
            if self._sessions.get(key) is not session:        # ended (a logout, a limit) while the account was read
                return None
            now = self._clock()
            if (now - session.last_seen > self.idle or now - session.created > self.absolute or user is None
                    or user.disabled or user.role != session.role
                    or _hash(user.password_hash) != session.fingerprint):
                del self._sessions[key]
                return None
            session.last_seen = max(session.last_seen, now)     # a slower request never moves the idle timer back
            return session

    def end(self, session: Session) -> None:
        self._drop(session.key)

    def end_user(self, username: str) -> int:
        """End every session of ``username``; how many there were."""
        with self._lock:
            keys = [k for k, s in self._sessions.items() if s.username == username]
            for key in keys:
                del self._sessions[key]
        return len(keys)

    def end_all(self) -> int:
        """End every session (a restore replaced the accounts); how many there were."""
        with self._lock:
            count = len(self._sessions)
            self._sessions.clear()
        return count

    def count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def remaining(self, session: Session) -> Tuple[int, int]:
        """Seconds until the idle timeout and until the absolute end (the sooner of the two limits applies)."""
        now = self._clock()
        return (max(0, round(self.idle - (now - session.last_seen))),
                max(0, round(self.absolute - (now - session.created))))

    # -- internals ----------------------------------------------------------------------------------------------

    def _drop(self, key: str) -> None:
        with self._lock:
            self._sessions.pop(key, None)

    def _trim_locked(self, username: str) -> None:
        mine: List[Session] = sorted((s for s in self._sessions.values() if s.username == username),
                                     key=lambda s: s.created)
        for old in mine[:-self.per_user] if len(mine) > self.per_user else []:
            del self._sessions[old.key]
        if len(self._sessions) > self.total:
            for old in sorted(self._sessions.values(), key=lambda s: s.created)[:len(self._sessions) - self.total]:
                del self._sessions[old.key]
