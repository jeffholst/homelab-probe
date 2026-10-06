"""Backup export and preview over the API (issue #226)."""

import base64
import io
import json
import re

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("cryptography")

from fastapi.testclient import TestClient  # noqa: E402
from server_support import CONFIG, auth_for, logged_in  # noqa: E402

from homelab_probe import backup, logs  # noqa: E402
from homelab_probe.demo.session import DemoSession  # noqa: E402
from homelab_probe.notes import NotesStore  # noqa: E402
from homelab_probe.server import backup_api  # noqa: E402
from homelab_probe.server import backup_crypto as crypto  # noqa: E402
from homelab_probe.server.app import create_app  # noqa: E402
from homelab_probe.server.service import ControllerService  # noqa: E402

API = "/api/v1/backup"
PASS = "a long backup passphrase"
KEY = "the-api-key-0123456789"
NOTE = "the secret reason for the note"


@pytest.fixture(autouse=True)
def cheap_key(monkeypatch):
    monkeypatch.setattr(crypto, "SCRYPT_N", 2 ** 10)
    monkeypatch.setattr(backup_api, "CLOCK", lambda: 1_900_000_000.0)


def make_app(tmp_path, **kwargs):
    from homelab_probe.server.auth import AuthState

    accounts = AuthState.for_directory(tmp_path, CONFIG) if (tmp_path / "users.json").exists() else auth_for(tmp_path)
    return create_app(CONFIG, state_dir=tmp_path, hosts=["testserver"], auth=accounts,
                      service=ControllerService(CONFIG, session=DemoSession(), ttl=0), **kwargs)


@pytest.fixture
def app(tmp_path):
    app = make_app(tmp_path)
    (tmp_path / "hlp.toml").write_text("[thresholds]\nslow_link_mbps = 100\n")
    (tmp_path / ".env").write_text(f"UNIFI_URL=https://controller.example\nUNIFI_API_KEY={KEY}\n")
    NotesStore(tmp_path / "snapshots" / "site-1", "site-1").add("device:AA:BB:CC:00:00:01", NOTE, "alice", 1.0)
    (tmp_path / "snapshots" / "site-1" / "snapshot-20260101-000000Z.json").write_text("{}")
    (tmp_path / "audit.log").write_text('{"event": "x"}\n')
    return app


@pytest.fixture
def admin(app):
    return logged_in(app, "alice")


@pytest.fixture
def viewer(app):
    return logged_in(app, "bob")


def export(client, passphrase=PASS, confirm=None, **extra):
    return client.post(API, json={"passphrase": passphrase, "confirm": passphrase if confirm is None else confirm,
                                  **extra})


def preview(client, blob, passphrase=PASS):
    return client.post(f"{API}/preview", json={"passphrase": passphrase,
                                               "archive": base64.b64encode(blob).decode()})


def tree(path):
    return {str(p.relative_to(path)): p.read_bytes() for p in path.rglob("*") if p.is_file()
            and not p.name.endswith(".lock") and p.name != "audit.log"}


# -- the export ---------------------------------------------------------------------------------------------------

def test_an_administrator_downloads_an_encrypted_backup_that_opens_with_the_passphrase(admin, tmp_path):
    response = export(admin)
    assert response.status_code == 200 and response.headers["content-type"] == "application/octet-stream"
    assert re.fullmatch(r'attachment; filename="homelab-probe-backup-20300317-174640Z\.hlpbackup"',
                        response.headers["content-disposition"])
    assert response.headers["cache-control"] == "no-store"
    blob = response.content
    assert blob.startswith(crypto.MAGIC)
    for secret in (KEY.encode(), NOTE.encode(), PASS.encode(), b"alice", b"scrypt$"):
        assert secret not in blob
    package = backup.unpack(crypto.open_sealed(blob, PASS))
    assert set(package.files) == {"hlp.toml", ".env", "users.json", "snapshots/site-1/notes.json"}
    assert package.manifest["categories"] == list(backup.ALWAYS)
    assert KEY.encode() in package.files[".env"] and NOTE.encode() in package.files["snapshots/site-1/notes.json"]


def test_the_optional_categories_come_only_when_asked_for(admin):
    package = backup.unpack(crypto.open_sealed(export(admin, include=["snapshots", "audit"]).content, PASS))
    assert {"audit.log", "snapshots/site-1/snapshot-20260101-000000Z.json"} <= set(package.files)
    assert "snapshots" in package.manifest["categories"] and "audit" in package.manifest["categories"]


def test_an_export_changes_nothing_on_disk(admin, tmp_path):
    before = tree(tmp_path)
    export(admin, include=["snapshots", "audit"])
    assert tree(tmp_path) == before


