"""The one restore that may run at a time, and what the rest of the server does while it does (issue #226).

``Maintenance.begin()`` takes the place (``False`` when a restore is already running). While it is held, a route that
writes a file on this machine answers ``503 restore_in_progress`` (``auth.refuse_if_read_only``), and the scheduler runs
no job; the restore itself holds the file locks the writers of the command line take.
"""

import threading


class Maintenance:
    def __init__(self) -> None:
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        return self._lock.locked()

    def begin(self) -> bool:
        return self._lock.acquire(blocking=False)

    def end(self) -> None:
        self._lock.release()
