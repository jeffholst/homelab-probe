"""Restoring a backup as one recoverable set (issue #226): the plan, the journal, crashes and failures."""

import json
import os
import stat

import pytest
from backup_support import make_data

from homelab_probe import backup, restore
from homelab_probe.backup import BackupError


class Crash(BaseException):
    """A simulated power loss: not an ``Exception``, so no handler of the restore can swallow it."""


@pytest.fixture(autouse=True)
def no_fault():
    restore.FAULT = None
    yield
    restore.FAULT = None


def tree(path):
    """Every file (not the lock files) with its content, by name."""
    return {str(p.relative_to(path)): p.read_bytes() for p in sorted(path.rglob("*"))
            if p.is_file() and not p.name.endswith(".lock")}


def old_and_new(tmp_path):
    """Two data directories: the one that is restored over (``old``) and the one the backup was made from (``new``,
    with other accounts, notes and an extra site)."""
    old = make_data(tmp_path / "old")
    new = make_data(tmp_path / "new")
    (new / "hlp.toml").write_text("[thresholds]\nslow_link_mbps = 7\n")
    (new / ".env").write_text("UNIFI_URL=https://other.example\nUNIFI_API_KEY=other-key-0123456789\n")
    from homelab_probe.notes import NotesStore
    NotesStore(new / "snapshots" / "site-9", "site-9").add("device:AA:BB:CC:00:00:09", "from the backup", "carol", 5.0)
    (new / "snapshots" / "site-1" / "snapshot-20270101-000000Z.json").write_text('{"schema_version": 1}')
    (old / "snapshots" / "site-2").mkdir()
    from homelab_probe.notes import NotesStore as Notes
    Notes(old / "snapshots" / "site-2", "site-2").add("device:AA:BB:CC:00:00:02", "only here", "alice", 1.0)
    (old / "certs" / "controller.pem").write_text("-----BEGIN CERTIFICATE-----\nMIIBcw==\n-----END CERTIFICATE-----\n")
    (new / "certs" / "controller.pem").unlink()
    (new / "certs").rmdir()
    return old, new


def package_of(new, include=("snapshots", "audit")):
    files = backup.collect(new, new / "hlp.toml", include)
    return backup.unpack(backup.pack(files, include, 1_900_000_000.0))


def restore_now(old, package):
    entries = restore.plan(package, old, old / "hlp.toml")
    restore.apply(old, old / "hlp.toml", entries)


ALWAYS = ("hlp.toml", ".env", "users.json", "certs/", "snapshots/site-1/notes.json", "snapshots/site-1/triage.json",
          "snapshots/site-2/", "snapshots/site-9/")


def always_state(root):
    return {n: d for n, d in tree(root).items() if n.startswith(ALWAYS)}


# -- the plan --------------------------------------------------------------------------------------------------

def test_the_plan_replaces_the_set_and_removes_what_the_backup_does_not_have(tmp_path):
    old, new = old_and_new(tmp_path)
    entries = {e.target: e for e in restore.plan(package_of(new), old, old / "hlp.toml")}
    assert entries["@settings"].data == (new / "hlp.toml").read_bytes() and entries["@settings"].existed
    assert entries["users.json"].data == (new / "users.json").read_bytes()
    assert entries["snapshots/site-9/notes.json"].existed is False
    assert entries["certs/controller.pem"].data is None and entries["certs/controller.pem"].existed
    assert entries["snapshots/site-2/notes.json"].data is None
    assert entries["snapshots/site-1/snapshot-20270101-000000Z.json"].existed is False
    imported = [t for t in entries if t.startswith("audit-imports/")]
    assert imported == ["audit-imports/20300317T174640Z/audit.log", "audit-imports/20300317T174640Z/audit.log.1"]
    assert "audit.log" not in entries                       # the present audit log is never a target


def test_a_snapshot_with_the_same_name_is_replaced_and_the_others_stay(tmp_path):
    old, new = old_and_new(tmp_path)
    same = "snapshots/site-1/snapshot-20260101-000000Z.json"
    (new / same).write_text('{"schema_version": 1, "n": 2}')
    entries = {e.target: e for e in restore.plan(package_of(new), old, old / "hlp.toml")}
    assert entries[same].existed and entries[same].data
    (old / "snapshots" / "site-1" / "snapshot-20240101-000000Z.json").write_text("{}")
    restore_now(old, package_of(new))
    assert (old / same).read_text() == '{"schema_version": 1, "n": 2}'
    assert (old / "snapshots/site-1/snapshot-20240101-000000Z.json").exists()