def test_both_passphrases_must_match_and_be_long_enough_and_nothing_is_made_otherwise(admin, tmp_path):
    for body, error in (({"passphrase": PASS, "confirm": PASS + "x"}, "passphrase_mismatch"),
                        ({"passphrase": "short", "confirm": "short"}, "invalid_passphrase"),
                        ({"passphrase": "x" * 1025, "confirm": "x" * 1025}, "invalid_passphrase"),
                        ({"passphrase": "", "confirm": ""}, "invalid_passphrase")):
        response = admin.post(API, json=body)
        assert response.status_code == 422 and response.json()["error"] == error, body
        assert body["passphrase"] not in response.text or not body["passphrase"]


@pytest.mark.parametrize("body", [{}, {"passphrase": PASS}, {"confirm": PASS}, {"passphrase": 5, "confirm": 5},
                                  {"passphrase": PASS, "confirm": PASS, "include": ["users"]},
                                  {"passphrase": PASS, "confirm": PASS, "include": "audit"},
                                  {"passphrase": PASS, "confirm": PASS, "extra": 1},
                                  {"passphrase": "x" * 5000, "confirm": "x" * 5000}])
def test_a_malformed_request_is_422_and_never_echoes_the_passphrase(admin, body):
    response = admin.post(API, json=body)
    assert response.status_code == 422 and response.json()["error"] == "invalid_parameter"
    assert "x" * 50 not in response.text and PASS not in response.text


def test_only_an_administrator_with_the_csrf_token_exports_and_nothing_is_a_get(app, viewer, admin):
    assert export(viewer).status_code == 403
    assert TestClient(app).post(API, json={"passphrase": PASS, "confirm": PASS},
                                headers={"Origin": "http://testserver"}).status_code == 401
    no_token = logged_in(app, "alice")
    del no_token.headers["X-CSRF-Token"]
    assert export(no_token).json()["error"] == "csrf_token"
    foreign = logged_in(app, "alice")
    foreign.headers["Origin"] = "http://evil.example"
    assert export(foreign).status_code == 403
    assert admin.get(API).status_code == 405 and admin.get(f"{API}/preview").status_code == 405
    assert viewer.post(f"{API}/preview", json={"passphrase": PASS, "archive": ""}).status_code == 403


def test_a_read_only_server_still_exports_and_previews_because_they_change_nothing(tmp_path, app):
    admin = logged_in(make_app(tmp_path, read_only=True), "alice")
    blob = export(admin).content
    assert crypto.open_sealed(blob, PASS)
    assert preview(admin, blob).status_code == 200


def test_the_audit_names_the_categories_and_the_size_never_the_passphrase_or_content(admin, tmp_path):
    export(admin, include=["audit"])
    entries = [json.loads(line) for line in (tmp_path / "audit.log").read_text().splitlines()]
    entry = entries[-1]
    assert (entry["event"], entry["actor"], entry["optional"]) == ("backup.exported", "alice", "audit")
    assert entry["files"] == 5 and entry["size"] > 100
    text = (tmp_path / "audit.log").read_text()
    for secret in (PASS, KEY, NOTE, "scrypt$"):
        assert secret not in text


def test_the_passphrase_is_in_no_log_no_file_and_no_response(admin, tmp_path):
    stream = io.StringIO()
    logs.configure("json", "DEBUG", stream=stream)
    try:
        blob = export(admin, passphrase="unmistakable-passphrase-0001").content
        wrong = preview(admin, blob, "unmistakable-wrong-0002")
        preview(admin, blob, "unmistakable-passphrase-0001")
        admin.post(API, json={"passphrase": "unmistakable-short"})
    finally:
        logs.configure("cli", "WARNING")
    assert "unmistakable" not in stream.getvalue() + wrong.text
    assert all(b"unmistakable" not in p.read_bytes() for p in tmp_path.rglob("*") if p.is_file())


def test_a_symbolic_link_in_the_data_directory_is_a_fixed_500(admin, tmp_path):
    target = tmp_path / "outside.txt"
    target.write_text("x")
    (tmp_path / ".env").unlink()
    (tmp_path / ".env").symlink_to(target)
    response = export(admin)
    assert response.status_code == 500 and response.json()["error"] == "backup_unsafe"
    assert str(tmp_path) not in response.text


def test_a_busy_file_is_503_and_the_next_try_works(admin, tmp_path, monkeypatch):
    from homelab_probe.util import file_lock

    monkeypatch.setattr(backup, "LOCK_WAIT_SECONDS", 0.2)
    with file_lock(tmp_path / "users.json.lock"):
        response = export(admin)
    assert response.status_code == 503 and response.json()["error"] == "backup_busy"
    assert export(admin).status_code == 200


def test_only_a_few_run_at_once_because_the_key_derivation_takes_memory(admin, monkeypatch):
    taken = [backup_api._SLOTS.acquire(blocking=False) for _ in range(backup_api.SLOTS)]
    try:
        assert all(taken)
        for response in (export(admin), preview(admin, b"x")):
            assert response.status_code == 503 and response.json()["error"] == "backup_busy"
    finally:
        for _ in taken:
            backup_api._SLOTS.release()
    assert export(admin).status_code == 200


