"""Notes on findings, devices and clients: the store (issue #225)."""

import json
import os
import stat
import threading

import pytest

from homelab_probe import notes, sitefile
from homelab_probe.notes import NotesStore, clean_text, subject_key
from homelab_probe.sitefile import StoreError
from homelab_probe.triage import finding_id

NOW = 1_800_000_000.0
MAC = "aa:bb:cc:00:00:01"


@pytest.fixture
def store(tmp_path):
    return NotesStore(tmp_path / "snapshots" / "site-1", "site-1")


# -- the subject ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, key", [
    ("device:aa:bb:cc:00:00:01", "device:AA:BB:CC:00:00:01"), ("device:AA-BB-CC-00-00-01", "device:AA:BB:CC:00:00:01"),
    (" Client:aabb.cc00.0001 ", "client:AA:BB:CC:00:00:01"), ("finding:0123456789ABCDEF", "finding:0123456789abcdef"),
])
def test_a_subject_is_a_kind_and_a_stable_reference_in_any_spelling(text, key):
    assert subject_key(text) == key


@pytest.mark.parametrize("text", ["", "device", "device:", "device:Garage AP", "device:aa:bb", "client:zz:zz:zz:zz:zz:zz",
                                  "finding:xyz", "finding:0123", "network:default", "Garage AP", "device:" + "a" * 100])
def test_a_name_or_anything_else_is_never_a_subject(text):
    with pytest.raises(StoreError) as caught:
        subject_key(text)
    assert caught.value.code == "invalid"


def test_a_finding_note_uses_the_id_of_the_findings_list():
    ident = finding_id("device.offline", "Garage AP", MAC)
    assert subject_key(f"finding:{ident}") == f"finding:{ident}"


# -- the text ---------------------------------------------------------------------------------------------------

def test_the_text_keeps_its_line_breaks_and_loses_control_and_invisible_characters():
    assert clean_text("  line one\r\nline\ttwo\x1b[31m \u202egone\u200b  ") == "line one\nline two[31m gone"


@pytest.mark.parametrize("text", ["", "   ", "\x1b\x07", "\u200b\u202e"])
def test_a_note_needs_some_text(text):
    with pytest.raises(StoreError) as caught:
        clean_text(text)
    assert caught.value.code == "invalid"


def test_a_note_is_limited_in_length():
    assert len(clean_text("x" * notes.MAX_TEXT)) == notes.MAX_TEXT
    with pytest.raises(StoreError):
        clean_text("x" * (notes.MAX_TEXT + 1))


# -- the store --------------------------------------------------------------------------------------------------

def test_a_note_has_an_author_and_when_it_was_written_and_changed(store):
    note = store.add("device:" + MAC, "moved to the shelf", "alice", NOW)
    assert len(note["id"]) == 16 and note["author"] == "alice" == note["modified_by"]
    assert note["created_at"] == note["modified_at"] == NOW and note["subject"] == "device:AA:BB:CC:00:00:01"
    edited = store.edit(note["id"], "moved to the rack", "bob", NOW + 60, note["revision"])
    assert (edited["text"], edited["author"], edited["created_at"], edited["modified_at"], edited["modified_by"]) == (
        "moved to the rack", "alice", NOW, NOW + 60, "bob")
    assert store.listing()[0]["text"] == "moved to the rack"


def test_notes_are_listed_oldest_first_and_by_subject_in_any_spelling_of_the_mac(store):
    store.add("device:" + MAC, "first", "alice", NOW)
    store.add("client:11:22:33:44:55:66", "other", "alice", NOW + 1)
    store.add("device:AA-BB-CC-00-00-01", "second", "bob", NOW + 2)
    assert [n["text"] for n in store.listing()] == ["first", "other", "second"]
    assert [n["text"] for n in store.listing("device:aabb.cc00.0001")] == ["first", "second"]
    assert store.listing("client:11:22:33:44:55:66")[0]["text"] == "other" and store.listing("device:00:00:00:00:00:01") == []
    assert store.counts() == {"device:AA:BB:CC:00:00:01": 2, "client:11:22:33:44:55:66": 1}


