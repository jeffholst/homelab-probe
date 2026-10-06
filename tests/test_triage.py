"""The local triage of findings: identity, the store and the order (issue #224)."""

import json
import os
import stat
import threading

import pytest

from homelab_probe import triage
from homelab_probe.diagnose import CODES, CRITICAL, INFO, WARNING
from homelab_probe.triage import TriageError, TriageStore, finding_id, rank_findings

NOW = 1_800_000_000.0
DAY = 86400.0


def make(severity, code, subject, mac=""):
    return {"severity": severity, "code": code, "subject": subject, "message": f"{subject}: {code}", "mac": mac}


@pytest.fixture
def store(tmp_path):
    return TriageStore(tmp_path / "snapshots" / "site-1", "site-1")


# -- identity ---------------------------------------------------------------------------------------------------

def test_the_id_is_short_stable_and_says_nothing_about_the_network():
    first = finding_id("device.offline", "Garage AP", "aa:bb:cc:00:00:01")
    assert first == finding_id("device.offline", "Garage AP", "aa:bb:cc:00:00:01") and len(first) == 16
    assert all(c in "0123456789abcdef" for c in first) and "garage" not in first


def test_a_device_with_a_mac_keeps_its_id_when_it_is_renamed_and_spelling_of_the_mac_does_not_matter():
    assert finding_id("device.offline", "Garage AP", "AA:BB:CC:00:00:01") == finding_id(
        "device.offline", "Workshop AP", "aa-bb-cc-00-00-01") == finding_id("device.offline", "x", "aabb.cc00.0001")


def test_without_a_mac_the_subject_is_the_identity_ignoring_case_and_spaces():
    assert finding_id("wan.availability", " Internet ") == finding_id("wan.availability", "internet")
    assert finding_id("wan.availability", "Internet") != finding_id("wan.availability", "Backup")


def test_the_code_is_part_of_the_identity():
    assert finding_id("device.offline", "x", "aa:bb:cc:00:00:01") != finding_id("device.overheating", "x",
                                                                              "aa:bb:cc:00:00:01")


# -- the store --------------------------------------------------------------------------------------------------

def test_a_state_is_saved_owner_only_and_holds_no_name_or_mac(store):
    ident = finding_id("device.offline", "Garage AP", "aa:bb:cc:00:00:01")
    store.set_state(ident, "device.offline", "acknowledged", "alice", NOW, note="seen")
    text = store.path.read_text()
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600 and stat.S_IMODE(os.stat(store.directory).st_mode) == 0o700
    assert "Garage" not in text and "aa:bb" not in text and json.loads(text)["site"] == "site-1"
    entry = store.load()[ident]
    assert (entry["state"], entry["by"], entry["note"], entry["code"]) == ("acknowledged", "alice", "seen", "device.offline")


def test_a_state_survives_a_new_store_object_which_is_a_restart(store, tmp_path):
    store.set_state("a" * 16, "wan.availability", "snoozed", "alice", NOW, NOW + 3 * DAY)
    again = TriageStore(tmp_path / "snapshots" / "site-1", "site-1")
    assert again.load()["a" * 16]["state"] == "snoozed"


def test_reopening_keeps_when_it_was_first_seen_and_clears_the_who_and_the_note(store):
    ident = "b" * 16
    store.reconcile({ident: "wan.availability"}, True, NOW - 5 * DAY)
    store.set_state(ident, "wan.availability", "acknowledged", "alice", NOW - DAY, note="ok")
    reopened = store.set_state(ident, "wan.availability", "open", "bob", NOW)
    assert reopened["first_seen_at"] == NOW - 5 * DAY and reopened["by"] == "" and reopened["note"] == ""


@pytest.mark.parametrize("state, until, note", [
    ("done", None, ""), ("snoozed", None, ""), ("snoozed", NOW - 1, ""), ("snoozed", NOW + 400 * DAY, ""),
    ("acknowledged", None, "x" * 201),
])
def test_a_change_that_makes_no_sense_is_refused(store, state, until, note):
    with pytest.raises(TriageError) as caught:
        store.set_state("c" * 16, "wan.availability", state, "alice", NOW, until, note)
    assert caught.value.code == "invalid" and not store.path.exists()