# -- the preview --------------------------------------------------------------------------------------------------

def test_the_preview_of_an_exported_backup_says_what_a_restore_would_do_and_holds_no_secret(admin, tmp_path):
    blob = export(admin, include=["audit"]).content
    response = preview(admin, blob)
    assert response.status_code == 200
    shown = response.json()
    assert shown["compatibility"]["compatible"] is True and shown["data_format"] == 1
    assert [u["username"] for u in shown["accounts"]["users"]] == ["alice", "bob"] and shown["notes"] == 1
    by_id = {c["id"]: c for c in shown["categories"]}
    assert by_id["audit"]["included"] and not by_id["snapshots"]["included"]
    assert by_id["snapshots"]["restore"] == "keep what is there"
    for secret in (KEY, NOTE, "scrypt$", PASS, "controller.example", "AA:BB"):
        assert secret not in response.text


def test_the_preview_names_a_setting_the_environment_keeps_overriding(admin, monkeypatch):
    monkeypatch.setenv("UNIFI_API_KEY", "the-environments-key")
    shown = preview(admin, export(admin).content).json()
    assert shown["environment_overrides"] == ["UNIFI_API_KEY"] and "the-environments-key" not in json.dumps(shown)


def test_a_preview_changes_nothing_and_is_audited_without_names(admin, tmp_path):
    blob = export(admin).content
    before = tree(tmp_path)
    preview(admin, blob)
    assert tree(tmp_path) == before
    entry = json.loads((tmp_path / "audit.log").read_text().splitlines()[-1])
    assert (entry["event"], entry["created"]) == ("backup.previewed", "2030-03-17T17:46:40Z")
    assert PASS not in json.dumps(entry) and "alice" not in json.dumps({k: v for k, v in entry.items() if k != "actor"})


def test_a_wrong_passphrase_a_modified_file_and_a_file_that_is_not_a_backup_are_refused_with_fixed_text(admin):
    blob = export(admin).content
    broken = bytearray(blob)
    broken[-5] ^= 1
    for given, passphrase, error in ((blob, "another long passphrase", "backup_decrypt"),
                                     (bytes(broken), PASS, "backup_decrypt"),
                                     (blob[:40], PASS, "backup_not_a_backup"),
                                     (b"just some text", PASS, "backup_not_a_backup")):
        response = preview(admin, given, passphrase)
        assert response.status_code == 422 and response.json()["error"] == error
        assert passphrase not in response.text
    assert preview(admin, blob).status_code == 200


@pytest.mark.parametrize("archive", ["not base64!!", "====", "QUJD\n", "é"])
def test_an_archive_that_is_not_base64_is_not_a_backup(admin, archive):
    response = admin.post(f"{API}/preview", json={"passphrase": PASS, "archive": archive})
    assert response.status_code == 422 and response.json()["error"] == "backup_not_a_backup"


def test_an_archive_over_the_limit_is_refused_before_anything_is_derived(admin, monkeypatch):
    monkeypatch.setattr(backup_api, "MAX_ARCHIVE", 10)
    monkeypatch.setattr(crypto, "_key", lambda *a: pytest.fail("a key was derived"))
    response = preview(admin, b"x" * 11)
    assert response.status_code == 422 and response.json()["error"] == "backup_too_large"


def test_a_backup_without_an_enabled_administrator_is_refused(admin, tmp_path):
    users = json.loads((tmp_path / "users.json").read_text())
    for user in users["users"]:
        user["disabled"] = True
    files = backup.collect(tmp_path, tmp_path / "hlp.toml")
    files["users.json"] = json.dumps(users).encode()
    blob = crypto.seal(backup.pack(files, (), 1.0), PASS)
    response = preview(admin, blob)
    assert response.status_code == 422 and response.json()["error"] == "backup_no_administrator"


def test_a_hostile_archive_inside_a_correctly_sealed_file_is_refused(admin):
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", "{}")
        archive.writestr("../../evil", "x")
    response = preview(admin, crypto.seal(buffer.getvalue(), PASS))
    assert response.status_code == 422 and response.json()["error"] == "backup_unsafe_member"


def test_a_backup_of_a_format_this_version_does_not_read_is_refused_before_a_key_is_derived(admin, monkeypatch):
    blob = bytearray(export(admin).content)
    header = json.loads(blob[14:14 + int.from_bytes(blob[10:14], "big")])
    header["format"] = 2
    text = json.dumps(header, sort_keys=True, separators=(",", ":")).encode()
    forged = crypto.MAGIC + len(text).to_bytes(4, "big") + text + bytes(blob[14 + int.from_bytes(blob[10:14], "big"):])
    monkeypatch.setattr(crypto, "_key", lambda *a: pytest.fail("a key was derived"))
    response = preview(admin, forged)
    assert response.status_code == 422 and response.json()["error"] == "backup_unsupported_format"