def test_a_note_follows_the_device_when_it_is_renamed_because_the_subject_is_the_mac(store):
    store.add("device:" + MAC, "belongs to the garage", "alice", NOW)
    # the device is now called something else on the controller: nothing about the note refers to a name
    assert [n["text"] for n in store.listing("device:" + MAC)] == ["belongs to the garage"]
    assert "Garage" not in store.path.read_text()


def test_a_note_can_be_deleted_and_says_what_it_was(store):
    note = store.add("device:" + MAC, "x", "alice", NOW)
    gone = store.remove(note["id"])
    assert gone["text"] == "x" and store.listing() == []
    for call in (lambda: store.remove(note["id"]), lambda: store.edit(note["id"], "y", "alice", NOW, "0" * 16)):
        with pytest.raises(StoreError) as caught:
            call()
        assert caught.value.code == "not_found"


def test_the_file_is_owner_only_names_its_site_and_survives_a_restart(store, tmp_path):
    store.add("device:" + MAC, "kept", "alice", NOW)
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600 and json.loads(store.path.read_text())["site"] == "site-1"
    assert NotesStore(tmp_path / "snapshots" / "site-1", "site-1").listing()[0]["text"] == "kept"


def test_a_note_cannot_reach_another_site(store, tmp_path):
    store.add("device:" + MAC, "site one only", "alice", NOW)
    other = NotesStore(store.directory, "site-2")
    with pytest.raises(StoreError) as caught:
        other.listing()
    assert caught.value.code == "another_site"
    elsewhere = NotesStore(tmp_path / "snapshots" / "site-2", "site-2")
    assert elsewhere.listing() == [] and elsewhere.counts() == {}


def test_the_number_of_notes_is_bounded_per_subject_and_per_site(store, monkeypatch):
    monkeypatch.setattr(notes, "MAX_PER_SUBJECT", 2)
    monkeypatch.setattr(notes, "MAX_NOTES", 3)
    store.add("device:" + MAC, "1", "alice", NOW)
    store.add("device:" + MAC, "2", "alice", NOW)
    with pytest.raises(StoreError) as per_subject:
        store.add("device:" + MAC, "3", "alice", NOW)
    assert per_subject.value.code == "full"
    store.add("client:11:22:33:44:55:66", "4", "alice", NOW)
    with pytest.raises(StoreError) as per_site:
        store.add("client:11:22:33:44:55:77", "5", "alice", NOW)
    assert per_site.value.code == "full" and len(store.listing()) == 3


def test_a_bad_subject_or_text_writes_nothing(store):
    for subject, text in (("Garage AP", "x"), ("device:" + MAC, ""), ("device:" + MAC, "x" * 5000)):
        with pytest.raises(StoreError):
            store.add(subject, text, "alice", NOW)
    assert not store.path.exists()


def test_a_damaged_file_and_a_symbolic_link_are_fixed_errors(store, tmp_path):
    store.directory.mkdir(parents=True)
    for text in ("not json", "[]", '{"version": 2, "entries": {}}', '{"version": 1, "entries": {"a": {"text": 1}}}',
                 '{"version": 1, "entries": {"a": []}}'):
        store.path.write_text(text)
        with pytest.raises(StoreError) as caught:
            store.listing()
        assert caught.value.code == "unreadable" and str(tmp_path) not in str(caught.value)
    store.path.unlink()
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "snapshots" / "site-1").rmdir()
    (tmp_path / "snapshots" / "site-1").symlink_to(tmp_path / "elsewhere")
    for call in (store.listing, lambda: store.add("device:" + MAC, "x", "alice", NOW)):
        with pytest.raises(StoreError) as linked:
            call()
        assert linked.value.code == "unsafe"
    assert list((tmp_path / "elsewhere").iterdir()) == []


