"""Restoring a backup over the API (issue #226): replacement as one set, recovery backups, crashes, fresh installs."""

import base64
import json
import os

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("cryptography")

from backup_support import make_data  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from server_support import CONFIG, auth_for, logged_in, login, origin_of  # noqa: E402

from homelab_probe import backup, cli, logs, restore  # noqa: E402
from homelab_probe.accounts import AccountStore  # noqa: E402
from homelab_probe.demo.session import DemoSession  # noqa: E402
from homelab_probe.notes import NotesStore  # noqa: E402
from homelab_probe.server import backup_api, restore_api  # noqa: E402
from homelab_probe.server import backup_crypto as crypto  # noqa: E402
from homelab_probe.server.app import create_app  # noqa: E402
from homelab_probe.server.auth import AuthState  # noqa: E402
from homelab_probe.server.scheduler import Scheduler  # noqa: E402
from homelab_probe.server.service import ControllerService  # noqa: E402
from homelab_probe.server.wizard import MODE_ADMIN, MODE_SETUP, SETUP_ACTOR, SetupState  # noqa: E402

API = "/api/v1/backup"
PASS = "the passphrase of the backup"
RECOVERY = "the recovery passphrase"
TOKEN = "setup-token-0123456789abcdef"
SOURCE_KEY = "other-key-0123456789"
STANDIN = CONFIG.__class__(controller_url="https://unconfigured.invalid", api_key="unconfigured")


class Crash(BaseException):
    pass


@pytest.fixture(autouse=True)
def cheap(monkeypatch):
    monkeypatch.setattr(restore_api, "ControllerService",
                        lambda config: ControllerService(config, session=DemoSession(), ttl=0))
    monkeypatch.setattr(crypto, "SCRYPT_N", 2 ** 10)
    monkeypatch.setattr(backup_api, "CLOCK", lambda: 1_900_000_000.0)
    restore.FAULT = None
    yield
    restore.FAULT = None


def make_app(directory, **kwargs):
    accounts = AuthState.for_directory(directory, CONFIG) if (directory / "users.json").exists() else auth_for(directory)
    return create_app(CONFIG, state_dir=directory, hosts=["testserver"], auth=accounts,
                      service=ControllerService(CONFIG, session=DemoSession(), ttl=0), **kwargs)


def tree(path):
    return {str(p.relative_to(path)): p.read_bytes() for p in sorted(path.rglob("*"))
            if p.is_file() and not p.name.endswith(".lock") and p.name != "audit.log" and "recovery" not in p.parts}


@pytest.fixture
def source(tmp_path):
    """The installation the backup is made from: another account, other notes and settings."""
    directory = tmp_path / "source"
    make_data(directory)
    AccountStore(directory).add("dave", "admin", "dave has a long password")
    (directory / "hlp.toml").write_text("[thresholds]\nslow_link_mbps = 7\n")
    (directory / ".env").write_text(f"UNIFI_URL=https://source.example\nUNIFI_API_KEY={SOURCE_KEY}\n")
    NotesStore(directory / "snapshots" / "site-9", "site-9").add("device:AA:BB:CC:00:00:09", "from the backup", "dave", 1.0)
    return directory


@pytest.fixture
def blob(source):
    admin = logged_in(make_app(source), "alice")
    response = admin.post(API, json={"passphrase": PASS, "confirm": PASS, "include": ["audit", "snapshots"]})
    assert response.status_code == 200
    return response.content


@pytest.fixture
def target(tmp_path):
    directory = make_data(tmp_path / "target")
    (directory / "snapshots" / "site-2").mkdir()
    NotesStore(directory / "snapshots" / "site-2", "site-2").add("device:AA:BB:CC:00:00:02", "only on the target", "alice", 1.0)
    return directory


@pytest.fixture
def app(target):
    return make_app(target)


@pytest.fixture
def admin(app):
    return logged_in(app, "alice")