def test_a_note_is_cleaned_like_every_name(store):
    store.set_state("d" * 16, "wan.availability", "acknowledged", "alice", NOW, note="a\x1b[31mred\x07 note")
    assert store.load()["d" * 16]["note"] == "a[31mred note"


def test_the_number_of_tracked_findings_is_bounded(store, monkeypatch):
    monkeypatch.setattr(triage, "MAX_ENTRIES", 2)
    store.set_state("e" * 15 + "1", "wan.availability", "acknowledged", "alice", NOW)
    store.set_state("e" * 15 + "2", "wan.availability", "acknowledged", "alice", NOW)
    with pytest.raises(TriageError) as caught:
        store.set_state("e" * 15 + "3", "wan.availability", "acknowledged", "alice", NOW)
    assert caught.value.code == "full"
    store.set_state("e" * 15 + "1", "wan.availability", "snoozed", "alice", NOW, NOW + DAY)         # an existing one may change
    added = store.reconcile({"e" * 15 + "4": "wan.availability"}, False, NOW)
    assert added["added"] == 0                                                                   # and none is added past it


def test_a_file_of_another_site_or_a_damaged_one_is_refused_with_a_fixed_message(store, tmp_path):
    store.set_state("f" * 16, "wan.availability", "acknowledged", "alice", NOW)
    other = TriageStore(store.directory, "site-2")
    with pytest.raises(TriageError) as caught:
        other.load()
    assert caught.value.code == "another_site" and str(tmp_path) not in str(caught.value)
    for text in ("not json", "[]", '{"version": 9, "entries": {}}', '{"version": 1, "entries": {"x": {"state": "weird"}}}',
                 '{"version": 1, "entries": []}'):
        store.path.write_text(text)
        with pytest.raises(TriageError) as damaged:
            store.load()
        assert damaged.value.code == "unreadable"


