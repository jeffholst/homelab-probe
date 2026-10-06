"""One small JSON file of local state per site, kept next to that site's snapshots: ``snapshots/<site id>/<name>.json``.

The triage of findings (``triage.py``) and the notes (``notes.py``) are files of this kind, so they have the same
guarantees: **owner-only, written atomically, changed under a lock** (``<name>.json.lock``, which also waits for another
run for a while and then says it is busy), **never read or written through a symbolic link** (the file, its directory
or the directory above), **refused when the file names another site**, and a damaged file is a fixed error, never a
traceback and never a path. Pure standard library.
"""

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Optional, Tuple

from .util import LockTimeout, file_lock

FORMAT_VERSION = 1
LOCK_WAIT_SECONDS = 20.0


class StoreError(Exception):
    """A local file that cannot be used or changed; ``code`` is a fixed word (``invalid``, ``full``, ``unsafe``,
    ``unreadable``, ``locked``, ``another_site``, ``not_found``) and the message never holds a name, a path or a
    secret."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SiteFile:
    """The file ``file_name`` of one site (``directory`` is the site's directory). Subclasses say what an entry looks
    like (``valid_entry``); ``label`` is how the file is called in a message (``triage``, ``notes``)."""

    file_name = ""
    label = "local"

    def __init__(self, directory: Path, site_id: str) -> None:
        self.directory, self.site_id = Path(directory), site_id
        self.path = self.directory / self.file_name

    def valid_entry(self, key: str, entry: Any) -> bool:
        return isinstance(key, str) and isinstance(entry, dict)

    def _refuse_links(self) -> None:
        if any(path.is_symlink() for path in (self.directory.parent, self.directory, self.path)):
            raise StoreError("unsafe", f"The {self.label} file or a directory above it is a symbolic link, which is "
                                       "left alone.")

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        self._refuse_links()
        try:
            with file_lock(self.directory / (self.file_name + ".lock"), LOCK_WAIT_SECONDS):
                yield
        except LockTimeout as error:
            raise StoreError("locked", f"Another change to the {self.label} was in progress too long.") from error
        except OSError as error:
            raise StoreError("unreadable", f"The {self.label} file cannot be used.") from error

    def parse(self, text: str) -> Tuple[Optional[str], Dict[str, Dict[str, Any]]]:
        """(the site the document names, or ``None``, its entries) of the file ``text``; ``StoreError("unreadable")``
        for a document that is damaged. The one reader of the format: ``load`` and the backup check use it."""
        damaged = StoreError("unreadable", f"The {self.label} file is damaged.")
        try:
            document = json.loads(text)
            entries = document["entries"]
            ok = document["version"] == FORMAT_VERSION and isinstance(entries, dict) and all(
                self.valid_entry(key, value) for key, value in entries.items())
            site = document.get("site")
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            raise damaged from error
        if not ok or not isinstance(site, (str, type(None))):
            raise damaged
        loaded: Dict[str, Dict[str, Any]] = entries
        return site, loaded

    def load(self) -> Dict[str, Dict[str, Any]]:
        """The entries by key (empty when there is no file). ``StoreError`` for a file that is damaged or is another
        site's."""
        self._refuse_links()
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeError) as error:
            raise StoreError("unreadable", f"The {self.label} file cannot be read.") from error
        site, entries = self.parse(text)
        if site not in (None, self.site_id):
            raise StoreError("another_site", f"The {self.label} file belongs to another site.")
        return entries

    def save(self, entries: Mapping[str, Mapping[str, Any]]) -> None:
        """Replace the file with ``entries`` in one step (the caller holds the lock)."""
        document = {"version": FORMAT_VERSION, "site": self.site_id, "entries": entries}
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(dir=self.directory, prefix=self.file_name + ".", suffix=".tmp")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(document, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        except OSError as error:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise StoreError("unreadable", f"The {self.label} file cannot be written.") from error