def body(blob, **extra):
    return {"passphrase": PASS, "archive": base64.b64encode(blob).decode(), "recovery_passphrase": RECOVERY,
            "recovery_confirm": RECOVERY, "confirm": True, **extra}


def audit(directory):
    return [json.loads(line) for line in (directory / "audit.log").read_text().splitlines()]


def always(root):
    """The files a restore replaces as one set (the always categories), with their content."""
    return {n: d for n, d in tree(root).items() if backup.classify(n) in backup.ALWAYS}


# -- a restore over an installation --------------------------------------------------------------------------------

def test_a_restore_replaces_the_set_ends_every_session_and_the_backups_accounts_log_in(admin, app, blob, target, source):
    other = logged_in(app, "bob")
    audit_before = audit(target)
    response = admin.post(f"{API}/restore", json=body(blob))
    assert response.status_code == 200
    answer = response.json()
    assert answer["restored"] is True and answer["sessions_ended"] is True
    assert answer["recovery_backup"].startswith("recovery-20300317-174640Z")
    assert always(target) == always(source)
    assert admin.get("/api/v1/auth/me").status_code == 401 and other.get("/api/v1/auth/me").status_code == 401
    fresh = TestClient(app)
    token = login(fresh, "dave", "dave has a long password")
    fresh.headers.update({"Origin": origin_of(fresh), "X-CSRF-Token": token})
    assert fresh.get("/api/v1/auth/me").json()["username"] == "dave"
    listed = fresh.get("/api/v1/unifi/sites/default/notes").json()["items"]
    assert {n["text"] for n in listed} == {"the secret reason for the note", "another"}
    assert audit(target)[: len(audit_before)] == audit_before                   # the present history is kept


def test_the_present_state_is_kept_in_an_owner_only_recovery_backup_that_opens_with_its_own_passphrase(
        admin, blob, target):
    present = tree(target)
    name = admin.post(f"{API}/restore", json=body(blob)).json()["recovery_backup"]
    path = target / "recovery" / name
    assert oct(os.stat(path).st_mode & 0o777) == "0o600" and oct(os.stat(path.parent).st_mode & 0o777) == "0o700"
    package = backup.unpack(crypto.open_sealed(path.read_bytes(), RECOVERY))
    for secret in (RECOVERY.encode(), PASS.encode()):
        assert secret not in path.read_bytes()
    with pytest.raises(backup.BackupError):
        crypto.open_sealed(path.read_bytes(), PASS)                           # not the passphrase of the backup
    assert {n: d for n, d in package.files.items()} == {
        n: d for n, d in present.items() if backup.classify(n) in backup.ALWAYS}
    assert "snapshots/site-2/notes.json" in package.files


def test_the_audit_says_what_was_restored_and_holds_no_passphrase_or_content(admin, blob, target):
    admin.post(f"{API}/restore", json=body(blob))
    entry = audit(target)[-1]
    assert (entry["event"], entry["actor"]) == ("backup.restored", "alice")
    assert entry["created"] == "2030-03-17T17:46:40Z" and entry["recovery"].startswith("recovery-")
    text = (target / "audit.log").read_text()
    for secret in (PASS, RECOVERY, SOURCE_KEY, "from the backup", "scrypt$"):
        assert secret not in text


def test_the_passphrases_are_in_no_log_and_no_file(admin, blob, target):
    import io as _io
    stream = _io.StringIO()
    logs.configure("json", "DEBUG", stream=stream)
    try:
        admin.post(f"{API}/restore", json=body(blob))
    finally:
        logs.configure("cli", "WARNING")
    assert PASS not in stream.getvalue() and RECOVERY not in stream.getvalue()
    assert all(PASS.encode() not in p.read_bytes() and RECOVERY.encode() not in p.read_bytes()
               for p in target.rglob("*") if p.is_file())


