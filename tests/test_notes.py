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
    edited = store.edit(note["id"], "moved to the rack", "bob", NOW + 60)
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
    for call in (lambda: store.remove(note["id"]), lambda: store.edit(note["id"], "y", "alice", NOW)):
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