def test_concurrent_additions_all_survive(store):
    threads = [threading.Thread(target=store.add, args=("device:" + MAC, f"note {i}", "alice", NOW + i))
               for i in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(store.listing()) == 10


def test_a_busy_lock_is_busy_not_lost(store, monkeypatch):
    from homelab_probe.util import file_lock

    monkeypatch.setattr(sitefile, "LOCK_WAIT_SECONDS", 0.2)
    with file_lock(store.directory / "notes.json.lock"):
        with pytest.raises(StoreError) as caught:
            store.add("device:" + MAC, "x", "alice", NOW)
    assert caught.value.code == "locked"


def test_the_base_of_the_site_files_accepts_any_object_under_a_text_key(tmp_path):
    base = sitefile.SiteFile(tmp_path, "site-1")
    assert base.valid_entry("a", {}) and not base.valid_entry("a", []) and not base.valid_entry(1, {})


# -- review: limits, revisions, the shape of the file, retained subjects ---------------------------------------------

def test_a_note_of_the_documented_maximum_is_kept_and_one_more_character_is_refused(store):
    assert notes.MAX_TEXT == 4000
    assert len(store.add("device:" + MAC, "x" * 4000, "alice", NOW)["text"]) == 4000
    with pytest.raises(StoreError):
        store.add("device:" + MAC, "x" * 4001, "alice", NOW)


def test_a_stale_edit_is_refused_and_keeps_the_change_that_was_saved_first(store):
    note = store.add("device:" + MAC, "first", "alice", NOW)
    first = store.edit(note["id"], "alice's change", "alice", NOW + 1, note["revision"])
    with pytest.raises(StoreError) as caught:
        store.edit(note["id"], "bob's change", "bob", NOW + 2, note["revision"])      # bob still holds the old revision
    assert caught.value.code == "conflict"
    assert store.listing()[0]["text"] == "alice's change" and store.listing()[0]["modified_by"] == "alice"
    assert first["revision"] != note["revision"]
    store.edit(note["id"], "bob's change", "bob", NOW + 3, first["revision"])         # after a reload it goes through
    assert store.listing()[0]["text"] == "bob's change"


def test_the_revision_changes_with_every_change_even_to_the_same_text(store):
    note = store.add("device:" + MAC, "same", "alice", NOW)
    again = store.edit(note["id"], "same", "alice", NOW + 1, note["revision"])
    assert again["revision"] != note["revision"] and again["revision"] == store.listing()[0]["revision"]


def test_a_note_file_is_checked_in_every_part_before_a_reader_uses_it(store):
    good = {"subject": "device:" + MAC.upper(), "text": "x", "author": "alice", "created_at": NOW, "modified_at": NOW,
            "modified_by": "alice"}
    store.directory.mkdir(parents=True)

    def attempt(key, entry):
        store.path.write_text(json.dumps({"version": 1, "site": "site-1", "entries": {key: entry}}))
        return store.load()

    assert attempt("a" * 16, good) and attempt("b" * 16, {**good, "context": {"name": "Garage", "at": NOW}})
    bad = [("short", good), ("A" * 16, good), ("a" * 16, [])]
    bad += [("a" * 16, {**good, **{field: value}}) for field, value in (
        ("subject", "Garage AP"), ("subject", "device:aa:bb:cc:00:00:01"), ("subject", 5), ("text", ""), ("text", 3),
        ("author", None), ("modified_by", 1), ("created_at", "x"), ("modified_at", True), ("created_at", float("nan")),
        ("context", "x"), ("context", {"name": 1, "at": NOW}), ("context", {"name": "x", "at": "y"}))]
    bad += [("a" * 16, {k: v for k, v in good.items() if k != missing}) for missing in good]
    for key, entry in bad:
        with pytest.raises(StoreError) as caught:
            attempt(key, entry)
        assert caught.value.code == "unreadable", (key, entry)


def test_the_last_known_name_is_kept_with_the_note_and_is_not_a_subject(store):
    store.add("device:" + MAC, "moved", "alice", NOW, name="  Garage AP ")
    assert store.listing()[0]["context"] == {"name": "Garage AP", "at": NOW}
    with pytest.raises(StoreError):
        store.add("device:" + MAC, "x", "alice", NOW, name="n" * 121)
    note = store.add("device:" + MAC, "again", "alice", NOW + 5)
    assert "context" not in note
    renamed = store.edit(note["id"], "again", "alice", NOW + 6, note["revision"], name="Workshop AP")
    assert renamed["context"] == {"name": "Workshop AP", "at": NOW + 6} and store.counts() == {"device:" + MAC.upper(): 2}


def test_every_subject_with_notes_is_listed_with_its_last_known_name_whether_or_not_it_exists_now(store):
    store.add("device:" + MAC, "one", "alice", NOW, name="Garage AP")
    store.add("device:" + MAC, "two", "alice", NOW + 10, name="Workshop AP")             # renamed since
    store.add("client:11:22:33:44:55:66", "left", "alice", NOW + 20)
    ident = finding_id("device.offline", "Garage AP", MAC)
    store.add(f"finding:{ident}", "cleared", "alice", NOW + 30, name="Garage AP offline")
    found = store.subjects()
    assert [s["subject"] for s in found] == [f"finding:{ident}", "client:11:22:33:44:55:66", "device:" + MAC.upper()]
    by = {s["subject"]: s for s in found}
    assert by["device:" + MAC.upper()]["last_known"] == {"name": "Workshop AP", "recorded_at": NOW + 10}
    assert by["device:" + MAC.upper()]["note_count"] == 2 and by["device:" + MAC.upper()]["kind"] == "device"
    assert by["client:11:22:33:44:55:66"]["last_known"] is None
    assert [s["subject"] for s in store.subjects("workshop")] == ["device:" + MAC.upper()]       # the last-known name
    assert [s["subject"] for s in store.subjects(" 11:22 ")] == ["client:11:22:33:44:55:66"]       # the reference
    assert [s["subject"] for s in store.subjects("OFFLINE")] == [f"finding:{ident}"] and store.subjects("nothing") == []
    for note in store.listing("client:11:22:33:44:55:66"):
        store.remove(note["id"])
    assert "client:11:22:33:44:55:66" not in {s["subject"] for s in store.subjects()}


def test_a_lock_that_is_a_symbolic_link_a_file_without_a_site_and_a_looser_directory(store, tmp_path):
    store.directory.mkdir(parents=True, mode=0o755)
    os.chmod(store.directory, 0o755)
    store.add("device:" + MAC, "x", "alice", NOW)
    assert stat.S_IMODE(os.stat(store.directory).st_mode) == 0o700
    document = json.loads(store.path.read_text())
    document.pop("site")
    store.path.write_text(json.dumps(document))
    with pytest.raises(StoreError) as caught:
        store.listing()
    assert caught.value.code == "another_site"
    document["site"] = "site-1"
    store.path.write_text(json.dumps(document))
    (store.directory / "notes.json.lock").unlink()
    (store.directory / "notes.json.lock").symlink_to(tmp_path / "elsewhere.lock")
    with pytest.raises(StoreError) as linked:
        store.add("device:" + MAC, "y", "alice", NOW)
    assert linked.value.code == "unsafe" and not (tmp_path / "elsewhere.lock").exists()


def test_a_directory_that_cannot_be_closed_is_a_fixed_error(store, monkeypatch):
    real = os.chmod

    def refuse(path, mode, *args, **kwargs):
        if str(path) == str(store.directory):
            raise PermissionError("not yours")
        return real(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "chmod", refuse)
    with pytest.raises(StoreError) as caught:
        store.add("device:" + MAC, "x", "alice", NOW)
    assert caught.value.code == "unreadable" and "not yours" not in str(caught.value)