def test_the_audit_history_of_a_backup_is_kept_apart_and_snapshots_are_added(admin, blob, target):
    present = (target / "audit.log").read_bytes()
    admin.post(f"{API}/restore", json=body(blob))
    assert (target / "audit-imports/20300317T174640Z/audit.log").read_text().startswith('{"event": "x"}\n')
    assert present in (target / "audit.log").read_bytes()
    assert (target / "snapshots/site-1/snapshot-20260101-000000Z.json").exists()


def test_the_environment_keeps_winning_over_the_restored_file(admin, app, blob, monkeypatch):
    monkeypatch.setenv("UNIFI_URL", "https://environment.example")
    admin.post(f"{API}/restore", json=body(blob))
    assert app.state.config.controller_url == "https://environment.example"
    assert app.state.config.api_key == SOURCE_KEY                              # the rest comes from the restored file


def test_the_settings_and_the_service_are_made_anew(admin, app, blob):
    before = app.state.service
    admin.post(f"{API}/restore", json=body(blob))
    assert app.state.service is not before and app.state.config.controller_url == "https://source.example"


# -- who may, and what is checked first --------------------------------------------------------------------------------

def test_only_an_administrator_with_the_csrf_token_and_the_confirmation_restores(app, blob, target):
    viewer, admin = logged_in(app, "bob"), logged_in(app, "alice")       # a login writes last_login: before the snapshot
    before = tree(target)
    assert viewer.post(f"{API}/restore", json=body(blob)).status_code == 403
    assert TestClient(app).post(f"{API}/restore", json=body(blob), headers={"Origin": "http://testserver"}
                                ).status_code == 401
    no_token = logged_in(app, "alice")
    del no_token.headers["X-CSRF-Token"]
    assert no_token.post(f"{API}/restore", json=body(blob)).json()["error"] == "csrf_token"
    for changed in ({"confirm": False}, {"confirm": "yes"}):
        assert admin.post(f"{API}/restore", json={**body(blob), **changed}).status_code == 422
    missing = body(blob)
    del missing["confirm"]
    assert admin.post(f"{API}/restore", json=missing).status_code == 422
    assert tree(target) == before


def test_a_read_only_server_and_a_demo_refuse(target, blob):
    admin = logged_in(make_app(target, read_only=True), "alice")
    response = admin.post(f"{API}/restore", json=body(blob))
    assert response.status_code == 403 and response.json()["error"] == "read_only"
    demo = create_app(CONFIG, state_dir=target, hosts=["testserver"], auth=AuthState.for_directory(target, CONFIG),
                      service=ControllerService(CONFIG, session=DemoSession(), ttl=0), demo=True)
    admin = logged_in(demo, "alice")
    assert admin.post(f"{API}/restore", json=body(blob)).json()["error"] == "demo"


def test_a_recovery_passphrase_is_needed_typed_twice_and_long_enough_when_there_is_something_to_keep(admin, blob, target):
    before = tree(target)
    for changed, error in (({"recovery_passphrase": None, "recovery_confirm": None}, "recovery_passphrase_required"),
                           ({"recovery_passphrase": "short", "recovery_confirm": "short"}, "invalid_recovery_passphrase"),
                           ({"recovery_confirm": RECOVERY + "x"}, "recovery_passphrase_mismatch")):
        response = admin.post(f"{API}/restore", json={**body(blob), **changed})
        assert response.status_code == 422 and response.json()["error"] == error
    assert tree(target) == before and not (target / "recovery").exists()