def test_without_the_optional_categories_what_is_there_stays(tmp_path):
    old, new = old_and_new(tmp_path)
    before = {n: d for n, d in tree(old).items() if "snapshot-" in n or n.startswith("audit")}
    restore_now(old, package_of(new, ()))
    assert {n: d for n, d in tree(old).items() if "snapshot-" in n or n.startswith("audit")} == before
    assert not (old / "audit-imports").exists()


def test_an_external_settings_file_is_written_but_never_removed(tmp_path):
    old, new = old_and_new(tmp_path)
    external = tmp_path / "elsewhere" / "named.toml"
    external.parent.mkdir()
    external.write_text("[thresholds]\nslow_link_mbps = 1\n")
    package = package_of(new)
    restore.apply(old, external, restore.plan(package, old, external))
    assert external.read_bytes() == (new / "hlp.toml").read_bytes()
    assert (old / "hlp.toml").read_text() == "[thresholds]\nslow_link_mbps = 100\n"      # the data directory's own stays
    without = backup.unpack(backup.pack({k: v for k, v in package.files.items() if k != "hlp.toml"}, ("snapshots", "audit"), 1.0))
    entries = restore.plan(without, old, external)
    assert "@settings" not in {e.target for e in entries}
    inside = restore.plan(without, old, old / "hlp.toml")
    assert [e for e in inside if e.target == "@settings"][0].data is None


