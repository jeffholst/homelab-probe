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

import hashlib
import math
import re
import secrets
from typing import Any, Dict, List, Optional

from .sitefile import SiteFile, StoreError
from .util import normalize_mac, printable, safe_output

FILE_NAME = "notes.json"
KINDS = ("finding", "device", "client")
MAX_TEXT = 4000                # characters in one note
MAX_NAME = 120                 # characters of the last-known name of a subject
MAX_PER_SUBJECT = 200
MAX_NOTES = 5000               # per site: a bound on the file, not one to meet
_MAC = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")
_FINDING = re.compile(r"^[0-9a-f]{16}$")
_ID = re.compile(r"^[0-9a-f]{16}$")


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


def clean_name(name: Optional[str]) -> str:
    """The last-known name of a subject (what a person saw when writing the note), one line of plain text."""
    cleaned = printable(name or "").strip()
    if len(cleaned) > MAX_NAME:
        raise StoreError("invalid", f"A name is limited to {MAX_NAME} characters.")
    return cleaned


def revision_of(entry: Dict[str, Any]) -> str:
    """An opaque token for the version of a note: it changes with every change, so a second editor who still holds the
    old one is refused (``conflict``) instead of overwriting the first."""
    seen = f"{entry['modified_at']!r}|{entry['modified_by']}|{entry['text']}"
    return hashlib.sha256(seen.encode("utf-8")).hexdigest()[:16]


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _canonical(subject: Any) -> bool:
    if not isinstance(subject, str):
        return False
    try:
        return subject_key(subject) == subject
    except StoreError:
        return False


class NotesStore(SiteFile):
    """The notes of one site, by note id."""

    file_name = FILE_NAME
    label = "notes"

    def valid_entry(self, key: str, entry: Any) -> bool:
        """Is ``entry`` a note as ``add`` writes it? Everything ``listing`` and the API read is checked, so a file
        edited by hand is a fixed error and never an exception in a reader."""
        context = entry.get("context") if isinstance(entry, dict) else None
        return (isinstance(key, str) and _ID.fullmatch(key) is not None and isinstance(entry, dict)
                and _canonical(entry.get("subject")) and isinstance(entry.get("text"), str) and bool(entry["text"])
                and isinstance(entry.get("author"), str) and isinstance(entry.get("modified_by"), str)
                and _number(entry.get("created_at")) and _number(entry.get("modified_at"))
                and (context is None or (isinstance(context, dict) and isinstance(context.get("name"), str)
                                         and _number(context.get("at")))))

    @staticmethod
    def _public(ident: str, entry: Dict[str, Any]) -> Dict[str, Any]:
        return {"id": ident, **entry, "revision": revision_of(entry)}

    def add(self, subject: str, text: str, author: str, now: float, name: str = "") -> Dict[str, Any]:
        """Add a note; returns it (with its ``id`` and ``revision``). ``name`` is what the subject was called when the
        note was written, kept as its last-known context."""
        key, body, label = subject_key(subject), clean_text(text), clean_name(name)
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
            if label:
                entries[ident]["context"] = {"name": label, "at": now}
            self.save(entries)
            return self._public(ident, entries[ident])

    def edit(self, ident: str, text: str, editor: str, now: float, revision: str,
             name: Optional[str] = None) -> Dict[str, Any]:
        """Change the text of a note; its subject and author stay, and the change is recorded. ``revision`` is the one
        the editor read: another change since (``conflict``) refuses the edit and keeps the saved one, so nobody's
        change is lost silently. A ``name`` refreshes the last-known context."""
        body, label = clean_text(text), clean_name(name)
        with self.locked():
            entries = dict(self.load())
            if ident not in entries:
                raise StoreError("not_found", "There is no such note.")
            if revision != revision_of(entries[ident]):
                raise StoreError("conflict", "The note was changed by someone else; reload it and try again.")
            entries[ident] = {**entries[ident], "text": body, "modified_at": now, "modified_by": editor}
            if label:
                entries[ident]["context"] = {"name": label, "at": now}
            self.save(entries)
            return self._public(ident, entries[ident])

    def remove(self, ident: str) -> Dict[str, Any]:
        """Delete a note; returns what was deleted."""
        with self.locked():
            entries = dict(self.load())
            if ident not in entries:
                raise StoreError("not_found", "There is no such note.")
            gone = entries.pop(ident)
            self.save(entries)
            return self._public(ident, gone)

    def listing(self, subject: Optional[str] = None) -> List[Dict[str, Any]]:
        """The notes, oldest first (by when they were written), optionally only those of one subject."""
        key = subject_key(subject) if subject else None
        entries = self.load()
        found = [self._public(ident, entry) for ident, entry in entries.items()
                 if key is None or entry["subject"] == key]
        return sorted(found, key=lambda note: (note["created_at"], note["id"]))

    def counts(self) -> Dict[str, int]:
        """How many notes each subject has."""
        counts: Dict[str, int] = {}
        for entry in self.load().values():
            counts[entry["subject"]] = counts.get(entry["subject"], 0) + 1
        return counts

    def subjects(self, query: str = "") -> List[Dict[str, Any]]:
        """Every subject that has notes, whether or not it is in the inventory now (the file knows nothing of the
        inventory, so a device that is gone, a client that left and a finding that cleared are listed all the same),
        with the **last-known** name the notes were written under and when. Newest note first. ``query`` keeps the
        subjects whose reference or last-known name contains it (case does not matter)."""
        found: Dict[str, Dict[str, Any]] = {}
        for entry in self.load().values():
            item = found.setdefault(entry["subject"], {"subject": entry["subject"],
                                                       "kind": entry["subject"].partition(":")[0], "note_count": 0,
                                                       "last_note_at": 0.0, "last_known": None})
            item["note_count"] += 1
            item["last_note_at"] = max(item["last_note_at"], entry["modified_at"])
            context = entry.get("context")
            if context and (item["last_known"] is None or context["at"] >= item["last_known"]["recorded_at"]):
                item["last_known"] = {"name": context["name"], "recorded_at": context["at"]}
        needle = query.strip().lower()
        shown = [item for item in found.values() if not needle or needle in item["subject"].lower()
                 or needle in ((item["last_known"] or {}).get("name") or "").lower()]
        return sorted(shown, key=lambda item: (-item["last_note_at"], item["subject"]))