def test_a_wrong_passphrase_a_modified_file_and_a_backup_without_an_administrator_change_nothing(admin, blob, target):
    before = tree(target)
    broken = bytearray(blob)
    broken[-3] ^= 1
    for given, errors in (({**body(blob), "passphrase": "not the passphrase at all"}, "backup_decrypt"),
                          (body(bytes(broken)), "backup_decrypt"), (body(b"junk"), "backup_not_a_backup")):
        response = admin.post(f"{API}/restore", json=given)
        assert response.status_code == 422 and response.json()["error"] == errors
    users = json.loads((target / "users.json").read_text())
    for user in users["users"]:
        user["disabled"] = True
    files = backup.collect(target, target / "hlp.toml")
    files["users.json"] = json.dumps(users).encode()
    locked_out = crypto.seal(backup.pack(files, (), 1.0), PASS)
    assert admin.post(f"{API}/restore", json=body(locked_out)).json()["error"] == "backup_no_administrator"
    assert tree(target) == before and not (target / "recovery").exists() and admin.get("/api/v1/auth/me").status_code == 200


# -- while it runs, and when it fails -----------------------------------------------------------------------------------

def test_while_a_restore_runs_files_are_not_written_a_second_restore_is_refused_and_the_scheduler_waits(
        admin, app, blob, target):
    seen = {}
    other = logged_in(app, "alice")             # a login writes the accounts file: it would wait for the restore

    def during(step):
        if step == "journal:applying":
            seen["note"] = other.post("/api/v1/unifi/sites/default/notes",
                                      json={"subject": "device:AA:BB:CC:00:00:01", "text": "late"})
            seen["restore"] = other.post(f"{API}/restore", json=body(blob))
            seen["ready"] = Scheduler(app).ready()
            seen["read"] = other.get("/api/v1/unifi/sites/default/notes").status_code

    restore.FAULT = during
    assert admin.post(f"{API}/restore", json=body(blob)).status_code == 200
    assert seen["note"].status_code == 503 and seen["note"].json()["error"] == "restore_in_progress"
    assert seen["restore"].status_code == 503 and seen["read"] == 200 and seen["ready"] is False
    assert Scheduler(app).ready() and not app.state.maintenance.active


def test_a_failure_puts_the_previous_state_back_keeps_the_sessions_and_the_recovery_backup(admin, app, blob, target):
    before = tree(target)

    def fail(step):
        if step == "installed:3":
            raise OSError("disk full")

    restore.FAULT = fail
    response = admin.post(f"{API}/restore", json=body(blob))
    restore.FAULT = None
    assert response.status_code == 500 and response.json()["error"] == "backup_restore_failed"
    assert "disk full" not in response.text and str(target) not in response.text
    assert tree(target) == before and not (target / restore.JOURNAL).exists()
    assert admin.get("/api/v1/auth/me").status_code == 200 and len(restore.recovery_backups(target)) == 1
    entry = audit(target)[-1]
    assert (entry["event"], entry["reason"]) == ("backup.restore_failed", "restore_failed")
    assert admin.post(f"{API}/restore", json=body(blob)).status_code == 200            # the place was given back


def test_a_crash_is_recovered_at_the_next_start_and_never_leaves_a_mixture(admin, blob, target, source):
    before, wanted = always(target), always(source)

    def crash(step):
        if step == "installed:2":
            raise Crash

    restore.FAULT = crash
    with pytest.raises(Crash):
        admin.post(f"{API}/restore", json=body(blob))
    restore.FAULT = None
    assert (target / restore.JOURNAL).exists()
    assert restore.recover(target, target / "hlp.toml") == "completed"
    assert always(target) == wanted != before


def test_a_recovery_backup_that_cannot_be_written_stops_the_restore_before_anything_changes(admin, blob, target):
    before = tree(target)
    (target / "recovery").write_text("a file where the folder belongs")
    response = admin.post(f"{API}/restore", json=body(blob))
    assert response.status_code == 500 and response.json()["error"] == "backup_recovery_failed"
    assert tree(target) == {**before, "recovery": b"a file where the folder belongs"} or tree(target) == before