def test_a_link_where_a_file_or_a_directory_would_be_written_refuses_the_plan(tmp_path):
    old, new = old_and_new(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (old / "snapshots" / "site-9").symlink_to(outside)
    with pytest.raises(BackupError) as caught:
        restore.plan(package_of(new), old, old / "hlp.toml")
    assert caught.value.code == "unsafe" and list(outside.iterdir()) == []
    (old / "snapshots" / "site-9").unlink()
    (old / "snapshots" / "site-9").mkdir()
    (old / "snapshots" / "site-9" / "notes.json").mkdir()
    with pytest.raises(BackupError):
        restore.plan(package_of(new), old, old / "hlp.toml")


# -- a restore that works -----------------------------------------------------------------------------------------

def test_a_restore_makes_the_directory_the_backup_and_leaves_nothing_behind(tmp_path):
    old, new = old_and_new(tmp_path)
    audit_before = (old / "audit.log").read_bytes()
    restore_now(old, package_of(new))
    assert always_state(old) == always_state(new)
    assert (old / "audit.log").read_bytes() == audit_before                       # the present history is untouched
    assert (old / "audit-imports/20300317T174640Z/audit.log").read_text() == '{"event": "x"}\n'
    leftovers = [n for n in tree(old) if n.endswith((restore.NEW, restore.OLD)) or n == restore.JOURNAL]
    assert leftovers == []
    for name in ("users.json", ".env", "snapshots/site-9/notes.json", "audit-imports/20300317T174640Z/audit.log"):
        assert stat.S_IMODE(os.stat(old / name).st_mode) == 0o600, name
    assert stat.S_IMODE(os.stat(old / "snapshots/site-9").st_mode) == 0o700


def test_a_restore_onto_an_empty_directory_creates_everything(tmp_path):
    _, new = old_and_new(tmp_path)
    fresh = tmp_path / "fresh"
    restore_now(fresh, package_of(new))
    assert always_state(fresh) == always_state(new)


def test_the_restored_files_are_the_ones_the_application_reads(tmp_path):
    from homelab_probe.accounts import AccountStore
    from homelab_probe.notes import NotesStore

    old, new = old_and_new(tmp_path)
    AccountStore(new).add("dave", "admin", "another correct horse")
    package = backup.unpack(backup.pack(backup.collect(new, new / "hlp.toml"), (), 1.0))
    restore_now(old, package)
    assert [u.username for u in AccountStore(old).users()] == ["alice", "bob", "dave"]
    assert NotesStore(old / "snapshots" / "site-9", "site-9").listing()[0]["text"] == "from the backup"


def test_a_journal_a_crash_left_is_dealt_with_first_and_a_damaged_one_refuses_the_restore(tmp_path):
    old, new = crash_with_journal(tmp_path / "left")
    restore_now(old, package_of(new))
    assert always_state(old) == always_state(new) and not (old / restore.JOURNAL).exists()
    other, newer = old_and_new(tmp_path / "damaged")
    (other / restore.JOURNAL).write_text("{}")
    with pytest.raises(BackupError) as caught:
        restore_now(other, package_of(newer))
    assert caught.value.code == "journal_damaged" and always_state(other) != always_state(newer)


# -- crashes ---------------------------------------------------------------------------------------------------

def steps_of(tmp_path):
    seen = []
    old, new = old_and_new(tmp_path / "probe")
    restore.FAULT = seen.append
    restore_now(old, package_of(new))
    restore.FAULT = None
    return seen


def test_the_steps_are_the_journal_every_staged_file_every_install_and_the_cleanup(tmp_path):
    steps = steps_of(tmp_path)
    assert steps[0] == "journal:staging" and "journal:applying" in steps and steps[-1] == "cleaned"
    assert any(s.startswith("staged:") for s in steps) and any(s.startswith("moved:") for s in steps)
    assert any(s.startswith("installed:") for s in steps)


def crash_at(step, nth):
    count = {}

    def fault(name):
        count[name] = count.get(name, 0) + 1
        if name == step and count[name] == nth:
            raise Crash(name)

    return fault


def test_a_crash_at_any_step_leaves_the_old_set_or_the_new_one_after_recovery_never_a_mixture(tmp_path):
    steps = steps_of(tmp_path)
    for position, step in enumerate(steps):
        nth = steps[: position + 1].count(step)
        base = tmp_path / f"crash-{position}"
        old, new = old_and_new(base)
        before, wanted = always_state(old), always_state(new)
        restore.FAULT = crash_at(step, nth)
        with pytest.raises(Crash):
            restore_now(old, package_of(new))
        restore.FAULT = None
        outcome = restore.recover(old, old / "hlp.toml")
        after = always_state(old)
        assert after in (before, wanted), step
        if step.startswith(("journal:staging", "staged:")):
            assert after == before and outcome == "discarded", step
        if step in ("journal:applying",) or step.startswith(("moved:", "installed:")):
            assert after == wanted and outcome == "completed", step
        assert [n for n in tree(old) if n.endswith((restore.NEW, restore.OLD)) or n == restore.JOURNAL] == [], step
        assert restore.recover(old, old / "hlp.toml") is None, step


def test_a_crash_while_recovering_is_recovered_again(tmp_path):
    steps = steps_of(tmp_path)
    middle = next(i for i, s in enumerate(steps) if s.startswith("installed:"))
    old, new = old_and_new(tmp_path / "twice")
    wanted = always_state(new)
    restore.FAULT = crash_at(steps[middle], 1)
    with pytest.raises(Crash):
        restore_now(old, package_of(new))
    for inner in ("installed:2", "installed:4", "cleaned"):
        restore.FAULT = crash_at(inner, 1)
        with contextlib_suppress(Crash):
            restore.recover(old, old / "hlp.toml")
    restore.FAULT = None
    restore.recover(old, old / "hlp.toml")
    assert always_state(old) == wanted and not (old / restore.JOURNAL).exists()


def contextlib_suppress(*errors):
    import contextlib
    return contextlib.suppress(*errors)


def crash_with_journal(tmp_path, step="installed:2"):
    old, new = old_and_new(tmp_path)
    restore.FAULT = crash_at(step, 1)
    with pytest.raises(Crash):
        restore_now(old, package_of(new))
    restore.FAULT = None
    return old, new


def test_a_staged_file_that_was_changed_or_lost_rolls_the_whole_restore_back(tmp_path):
    for damage in ("changed", "lost"):
        old, new = old_and_new(tmp_path / damage)
        before = always_state(old)
        restore.FAULT = crash_at("journal:applying", 1)
        with pytest.raises(Crash):
            restore_now(old, package_of(new))
        restore.FAULT = None
        staged = next(p for p in sorted(old.rglob("*" + restore.NEW)) if p.name == "users.json" + restore.NEW)
        staged.write_bytes(b"tampered") if damage == "changed" else staged.unlink()
        assert restore.recover(old, old / "hlp.toml") == "rolled_back", damage
        assert always_state(old) == before, damage
        assert [n for n in tree(old) if n.endswith((restore.NEW, restore.OLD)) or n == restore.JOURNAL] == []


def test_a_journal_that_is_not_ours_stops_the_start_without_touching_anything(tmp_path):
    old, _ = old_and_new(tmp_path)
    before = tree(old)
    good = {"target": "users.json", "install": False, "existed": True, "sha256": None}
    for text in ("not json", "[]", json.dumps({"version": 2, "state": "applying", "entries": []}),
                 json.dumps({"version": 1, "state": "done", "entries": []}),
                 json.dumps({"version": 1, "state": "applying", "entries": "x"}),
                 json.dumps({"version": 1, "state": "applying", "entries": [{**good, "target": "../../etc/passwd"}]}),
                 json.dumps({"version": 1, "state": "applying", "entries": [{**good, "target": "/etc/passwd"}]}),
                 json.dumps({"version": 1, "state": "applying", "entries": [{**good, "target": "audit.log"}]}),
                 json.dumps({"version": 1, "state": "applying", "entries": [{**good, "install": "yes"}]}),
                 json.dumps({"version": 1, "state": "applying", "entries": [{**good, "sha256": "ab"}]}),
                 json.dumps({"version": 1, "state": "applying", "entries": [{"target": "users.json"}]}),
                 json.dumps({"version": 1, "state": "applying", "entries": [5]})):
        (old / restore.JOURNAL).write_text(text)
        with pytest.raises(BackupError) as caught:
            restore.recover(old, old / "hlp.toml")
        assert caught.value.code == "journal_damaged", text
    (old / restore.JOURNAL).unlink()
    assert tree(old) == before
    (old / restore.JOURNAL).write_bytes(b"\xff\xfe")
    with pytest.raises(BackupError):
        restore.recover(old, old / "hlp.toml")


def test_with_no_journal_there_is_nothing_to_do(tmp_path):
    old, _ = old_and_new(tmp_path)
    assert restore.recover(old, old / "hlp.toml") is None
    assert restore.recover(tmp_path / "missing", tmp_path / "missing" / "hlp.toml") is None


# -- failures while it runs ---------------------------------------------------------------------------------------

def failing_at(step, nth=1):
    count = {}

    def fault(name):
        count[name] = count.get(name, 0) + 1
        if name == step and count[name] == nth:
            raise OSError("disk full")

    return fault


def test_a_failure_at_any_step_puts_the_previous_state_back_and_says_so(tmp_path):
    steps = steps_of(tmp_path)
    for position, step in enumerate(steps):
        if step == "cleaned":
            continue
        old, new = old_and_new(tmp_path / f"fail-{position}")
        before = tree(old)
        restore.FAULT = failing_at(step, steps[: position + 1].count(step))
        with pytest.raises(BackupError) as caught:
            restore_now(old, package_of(new))
        restore.FAULT = None
        assert caught.value.code == "restore_failed" and "disk full" not in str(caught.value), step
        assert tree(old) == before, step
        assert not (old / restore.JOURNAL).exists(), step


def test_a_failure_while_tidying_up_is_not_a_failed_restore_and_the_next_start_finishes_the_tidying(tmp_path):
    old, new = old_and_new(tmp_path)
    restore.FAULT = failing_at("cleaned")
    restore_now(old, package_of(new))
    restore.FAULT = None
    assert always_state(old) == always_state(new) and (old / restore.JOURNAL).exists()
    assert restore.recover(old, old / "hlp.toml") == "completed"
    assert [n for n in tree(old) if n.endswith((restore.NEW, restore.OLD)) or n == restore.JOURNAL] == []


def test_when_even_the_undo_fails_the_restore_is_pending_and_the_next_start_finishes_it(tmp_path, monkeypatch):
    old, new = old_and_new(tmp_path)
    before = always_state(old)
    restore.FAULT = failing_at("installed:3")
    real_rollback = restore._rollback
    monkeypatch.setattr(restore, "_rollback", lambda *a: (_ for _ in ()).throw(OSError("still full")))
    with pytest.raises(BackupError) as caught:
        restore_now(old, package_of(new))
    assert caught.value.code == "restore_pending" and (old / restore.JOURNAL).exists()
    restore.FAULT = None
    monkeypatch.setattr(restore, "_rollback", real_rollback)
    assert restore.recover(old, old / "hlp.toml") == "completed"
    assert always_state(old) == always_state(new) != before


def test_recovery_that_can_do_neither_stops_the_start(tmp_path, monkeypatch):
    old, _ = crash_with_journal(tmp_path)
    monkeypatch.setattr(restore, "_forward", lambda *a: (_ for _ in ()).throw(OSError("x")))
    monkeypatch.setattr(restore, "_rollback", lambda *a: (_ for _ in ()).throw(OSError("y")))
    with pytest.raises(BackupError) as caught:
        restore.recover(old, old / "hlp.toml")
    assert caught.value.code == "restore_pending" and (old / restore.JOURNAL).exists()


def test_a_failure_to_stage_removes_what_was_staged(tmp_path, monkeypatch):
    old, new = old_and_new(tmp_path)
    before = tree(old)
    real = restore._stage
    calls = []

    def stage(path, data):
        calls.append(path)
        if len(calls) == 3:
            raise OSError("no space")
        real(path, data)

    monkeypatch.setattr(restore, "_stage", stage)
    with pytest.raises(BackupError) as caught:
        restore_now(old, package_of(new))
    assert caught.value.code == "restore_failed" and tree(old) == before


def test_a_stale_staged_file_or_a_link_in_its_place_is_replaced_not_followed(tmp_path):
    old, new = old_and_new(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("untouched")
    (old / ("users.json" + restore.NEW)).symlink_to(outside)
    (old / (".env" + restore.NEW)).write_text("stale")
    restore_now(old, package_of(new))
    assert outside.read_text() == "untouched" and (old / ".env").read_bytes() == (new / ".env").read_bytes()


# -- the recovery backups --------------------------------------------------------------------------------------

def test_recovery_backups_are_owner_only_unique_and_listed_oldest_first(tmp_path):
    names = [restore.write_recovery(tmp_path, b"sealed", "20300101-000000Z") for _ in range(3)]
    assert names == ["recovery-20300101-000000Z.hlpbackup", "recovery-20300101-000000Z-1.hlpbackup",
                     "recovery-20300101-000000Z-2.hlpbackup"]
    assert stat.S_IMODE(os.stat(tmp_path / "recovery").st_mode) == 0o700
    assert all(stat.S_IMODE(os.stat(tmp_path / "recovery" / n).st_mode) == 0o600 for n in names)
    restore.write_recovery(tmp_path, b"x", "20290101-000000Z")
    (tmp_path / "recovery" / "notes.txt").write_text("not a recovery backup")
    assert restore.recovery_backups(tmp_path)[0] == "recovery-20290101-000000Z.hlpbackup"
    assert len(restore.recovery_backups(tmp_path)) == 4 and restore.recovery_backups(tmp_path / "none") == []


def test_only_the_newest_three_are_kept_and_never_while_a_restore_is_unfinished(tmp_path):
    for day in range(1, 7):
        restore.write_recovery(tmp_path, b"x", f"2030010{day}-000000Z")
    (tmp_path / restore.JOURNAL).write_text("{}")
    assert restore.prune_recovery(tmp_path) == [] and len(restore.recovery_backups(tmp_path)) == 6
    (tmp_path / restore.JOURNAL).unlink()
    gone = restore.prune_recovery(tmp_path)
    assert gone == [f"recovery-2030010{d}-000000Z.hlpbackup" for d in (1, 2, 3)]
    assert restore.recovery_backups(tmp_path) == [f"recovery-2030010{d}-000000Z.hlpbackup" for d in (4, 5, 6)]
    assert restore.prune_recovery(tmp_path, keep=0) and restore.recovery_backups(tmp_path) == []


def test_a_recovery_backup_that_cannot_be_written_leaves_nothing_and_says_so(tmp_path, monkeypatch):
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "recovery").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(BackupError) as caught:
        restore.write_recovery(tmp_path, b"x", "20300101-000000Z")
    assert caught.value.code == "recovery_failed" and list((tmp_path / "elsewhere").iterdir()) == []
    (tmp_path / "recovery").unlink()
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("io")))
    with pytest.raises(BackupError):
        restore.write_recovery(tmp_path, b"x", "20300101-000000Z")
    monkeypatch.setattr(os, "fsync", real_fsync)
    assert restore.recovery_backups(tmp_path) == []


