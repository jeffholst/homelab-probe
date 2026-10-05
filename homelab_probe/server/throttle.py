"""Slowing down password guessing without letting anyone lock a user out.

Failed logins are counted per **source address** and per **username**. The first few failures cost nothing (a typo is
not an attack); after that each failure doubles the wait before the next attempt is looked at, up to a cap. While a
key is waiting, an attempt gets "too many attempts, try again in N seconds" **whatever password it carries**, so the
answer teaches nothing. The wait is never permanent: it ends by itself, and a username can be held for at most
``USER_CAP`` seconds (an attacker who keeps failing as someone can annoy them for half a minute, not lock them out),
while an address can be held for ``ADDRESS_CAP``. A successful login clears both keys. The table is bounded.
"""

import threading
import time
from collections import OrderedDict
from typing import Callable, Tuple

FREE_FAILURES = 3          # failures before the first wait
BASE_WAIT = 2.0            # seconds after the first failure that counts; doubles each time
ADDRESS_CAP = 300.0
USER_CAP = 30.0
MAX_KEYS = 10_000
FORGET_AFTER = 900.0       # seconds without a failure after which the count starts again


class LoginThrottle:
    def __init__(self, clock: Callable[[], float] = time.monotonic, max_keys: int = MAX_KEYS) -> None:
        self._clock, self._max = clock, max_keys
        self._failures: OrderedDict[Tuple[str, str], Tuple[int, float, float]] = OrderedDict()   # (count, until, last)
        self._lock = threading.Lock()

    def wait(self, address: str, username: str) -> float:
        """Seconds the caller must wait before this attempt may be looked at (0: go ahead)."""
        now = self._clock()
        with self._lock:
            until = max((self._until(("address", address), now), self._until(("user", username.lower()), now)))
        return max(0.0, until - now)

    def failed(self, address: str, username: str) -> None:
        now = self._clock()
        with self._lock:
            self._add(("address", address), now, ADDRESS_CAP)
            self._add(("user", username.lower()), now, USER_CAP)

    def succeeded(self, address: str, username: str) -> None:
        with self._lock:
            self._failures.pop(("address", address), None)
            self._failures.pop(("user", username.lower()), None)

    def _until(self, key: Tuple[str, str], now: float) -> float:
        entry = self._failures.get(key)
        return entry[1] if entry is not None else 0.0

    def _add(self, key: Tuple[str, str], now: float, cap: float) -> None:
        count, _, last = self._failures.get(key, (0, 0.0, now))
        count = (count if now - last < FORGET_AFTER else 0) + 1
        extra = count - FREE_FAILURES
        wait = min(cap, BASE_WAIT * 2 ** (extra - 1)) if extra > 0 else 0.0
        self._failures[key] = (count, now + wait, now)
        self._failures.move_to_end(key)
        while len(self._failures) > self._max:
            self._failures.popitem(last=False)