def test_only_the_newest_three_recovery_backups_are_kept(app, blob, target, monkeypatch):
    import itertools
    ticks = itertools.count(1_900_000_000, 60)
    monkeypatch.setattr(backup_api, "CLOCK", lambda: next(ticks))
    for _ in range(5):                       # alice has the same password in the backup, so she logs in again each time
        assert logged_in(app, "alice").post(f"{API}/restore", json=body(blob)).status_code == 200
    names = restore.recovery_backups(target)
    assert len(names) == 3 and names == sorted(names)
    assert not any(p.name.endswith((restore.NEW, restore.OLD)) for p in target.rglob("*"))


# -- the preview says what the restore needs ----------------------------------------------------------------------------

def test_the_preview_says_whether_a_recovery_backup_is_needed_and_where_they_are_kept(admin, blob, tmp_path):
    shown = admin.post(f"{API}/preview", json={"passphrase": PASS, "archive": base64.b64encode(blob).decode()}).json()
    assert shown["recovery"] == {"required": True, "keep": 3, "folder": "recovery"}


# -- a fresh installation ---------------------------------------------------------------------------------------------

def fresh_server(directory, mode=MODE_SETUP, **kwargs):
    state = SetupState(mode, "no_config", TOKEN, resolver=lambda host, port: ["192.168.1.1"],
                       service_factory=lambda config: ControllerService(config, session=DemoSession(), ttl=0), **kwargs)
    auth = AuthState.for_directory(directory, STANDIN)
    app = create_app(STANDIN, state_dir=directory, hosts=["testserver"], auth=auth, setup=state)
    client = TestClient(app, headers={"Origin": "http://testserver", "X-Setup-Token": TOKEN})
    return app, state, client


def test_a_fresh_installation_is_restored_with_the_setup_token_and_needs_no_recovery_backup(tmp_path, blob, source):
    directory = tmp_path / "fresh"
    app, state, client = fresh_server(directory)
    shown = client.post(f"{API}/preview", json={"passphrase": PASS, "archive": base64.b64encode(blob).decode()})
    assert shown.status_code == 200 and shown.json()["recovery"]["required"] is False
    response = client.post(f"{API}/restore", json={"passphrase": PASS, "archive": base64.b64encode(blob).decode(),
                                                   "confirm": True})
    assert response.status_code == 200 and response.json()["recovery_backup"] is None
    assert always(directory) == always(source) and not (directory / "recovery").exists()
    assert state.mode is None and app.state.service is not None
    assert app.state.config.controller_url == "https://source.example"
    plain = TestClient(app)
    token = login(plain, "dave", "dave has a long password")
    assert token and plain.get("/api/v1/meta").json()["needs_setup"] is False
    entry = next(e for e in audit(directory) if e["event"] == "backup.restored")
    assert (entry["actor"], entry["recovery"]) == (SETUP_ACTOR, "none")


def test_without_the_token_nothing_is_previewed_or_restored(tmp_path, blob):
    directory = tmp_path / "fresh"
    app, _, _ = fresh_server(directory)
    for token in ("", "wrong-token-0123456789abcdef"):
        anonymous = TestClient(app, headers={"Origin": "http://testserver", "X-Setup-Token": token})
        for path in ("preview", "restore"):
            assert anonymous.post(f"{API}/{path}", json={"passphrase": PASS, "archive": "", "confirm": True}
                                  ).status_code == 401
    assert not directory.exists() or not list(directory.rglob("users.json"))


def test_an_installation_with_settings_but_no_administrator_is_restored_with_the_token_too(tmp_path, blob, source):
    directory = tmp_path / "admin-mode"
    (directory).mkdir()
    (directory / ".env").write_text("UNIFI_URL=https://old.example\nUNIFI_API_KEY=old-key-0123456789\n")
    app, state, client = fresh_server(directory, MODE_ADMIN)
    response = client.post(f"{API}/restore", json={"passphrase": PASS, "archive": base64.b64encode(blob).decode(),
                                                   "confirm": True, "recovery_passphrase": RECOVERY,
                                                   "recovery_confirm": RECOVERY})
    assert response.status_code == 200 and response.json()["recovery_backup"].startswith("recovery-")
    assert state.mode is None and (directory / "users.json").exists()