def test_there_are_at_most_a_thousand_names_for_one_second(tmp_path, monkeypatch):
    real_open = os.open

    def taken(path, *args, **kwargs):
        if str(path).endswith(".hlpbackup"):
            raise FileExistsError
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", taken)
    with pytest.raises(BackupError) as caught:
        restore.write_recovery(tmp_path, b"x", "20300101-000000Z")
    assert caught.value.code == "recovery_failed"


def test_a_journal_target_must_be_a_name_a_restore_may_touch():
    for target in (5, None, "../x", "/etc/passwd", "audit.log", "audit-imports/../x/audit.log", "users.json.lock"):
        assert restore._target_ok(target) is False, target
    for target in ("@settings", "users.json", "audit-imports/20300317T174640Z/audit.log.1", "certs/controller.pem"):
        assert restore._target_ok(target) is True, target


def test_a_link_between_the_data_directory_and_a_target_or_a_directory_as_a_target_refuses(tmp_path):
    old, _ = old_and_new(tmp_path)
    (tmp_path / "elsewhere").mkdir()
    (old / "snapshots" / "site-5").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(BackupError):
        restore._refuse_links(old, old / "snapshots" / "site-5" / "notes.json")
    with pytest.raises(BackupError):
        restore._refuse_links(old, old / "snapshots")
    restore._refuse_links(old, old / "snapshots" / "site-1" / "notes.json")