def test_an_unreadable_or_unwritable_file_is_a_fixed_error(store, monkeypatch):
    store.directory.mkdir(parents=True)
    store.path.mkdir()                                                              # a directory where the file should be
    with pytest.raises(TriageError) as caught:
        store.load()
    assert caught.value.code == "unreadable"
    store.path.rmdir()

    def broken(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", broken)
    with pytest.raises(TriageError) as written:
        store.set_state("1" * 16, "wan.availability", "acknowledged", "alice", NOW)
    assert written.value.code == "unreadable" and "No space" not in str(written.value)
    assert [p.name for p in store.directory.iterdir() if p.suffix == ".tmp"] == []


def test_a_symbolic_link_is_never_followed(store, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "snapshots").symlink_to(elsewhere)
    for call in (lambda: store.load(), lambda: store.set_state("2" * 16, "wan.availability", "open", "a", NOW),
                 lambda: store.reconcile({}, True, NOW)):
        with pytest.raises(TriageError) as caught:
            call()
        assert caught.value.code == "unsafe"
    assert list(elsewhere.iterdir()) == []


def test_a_change_made_while_another_is_in_progress_waits_and_both_survive(store):
    store.set_state("3" * 16, "wan.availability", "acknowledged", "alice", NOW)
    threads = [threading.Thread(target=store.set_state, args=(f"{i:016x}", "wan.availability", "acknowledged", "alice", NOW))
               for i in range(10, 20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(store.load()) == 11


def test_a_lock_that_is_not_free_in_time_is_busy_not_lost(store, monkeypatch):
    from homelab_probe.util import file_lock

    monkeypatch.setattr(triage, "LOCK_WAIT_SECONDS", 0.2)
    with file_lock(store.directory / "triage.json.lock"):
        with pytest.raises(TriageError) as caught:
            store.set_state("4" * 16, "wan.availability", "acknowledged", "alice", NOW)
    assert caught.value.code == "locked"


# -- reconcile: only a complete read ends an entry --------------------------------------------------------------------

def test_a_finding_that_is_there_is_recorded_with_when_it_was_first_and_last_seen(store):
    store.reconcile({"5" * 16: "wan.availability"}, True, NOW)
    store.reconcile({"5" * 16: "wan.availability"}, True, NOW + DAY)
    entry = store.load()["5" * 16]
    assert (entry["state"], entry["first_seen_at"], entry["last_seen_at"]) == ("open", NOW, NOW + DAY)


def test_a_complete_read_that_no_longer_shows_a_finding_ends_its_entry_acknowledged_or_not(store):
    store.set_state("6" * 16, "wan.availability", "acknowledged", "alice", NOW)
    store.set_state("7" * 16, "device.offline", "snoozed", "alice", NOW, NOW + DAY)
    counts = store.reconcile({}, True, NOW + DAY)
    assert store.load() == {} and counts["dropped"] == 2


def test_a_partial_or_failed_read_never_ends_an_entry(store):
    store.set_state("6" * 16, "wan.availability", "acknowledged", "alice", NOW)
    before = store.load()
    counts = store.reconcile({}, False, NOW + DAY)                                  # nothing found, but the read was partial
    assert store.load() == before and counts["dropped"] == 0


def test_a_snooze_that_has_ended_is_open_again_and_a_returning_finding_is_not_hidden(store):
    store.set_state("8" * 16, "wan.availability", "snoozed", "alice", NOW, NOW + DAY, "later")
    store.reconcile({"8" * 16: "wan.availability"}, True, NOW + 2 * DAY)
    entry = store.load()["8" * 16]
    assert entry["state"] == "open" and entry["until"] is None and entry["note"] == "" and entry["by"] == ""


def test_a_finding_that_cleared_and_came_back_is_a_new_one(store):
    ident = "9" * 16
    store.set_state(ident, "wan.availability", "acknowledged", "alice", NOW)
    store.reconcile({}, True, NOW + DAY)                                           # it cleared in a complete read
    store.reconcile({ident: "wan.availability"}, True, NOW + 2 * DAY)                    # and it is back
    entry = store.load()[ident]
    assert entry["state"] == "open" and entry["first_seen_at"] == NOW + 2 * DAY


# -- the order and its reasons -----------------------------------------------------------------------------------

FINDINGS = [make(INFO, "reservation.offline", "Printer", "aa:00:00:00:00:01"),
            make(WARNING, "device.offline", "Garage AP", "aa:00:00:00:00:02"),
            make(CRITICAL, "wan.availability", "Internet"), make(CRITICAL, "device.overheating", "Gateway", "aa:00:00:00:00:03"),
            make(WARNING, "wifi.weak_signal", "Phone", "aa:00:00:00:00:04")]


def test_findings_are_ordered_by_severity_then_scope_and_the_order_is_always_the_same():
    first = rank_findings(FINDINGS, {}, NOW)
    assert [r.finding["code"] for r in first] == ["wan.availability", "device.overheating", "device.offline", "wifi.weak_signal",
                                                  "reservation.offline"]
    assert [r.rank for r in first] == [1, 2, 3, 4, 5]
    assert [r.id for r in rank_findings(list(reversed(FINDINGS)), {}, NOW)] == [r.id for r in first]


def test_every_finding_says_what_put_it_where_and_what_is_not_known():
    top = rank_findings(FINDINGS, {}, NOW)[0]
    assert top.reasons == ["critical severity", "the check affects the network as a whole"]
    assert top.scope == "network" and top.score == 3 * 100 + 3 * 10
    assert top.limitations == ["How long this has been happening is unknown: it has not been recorded before."]


def test_persistence_is_used_only_where_there_is_a_record():
    ident = finding_id("device.offline", "Garage AP", "aa:00:00:00:00:02")
    entries = {ident: {"state": "open", "first_seen_at": NOW - 10 * DAY}}
    ranked = {r.finding["code"]: r for r in rank_findings(FINDINGS, entries, NOW)}
    offline = ranked["device.offline"]
    assert "first recorded 10 days ago" in offline.reasons and offline.limitations == []
    assert offline.score == 2 * 100 + 2 * 10 + 3 and ranked["wifi.weak_signal"].score == 2 * 100 + 1 * 10
    assert any("unknown" in text for text in ranked["wifi.weak_signal"].limitations)


@pytest.mark.parametrize("days, bucket", [(0.001, 0), (0.1, 1), (1.5, 2), (7, 3), (400, 3)])
def test_how_long_it_has_been_there_counts_in_steps(days, bucket):
    ident = finding_id("wan.availability", "Internet")
    ranked = rank_findings([make(WARNING, "wan.availability", "Internet")], {ident: {"state": "open", "first_seen_at": NOW - days * DAY}}, NOW)
    assert ranked[0].score == 2 * 100 + 3 * 10 + bucket


def test_a_longer_known_finding_ranks_before_a_newer_one_of_the_same_kind():
    old, new = make(WARNING, "device.offline", "A", "aa:00:00:00:00:0a"), make(WARNING, "device.offline", "B", "aa:00:00:00:00:0b")
    entries = {finding_id("device.offline", "B", "aa:00:00:00:00:0b"): {"state": "open", "first_seen_at": NOW - 8 * DAY}}
    assert [r.finding["subject"] for r in rank_findings([old, new], entries, NOW)] == ["B", "A"]


def test_acknowledged_and_snoozed_findings_stay_in_the_list_after_the_open_ones():
    entries = {finding_id("wan.availability", "Internet"): {"state": "acknowledged", "by": "alice", "at": NOW - 60,
                                                         "note": "known", "first_seen_at": NOW - DAY},
               finding_id("device.overheating", "Gateway", "aa:00:00:00:00:03"): {
                   "state": "snoozed", "by": "bob", "at": NOW - 60, "until": NOW + DAY, "first_seen_at": NOW - DAY}}
    ranked = rank_findings(FINDINGS, entries, NOW)
    assert [(r.finding["code"], r.triage["state"]) for r in ranked] == [
        ("device.offline", "open"), ("wifi.weak_signal", "open"), ("reservation.offline", "open"),
        ("wan.availability", "acknowledged"), ("device.overheating", "snoozed")]
    ack = next(r for r in ranked if r.finding["code"] == "wan.availability")
    assert ack.triage["by"] == "alice" and ack.triage["note"] == "known" and "acknowledged by an administrator" in ack.reasons
    snoozed = next(r for r in ranked if r.finding["code"] == "device.overheating")
    assert snoozed.triage["until"].endswith("Z") and "snoozed by an administrator" in snoozed.reasons


def test_a_snooze_that_ended_is_open_without_anyone_writing_anything():
    ident = finding_id("wan.availability", "Internet")
    entries = {ident: {"state": "snoozed", "by": "bob", "at": NOW - DAY, "until": NOW - 1, "first_seen_at": NOW - DAY}}
    assert rank_findings([make(WARNING, "wan.availability", "Internet")], entries, NOW)[0].triage == {
        "state": "open", "by": None, "at": None, "until": None, "note": ""}


def test_a_partial_read_says_so_on_every_finding():
    ranked = rank_findings(FINDINGS, {}, NOW, complete=False)
    assert all(any("partial" in text for text in r.limitations) for r in ranked)


def test_the_scope_comes_from_the_area_of_the_code_for_every_code():
    seen = {code: triage.scope_of(code) for code in CODES}
    assert set(seen.values()) <= set(triage.SCOPE_WEIGHT) and seen["wan.availability"] == "network"
    assert seen["port.errors"] == "link" and seen["wifi.weak_signal"] == "wireless" and seen["event.client_disconnects"] == "events"
    assert triage.scope_of("") == "device" and triage.scope_of("audit.wifi_open") == "device"


def test_the_guidance_is_general_per_area_with_a_docs_link():
    checks, docs = triage.guidance("wan.availability")
    assert checks and "hlp wan" in checks[0] and docs == triage.DOCS
    assert triage.guidance("nonsense")[0] == []


def test_iso_times():
    assert triage.iso(0) == "1970-01-01T00:00:00Z" and triage.iso(None) is None