def test_a_backup_whose_settings_cannot_be_loaded_is_restored_but_the_setup_stays_open(tmp_path, source):
    (source / ".env").unlink()
    files = backup.collect(source, source / "hlp.toml")
    sealed = crypto.seal(backup.pack(files, (), 1.0), PASS)
    directory = tmp_path / "fresh"
    app, state, client = fresh_server(directory)
    response = client.post(f"{API}/restore", json={"passphrase": PASS, "archive": base64.b64encode(sealed).decode(),
                                                   "confirm": True})
    assert response.status_code == 500 and response.json()["error"] == "reload_failed"
    assert (directory / "users.json").exists() and state.mode == MODE_SETUP and app.state.service is None


# -- the start-up recovery --------------------------------------------------------------------------------------------

def test_the_server_start_finishes_an_interrupted_restore_before_it_reads_the_settings(tmp_path, source, blob, monkeypatch,
                                                                                    capsys):
    from homelab_probe.server import runner

    calls = []
    monkeypatch.setattr(runner.uvicorn, "run", lambda app, **kwargs: calls.append(app))
    directory = make_data(tmp_path / "crashed")
    AccountStore(directory).add("dave", "admin", "dave has a long password")
    package = backup.unpack(crypto.open_sealed(blob, PASS))

    def crash(step):
        if step == "installed:1":
            raise Crash

    restore.FAULT = crash
    with pytest.raises(Crash):
        restore.apply(directory, directory / "hlp.toml", restore.plan(package, directory, directory / "hlp.toml"))
    restore.FAULT = None
    assert cli.main(["serve", "--data-dir", str(directory)]) == 0
    err = capsys.readouterr().err
    assert "unfinished restore" in err and "completed" in err
    assert not (directory / restore.JOURNAL).exists() and always(directory) == always(source)
    assert calls[0].state.config.controller_url == "https://source.example"


def test_a_journal_that_cannot_be_read_stops_the_start_with_exit_code_3(tmp_path, monkeypatch, capsys):
    directory = make_data(tmp_path / "broken")
    (directory / restore.JOURNAL).write_text("{}")
    assert cli.main(["serve", "--data-dir", str(directory)]) == 3
    err = capsys.readouterr().err
    assert "restore-journal.json" in err and str(directory) not in err.replace("restore-journal.json", "")


def test_a_demo_never_looks_for_a_journal(tmp_path, monkeypatch):
    from homelab_probe.server import runner

    monkeypatch.setattr(runner.uvicorn, "run", lambda app, **kwargs: None)
    directory = tmp_path / "demo-dir"
    directory.mkdir()
    (directory / restore.JOURNAL).write_text("{}")
    assert cli.main(["--demo", "serve", "--data-dir", str(directory)]) == 0


def test_a_restore_that_loses_the_race_for_the_place_is_refused_without_touching_anything(app, blob, target, monkeypatch):
    from homelab_probe.server.maintenance import Maintenance

    admin = logged_in(app, "alice")
    before = tree(target)
    assert app.state.maintenance.begin()                                    # another restore holds the place ...
    monkeypatch.setattr(Maintenance, "active", property(lambda self: False))      # ... and the guard has not seen it yet
    response = admin.post(f"{API}/restore", json=body(blob))
    assert response.status_code == 503 and response.json()["error"] == "restore_in_progress"
    assert tree(target) == before


def test_every_session_ends_even_when_the_account_is_unchanged_by_the_backup(admin, app, target):
    own = admin.post(API, json={"passphrase": PASS, "confirm": PASS}).content            # a backup of this very state
    other = logged_in(app, "bob")
    assert admin.post(f"{API}/restore", json=body(own)).status_code == 200
    assert app.state.auth.sessions.count() == 0
    assert admin.get("/api/v1/auth/me").status_code == 401 and other.get("/api/v1/auth/me").status_code == 401