def test_a_journal_that_cannot_be_written_leaves_nothing_behind(tmp_path, monkeypatch):
    old, new = old_and_new(tmp_path)
    before = tree(old)
    monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(OSError("read-only file system")))
    with pytest.raises(BackupError) as caught:
        restore_now(old, package_of(new))
    assert caught.value.code == "restore_failed" and tree(old) == before
    assert not [p for p in old.iterdir() if p.name.startswith(restore.JOURNAL)]


# -- review: a staged file that was replaced by a link ----------------------------------------------------------------

def test_a_staged_file_that_became_a_link_is_never_moved_into_place(tmp_path):
    old, new = old_and_new(tmp_path)
    before = always_state(old)
    restore.FAULT = crash_at("journal:applying", 1)
    with pytest.raises(Crash):
        restore_now(old, package_of(new))
    restore.FAULT = None
    staged = old / ("users.json" + restore.NEW)
    outside = tmp_path / "outside.json"
    outside.write_bytes(staged.read_bytes())                    # the very bytes the journal expects
    staged.unlink()
    staged.symlink_to(outside)
    assert restore.recover(old, old / "hlp.toml") == "rolled_back"
    assert not (old / "users.json").is_symlink() and always_state(old) == before
    assert outside.exists() and not os.path.lexists(staged)


