"""Lightweight notes on findings, devices and clients, kept locally per site (``snapshots/<site id>/notes.json``).

A note is plain text with its author and when it was written and last changed. It belongs to a **subject** that has a
stable identity inside one site, never a display name: a **device** or a **client** by its MAC address (in any
spelling), a **finding** by its id (``triage.finding_id``). A rename, a refresh or a new controller read therefore keeps
the note with the thing it is about, and because the file is one per site (and refuses another site's), a note cannot
reach another site.

Notes are stored and shown as text only: control and invisible characters are removed when they are written (line breaks
are kept), they are never sent anywhere, and nothing here knows how to talk to the controller. The file is the one
mechanism of ``sitefile.SiteFile``: owner-only, atomic, locked, never through a symbolic link.

There are no attachments, no rich text, no assignments, no due dates: one text per note, a list per subject.
"""

import re
import secrets
from typing import Any, Dict, List, Optional

from .sitefile import SiteFile, StoreError
from .util import normalize_mac, safe_output

FILE_NAME = "notes.json"
KINDS = ("finding", "device", "client")
MAX_TEXT = 2000                # characters in one note
MAX_PER_SUBJECT = 200
MAX_NOTES = 5000               # per site: a bound on the file, not one to meet
_MAC = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")
_FINDING = re.compile(r"^[0-9a-f]{16}$")


def subject_key(text: str) -> str:
    """The canonical subject of ``kind:reference``: ``device:AA:BB:CC:DD:EE:FF``, ``client:...`` (the MAC in any
    spelling) or ``finding:<id>``. ``StoreError("invalid")`` for anything else, so a name is never a subject."""
    kind, _, reference = text.strip().partition(":")
    kind = kind.lower()
    if kind in ("device", "client"):
        mac = normalize_mac(reference)
        if _MAC.fullmatch(mac):
            return f"{kind}:{mac}"
    elif kind == "finding" and _FINDING.fullmatch(reference.strip().lower()):
        return f"finding:{reference.strip().lower()}"
    raise StoreError("invalid", "A note belongs to a device or a client (by its MAC address) or to a finding (by its "
                                "id).")


def clean_text(text: str) -> str:
    """The text of a note as it is kept: trimmed, without control or invisible characters (line breaks stay)."""
    cleaned = safe_output(text).strip()
    if not cleaned:
        raise StoreError("invalid", "A note needs some text.")
    if len(cleaned) > MAX_TEXT:
        raise StoreError("invalid", f"A note is limited to {MAX_TEXT} characters.")
    return cleaned


class NotesStore(SiteFile):
    """The notes of one site, by note id."""

    file_name = FILE_NAME
    label = "notes"

    def valid_entry(self, key: str, entry: Any) -> bool:
        return (isinstance(key, str) and isinstance(entry, dict) and isinstance(entry.get("subject"), str)
                and isinstance(entry.get("text"), str))

    def add(self, subject: str, text: str, author: str, now: float) -> Dict[str, Any]:
        """Add a note; returns it (with its ``id``)."""
        key, body = subject_key(subject), clean_text(text)
        with self.locked():
            entries = dict(self.load())
            if len(entries) >= MAX_NOTES:
                raise StoreError("full", "Too many notes are kept; delete some first.")
            if sum(1 for entry in entries.values() if entry["subject"] == key) >= MAX_PER_SUBJECT:
                raise StoreError("full", f"A subject can have at most {MAX_PER_SUBJECT} notes.")
            ident = secrets.token_hex(8)
            while ident in entries:                                  # pragma: no cover  (a collision of 64 random bits)
                ident = secrets.token_hex(8)
            entries[ident] = {"subject": key, "text": body, "author": author, "created_at": now, "modified_at": now,
                              "modified_by": author}
            self.save(entries)
            return {"id": ident, **entries[ident]}

    def edit(self, ident: str, text: str, editor: str, now: float) -> Dict[str, Any]:
        """Change the text of a note; its subject and author stay, and the change is recorded."""
        body = clean_text(text)
        with self.locked():
            entries = dict(self.load())
            if ident not in entries:
                raise StoreError("not_found", "There is no such note.")
            entries[ident] = {**entries[ident], "text": body, "modified_at": now, "modified_by": editor}
            self.save(entries)
            return {"id": ident, **entries[ident]}

    def remove(self, ident: str) -> Dict[str, Any]:
        """Delete a note; returns what was deleted."""
        with self.locked():
            entries = dict(self.load())
            if ident not in entries:
                raise StoreError("not_found", "There is no such note.")
            gone = entries.pop(ident)
            self.save(entries)
            return {"id": ident, **gone}

    def listing(self, subject: Optional[str] = None) -> List[Dict[str, Any]]:
        """The notes, oldest first (by when they were written), optionally only those of one subject."""
        key = subject_key(subject) if subject else None
        entries = self.load()
        found = [{"id": ident, **entry} for ident, entry in entries.items() if key is None or entry["subject"] == key]
        return sorted(found, key=lambda note: (note["created_at"], note["id"]))

    def counts(self) -> Dict[str, int]:
        """How many notes each subject has."""
        counts: Dict[str, int] = {}
        for entry in self.load().values():
            counts[entry["subject"]] = counts.get(entry["subject"], 0) + 1
        return counts