def test_an_installed_file_that_was_replaced_by_a_link_is_not_taken_for_installed(tmp_path):
    old, new = old_and_new(tmp_path)
    before = always_state(old)
    restore.FAULT = crash_at("installed:2", 1)
    with pytest.raises(Crash):
        restore_now(old, package_of(new))
    restore.FAULT = None
    entry = restore.plan(package_of(new), old, old / "hlp.toml")[0]
    target = old / entry.target if entry.target != "@settings" else old / "hlp.toml"
    data = target.read_bytes()
    outside = tmp_path / "copy"
    outside.write_bytes(data)
    target.unlink()
    target.symlink_to(outside)
    assert restore.recover(old, old / "hlp.toml") == "rolled_back"
    assert always_state(old) == before


def test_a_staged_path_that_is_not_a_regular_file_is_neither_completed_nor_silently_undone(tmp_path):
    old, new = old_and_new(tmp_path)
    restore.FAULT = crash_at("journal:applying", 1)
    with pytest.raises(Crash):
        restore_now(old, package_of(new))
    restore.FAULT = None
    staged = old / ("users.json" + restore.NEW)
    staged.unlink()
    staged.mkdir()                                     # not a file at all
    with pytest.raises(BackupError) as caught:
        restore.recover(old, old / "hlp.toml")
    assert caught.value.code == "restore_pending" and (old / restore.JOURNAL).exists()
    staged.rmdir()
    assert restore.recover(old, old / "hlp.toml") == "rolled_back"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs named pipes")
def test_a_staged_pipe_is_refused_without_waiting_for_a_writer(tmp_path):
    old, new = old_and_new(tmp_path)
    before = always_state(old)
    restore.FAULT = crash_at("journal:applying", 1)
    with pytest.raises(Crash):
        restore_now(old, package_of(new))
    restore.FAULT = None
    staged = old / ("users.json" + restore.NEW)
    staged.unlink()
    os.mkfifo(staged)
    assert restore.recover(old, old / "hlp.toml") == "rolled_back" and always_state(old) == before
