"""The application backup: archive, collection, checks and preview (issue #226)."""

import io
import json
import os
import threading
import zipfile
from functools import partial

import pytest
from backup_support import ENV, KEY, NOTE, NOW, make_data

from homelab_probe import backup
from homelab_probe.backup import BackupError, Package
from homelab_probe.notes import NotesStore


@pytest.fixture
def data(tmp_path):
    return make_data(tmp_path / "data")


def pack_all(data, include=("snapshots", "audit")):
    return backup.pack(backup.collect(data, data / "hlp.toml", include), include, NOW)


def code(call):
    with pytest.raises(BackupError) as caught:
        call()
    return caught.value.code


def zip_of(members, manifest="valid"):
    """A hand-made archive of ``members`` (name -> bytes or ZipInfo/bytes pair) with a manifest of those names."""
    files = {n: d for n, d in members.items() if not isinstance(d, tuple)}
    if manifest == "valid":
        import hashlib
        manifest = {"format": 1, "data_format": 1, "app_version": "0.3.0", "created_at": "2026-01-01T00:00:00Z",
                    "categories": list(backup.ALWAYS),
                    "files": {n: {"size": len(d), "sha256": hashlib.sha256(d).hexdigest()} for n, d in files.items()}}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        if manifest is not None:
            archive.writestr("manifest.json", manifest if isinstance(manifest, bytes) else json.dumps(manifest))
        for name, data in members.items():
            if isinstance(data, tuple):
                archive.writestr(data[0], data[1])
            else:
                archive.writestr(name, data)
    return buffer.getvalue()


def valid_manifest(**changes):
    base = {"format": 1, "data_format": 1, "app_version": "0.3.0", "created_at": "2026-01-01T00:00:00Z",
            "categories": list(backup.ALWAYS), "files": {}}
    return {**base, **changes}


# -- which names are allowed ------------------------------------------------------------------------------------

@pytest.mark.parametrize("name, category", [
    ("hlp.toml", "settings"), (".env", "config"), ("users.json", "accounts"), ("certs/controller.pem", "certificates"),
    ("snapshots/site-1/notes.json", "notes"), ("snapshots/site-1/triage.json", "triage"),
    ("snapshots/_x/notes.json", "notes"), ("snapshots/-abc/notes.json", "notes"),
    ("snapshots/site-1/snapshot-20260101-000000Z.json", "snapshots"), ("snapshots/snapshot-20250101-000000Z.json", "snapshots"),
    ("audit.log", "audit"), ("audit.log.3", "audit")])
def test_the_names_the_table_allows_have_their_category(name, category):
    assert backup.classify(name) == category


@pytest.mark.parametrize("name", [
    "", "../users.json", "/etc/passwd", "a\\b", "users.json/", "./users.json", "snapshots/../users.json",
    "snapshots/.hidden/notes.json", "snapshots//notes.json", "snapshots/a/b/notes.json", "snapshots/notes.json",
    "certs/x.txt", "certs/../users.json", "certs/.pem", "users.json.lock", "users.json.bak", "notify-state.json",
    "snapshots/site-1/notify-state.json", "snapshots/site-1/notes.json.lock", "snapshots/site-1/x.json",
    "snapshots/site-1/snapshot-x.txt", "snapshots/site-1/notes.json\n", "audit.log.x", ".env.bak", "hlp.toml.bak",
    "sessions.json", "snapshots/" + "a" * 65 + "/notes.json", "snapshots/site 1/notes.json", "manifest.json",
    "snapshots/site-1/../notes.json", "snapshots/\u202esite/notes.json"])
def test_any_other_name_is_not_allowed(name):
    assert backup.classify(name) is None


# -- reading the data directory --------------------------------------------------------------------------------

def test_the_live_files_are_exactly_the_categories_and_never_locks_state_or_temporary_files(data):
    (data / "users.json.lock").write_text("")
    (data / "snapshots" / "site-1" / "notes.json.lock").write_text("")
    (data / "snapshots" / "site-1" / "notes.json.abc.tmp").write_text("x")
    (data / "sessions.json").write_text("{}")
    names = set(backup.live_files(data, data / "hlp.toml", ()))
    assert names == {"hlp.toml", ".env", "users.json", "certs/controller.pem", "snapshots/site-1/notes.json",
                     "snapshots/site-1/triage.json"}
    everything = set(backup.live_files(data, data / "hlp.toml", ("snapshots", "audit")))
    assert everything - names == {"snapshots/site-1/snapshot-20260101-000000Z.json",
                                  "snapshots/snapshot-20250101-000000Z.json", "audit.log", "audit.log.1"}
    assert set(backup.live_files(data, data / "hlp.toml", ("audit",))) - names == {"audit.log", "audit.log.1"}


def test_an_empty_directory_has_nothing_and_a_missing_one_too(tmp_path):
    assert backup.live_files(tmp_path, tmp_path / "hlp.toml", ("snapshots", "audit")) == {}
    assert backup.live_files(tmp_path / "missing", tmp_path / "missing" / "hlp.toml", ()) == {}


def test_the_settings_file_the_server_uses_goes_in_as_hlp_toml(data, tmp_path):
    named = tmp_path / "elsewhere.toml"
    named.write_text("[thresholds]\nslow_link_mbps = 10\n")
    files = backup.collect(data, named)
    assert files["hlp.toml"] == named.read_bytes()


@pytest.mark.parametrize("victim", ["users.json", ".env", "hlp.toml", "certs/controller.pem",
                                    "snapshots/site-1/notes.json", "snapshots/site-1/snapshot-20260101-000000Z.json",
                                    "snapshots/snapshot-20250101-000000Z.json", "audit.log"])
def test_a_symbolic_link_in_place_of_a_file_refuses_the_backup(data, tmp_path, victim):
    target = tmp_path / "secret.txt"
    target.write_text("outside")
    path = data / victim
    path.unlink()
    path.symlink_to(target)
    assert code(lambda: backup.collect(data, data / "hlp.toml", ("snapshots", "audit"))) == "unsafe"


@pytest.mark.parametrize("victim", ["certs", "snapshots", "snapshots/site-1"])
def test_a_symbolic_link_in_place_of_a_directory_refuses_the_backup(data, tmp_path, victim):
    import shutil
    path = data / victim
    moved = tmp_path / "moved"
    shutil.move(str(path), moved)
    path.symlink_to(moved)
    assert code(lambda: backup.collect(data, data / "hlp.toml", ("snapshots", "audit"))) == "unsafe"


def test_a_directory_where_a_file_belongs_refuses_the_backup(data):
    (data / ".env").unlink()
    (data / ".env").mkdir()
    assert code(lambda: backup.collect(data, data / "hlp.toml")) == "unsafe"
    (data / ".env").rmdir()
    (data / "certs").rmdir() if not any((data / "certs").iterdir()) else None
    (data / "certs" / "controller.pem").unlink()
    (data / "certs").rmdir()
    (data / "certs").write_text("a file")
    assert code(lambda: backup.collect(data, data / "hlp.toml")) == "unsafe"


def test_collecting_reads_the_bytes_and_changes_nothing(data):
    def state():
        return {str(p.relative_to(data)): (p.read_bytes(), p.stat().st_mtime_ns) for p in data.rglob("*")
                if p.is_file() and not p.name.endswith(".lock")}

    before = state()
    files = backup.collect(data, data / "hlp.toml", ("snapshots", "audit"))
    assert state() == before
    assert files["users.json"] == (data / "users.json").read_bytes() and files[".env"] == ENV.encode()


def test_a_busy_lock_is_busy_not_a_half_read_file(data, monkeypatch):
    from homelab_probe.util import file_lock

    monkeypatch.setattr(backup, "LOCK_WAIT_SECONDS", 0.2)
    for lock in ("users.json.lock", "snapshots/site-1/notes.json.lock", "snapshots/site-1/triage.json.lock"):
        with file_lock(data / lock):
            assert code(lambda: backup.collect(data, data / "hlp.toml")) == "busy"
    assert backup.collect(data, data / "hlp.toml")


def test_a_writer_that_holds_the_lock_makes_the_export_wait_and_then_read_the_new_file(data, monkeypatch):
    from homelab_probe.util import file_lock

    started, release = threading.Event(), threading.Event()
    seen = {}

    def writer():
        with file_lock(data / "users.json.lock"):
            started.set()
            release.wait(5)
            (data / "users.json").write_text((data / "users.json").read_text())

    thread = threading.Thread(target=writer)
    thread.start()
    started.wait(5)
    threading.Timer(0.3, release.set).start()
    seen["files"] = backup.collect(data, data / "hlp.toml")
    thread.join()
    assert seen["files"]["users.json"] == (data / "users.json").read_bytes()


def test_a_file_or_a_directory_that_is_too_large_is_refused(data, monkeypatch):
    monkeypatch.setattr(backup, "MAX_FILE", 10)
    assert code(lambda: backup.collect(data, data / "hlp.toml")) == "too_large"
    monkeypatch.setattr(backup, "MAX_FILE", 10 ** 6)
    monkeypatch.setattr(backup, "MAX_TOTAL", 100)
    assert code(lambda: backup.collect(data, data / "hlp.toml")) == "too_large"


# -- the archive -------------------------------------------------------------------------------------------------

def test_an_archive_round_trips_with_a_manifest_that_describes_it(data):
    files = backup.collect(data, data / "hlp.toml", ("snapshots",))
    package = backup.unpack(backup.pack(files, ("snapshots",), NOW))
    assert package.files == files
    manifest = package.manifest
    assert (manifest["format"], manifest["data_format"], manifest["app_version"]) == (1, 1, backup.__version__)
    assert manifest["created_at"] == "2030-03-17T17:46:40Z"
    assert manifest["categories"] == [*backup.ALWAYS, "snapshots"] and set(manifest["files"]) == set(files)
    assert all(e["size"] == len(files[n]) for n, e in manifest["files"].items())


def test_the_optional_categories_are_only_in_the_archive_when_chosen(data):
    plain = backup.unpack(pack_all(data, ()))
    assert plain.manifest["categories"] == list(backup.ALWAYS)
    assert not any(backup.classify(n) in backup.OPTIONAL for n in plain.files)
    assert "audit" in backup.unpack(pack_all(data, ("audit",))).manifest["categories"]


def test_pack_refuses_a_name_no_chosen_category_allows(data):
    assert code(lambda: backup.pack({"evil.sh": b"x"}, (), NOW)) == "unsafe_member"
    assert code(lambda: backup.pack({"audit.log": b"x"}, (), NOW)) == "unsafe_member"     # audit was not chosen


def test_no_session_cache_lock_or_notification_state_is_in_an_archive(data):
    names = set(backup.unpack(pack_all(data)).files)
    assert not any(n.endswith((".lock", ".tmp")) or "notify-state" in n or "session" in n for n in names)


@pytest.mark.parametrize("name", ["../evil", "/abs", "snapshots/../../x", "a\\b.json", "users.json/../x", "evil.sh",
                                  "snapshots/site-1/", "certs/", "notify-state.json"])
def test_an_unsafe_member_name_refuses_the_whole_archive(name):
    assert code(lambda: backup.unpack(zip_of({"users.json": b"{}", name: b"x"}))) == "unsafe_member"


def test_a_duplicate_member_a_directory_a_link_and_an_encrypted_member_are_refused(tmp_path):
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        duplicate = zip_of({"users.json": b"{}", "again": ("users.json", b"{}")})
    assert code(lambda: backup.unpack(duplicate)) == "unsafe_member"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", json.dumps(valid_manifest()))
        link = zipfile.ZipInfo("users.json")
        link.external_attr = 0o120777 << 16
        archive.writestr(link, "/etc/passwd")
    assert code(lambda: backup.unpack(buffer.getvalue())) == "unsafe_member"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", json.dumps(valid_manifest()))
        archive.writestr(zipfile.ZipInfo("certs/"), "")
    assert code(lambda: backup.unpack(buffer.getvalue())) == "unsafe_member"
    data = bytearray(zip_of({"users.json": b"{}"}))
    for marker in (b"PK\x03\x04", b"PK\x01\x02"):          # set the "encrypted" bit of every header
        index = 0
        while (index := data.find(marker, index)) != -1:
            offset = index + (6 if marker == b"PK\x03\x04" else 8)
            data[offset] |= 1
            index += 4
    assert code(lambda: backup.unpack(bytes(data))) == "unsafe_member"


@pytest.mark.parametrize("blob", [b"", b"not a zip", b"PK\x03\x04" + b"\0" * 30])
def test_something_that_is_not_an_archive_is_refused(blob):
    assert code(lambda: backup.unpack(blob)) == "not_an_archive"


def test_a_damaged_archive_is_refused(data):
    blob = bytearray(pack_all(data))
    assert code(lambda: backup.unpack(bytes(blob[: len(blob) // 2]))) == "not_an_archive"
    flipped = bytearray(blob)
    flipped[60] ^= 0xFF
    assert code(lambda: backup.unpack(bytes(flipped))) in {"not_an_archive", "mismatch", "unsafe_member", "bad_manifest"}


def test_an_empty_archive_and_one_without_a_manifest_are_refused():
    assert code(lambda: backup.unpack(zip_of({}, manifest=None))) == "not_an_archive"
    assert code(lambda: backup.unpack(zip_of({"users.json": b"{}"}, manifest=None))) == "bad_manifest"


def test_too_many_or_too_large_members_are_refused(monkeypatch, data):
    blob = pack_all(data)
    monkeypatch.setattr(backup, "MAX_MEMBERS", 3)
    assert code(lambda: backup.unpack(blob)) == "too_large"
    monkeypatch.setattr(backup, "MAX_MEMBERS", 20000)
    monkeypatch.setattr(backup, "MAX_FILE", 50)
    assert code(lambda: backup.unpack(blob)) == "too_large"
    monkeypatch.setattr(backup, "MAX_FILE", 10 ** 6)
    monkeypatch.setattr(backup, "MAX_TOTAL", 200)
    assert code(lambda: backup.unpack(blob)) == "too_large"


@pytest.mark.parametrize("manifest", [
    b"not json", b"\xff\xfe", b"[]", b"null", b'"x"', json.dumps(valid_manifest(files=[])).encode(),
    json.dumps(valid_manifest(categories="all")).encode(), json.dumps(valid_manifest(created_at=5)).encode(),
    json.dumps(valid_manifest(app_version=None)).encode(), json.dumps(valid_manifest(categories=["notes"])).encode(),
    json.dumps(valid_manifest(categories=[*backup.ALWAYS, "bogus"])).encode(),
    json.dumps(valid_manifest(categories=[*backup.ALWAYS, "notes"])).encode(),
    json.dumps(valid_manifest(files={"users.json": {"size": True, "sha256": "x"}})).encode(),
    json.dumps(valid_manifest(files={"users.json": {"size": 2}})).encode(),
    json.dumps(valid_manifest(files={"users.json": 5})).encode()])
def test_a_damaged_manifest_is_refused(manifest):
    assert code(lambda: backup.unpack(zip_of({"users.json": b"{}"}, manifest=manifest))) == "bad_manifest"


def test_the_formats_are_checked_before_anything_is_used():
    assert code(lambda: backup.unpack(zip_of({}, manifest=valid_manifest(format=2)))) == "unsupported_format"
    for version in (0, 2, 99, "1", None):
        assert code(partial(backup.unpack, zip_of({}, manifest=valid_manifest(data_format=version)))
                    ) == "unsupported_data_format"


def test_the_manifest_must_match_the_members_exactly(data):
    import hashlib
    users = b"{}"
    good = {"size": 2, "sha256": hashlib.sha256(users).hexdigest()}
    for files in ({}, {"users.json": {**good, "size": 3}}, {"users.json": {**good, "sha256": "0" * 64}},
                  {"users.json": good, ".env": good}):
        assert code(partial(backup.unpack, zip_of({"users.json": users}, manifest=valid_manifest(files=files)))
                    ) == "mismatch"
    # a member whose category the manifest did not list
    optional = {"audit.log": {"size": 1, "sha256": hashlib.sha256(b"x").hexdigest()}}
    assert code(lambda: backup.unpack(zip_of({"audit.log": b"x"}, manifest=valid_manifest(files=optional)))
                ) == "mismatch"


# -- the contents ------------------------------------------------------------------------------------------------

def package_of(data, **replace):
    files = backup.collect(data, data / "hlp.toml", ("snapshots", "audit"))
    for name, value in replace.items():
        key = name.replace("__", "/").replace("_dot_", ".")
        if value is None:
            files.pop(key)
        else:
            files[key] = value if isinstance(value, bytes) else value.encode()
    return Package(backup.unpack(backup.pack(files, ("snapshots", "audit"), NOW)).manifest, files)


def test_a_good_backup_is_inspected_without_secrets(data):
    summary = backup.inspect(package_of(data))
    assert summary["administrators"] == 1 and summary["notes"] == 2 and summary["triage"] == 1
    assert summary["users"] == [{"username": "alice", "role": "admin", "disabled": False},
                                {"username": "bob", "role": "viewer", "disabled": False}]
    assert summary["settings_names"] == ["NOTIFY_NTFY_URL", "UNIFI_API_KEY", "UNIFI_URL"]
    assert KEY not in json.dumps(summary) and NOTE not in json.dumps(summary)


def test_a_backup_without_an_enabled_administrator_is_refused(data, tmp_path):
    users = json.loads((data / "users.json").read_text())
    for user in users["users"]:
        user["disabled"] = user["role"] == "admin"
    assert code(lambda: backup.inspect(package_of(data, users_dot_json=json.dumps(users)))) == "no_administrator"
    only_viewer = make_data(tmp_path / "viewers", admin=False)
    assert code(lambda: backup.inspect(package_of(only_viewer))) == "no_administrator"
    assert code(lambda: backup.inspect(package_of(data, users_dot_json=None))) == "no_administrator"


@pytest.mark.parametrize("name, content, category", [
    ("users_dot_json", "not json", "accounts"), ("users_dot_json", '{"version": 1, "users": [{}]}', "accounts"),
    ("users_dot_json", b"\xff\xfe", "accounts"),
    ("hlp_dot_toml", "[thresholds\n", "settings"), ("hlp_dot_toml", "[thresholds]\nslow_link_mbps = -1\n", "settings"),
    ("_dot_env", b"\xff\xfe", "config"),
    ("certs__controller_dot_pem", "not a certificate", "certificates"),
    ("certs__controller_dot_pem", "-----BEGIN CERTIFICATE-----\n!!!\n-----END CERTIFICATE-----\n", "certificates"),
    ("certs__controller_dot_pem", b"\xff\xfe", "certificates"),
    ("snapshots__site_1__notes_dot_json", "not json", "notes"),
    ("snapshots__site_1__notes_dot_json", '{"version": 1, "site": "site-1", "entries": {"a": {"text": 1}}}', "notes"),
    ("snapshots__site_1__triage_dot_json", '{"version": 1, "site": "site-1", "entries": {"a": {"state": "x"}}}',
     "triage"),
    ("snapshots__site_1__snapshot-20260101-000000Z_dot_json", "[]", "snapshots"),
    ("snapshots__site_1__snapshot-20260101-000000Z_dot_json", "not json", "snapshots"),
    ("audit_dot_log", b"\xff\xfe", "audit")])
def test_a_file_that_does_not_parse_with_the_applications_own_reader_is_refused(data, name, content, category):
    key = name.replace("__", "/").replace("_dot_", ".").replace("site_1", "site-1")
    files = backup.collect(data, data / "hlp.toml", ("snapshots", "audit"))
    files[key] = content if isinstance(content, bytes) else content.encode()
    with pytest.raises(BackupError) as caught:
        backup.inspect(Package({}, files))
    assert caught.value.code == "invalid_content" and caught.value.category == category
    assert content not in str(caught.value) if isinstance(content, str) else True


def test_notes_and_triage_cannot_name_another_site_than_their_directory(data):
    for file_name in ("notes.json", "triage.json"):
        path = data / "snapshots" / "site-1" / file_name
        document = json.loads(path.read_text())
        for site in ("site-2", None, ""):
            changed = dict(document)
            changed["site"] = site
            if site is None:
                changed.pop("site")
            files = backup.collect(data, data / "hlp.toml")
            files["snapshots/site-1/" + file_name] = json.dumps(changed).encode()
            with pytest.raises(BackupError) as caught:
                backup.inspect(Package({}, files))
            assert caught.value.code == "invalid_content", (file_name, site)
    # the site of a directory is its key: an id with other characters is kept under its key
    store = NotesStore(data / "snapshots" / "a_b", "a/b")
    store.add("device:AA:BB:CC:00:00:01", "x", "alice", NOW)
    assert backup.inspect(package_of(data))["sites"] == ["a_b", "site-1"]


# -- the preview ------------------------------------------------------------------------------------------------

def preview(data, target=None, environ=None, **kw):
    package = backup.unpack(pack_all(data))
    return backup.preview(package, target or data, (target or data) / "hlp.toml", environ or {})


def test_the_preview_says_what_a_restore_does_and_holds_no_secret(data, tmp_path):
    shown = preview(data, tmp_path / "fresh")
    text = json.dumps(shown)
    for secret in (KEY, NOTE, "correct horse", "scrypt$", "controller.example", "lab-alerts", "AA:BB:CC"):
        assert secret not in text, secret
    assert shown["created_at"] == "2030-03-17T17:46:40Z" and shown["data_format"] == 1
    assert shown["compatibility"]["compatible"] is True and shown["compatibility"]["supported_data_formats"] == [1]
    by_id = {c["id"]: c for c in shown["categories"]}
    assert [c["id"] for c in shown["categories"]] == list(backup.CATEGORIES)
    assert by_id["accounts"]["included"] and by_id["accounts"]["restore"] == "replace" and by_id["accounts"]["files"] == 1
    assert by_id["notes"]["files"] == 1 and by_id["snapshots"]["files"] == 2 and by_id["audit"]["files"] == 2
    assert all(c["present_files"] == 0 for c in shown["categories"])                  # a fresh directory
    assert "separate" in by_id["audit"]["restore"] and "stay" in by_id["snapshots"]["restore"]
    assert shown["accounts"]["replace"] is True and shown["accounts"]["administrators"] == 1
    assert [u["username"] for u in shown["accounts"]["users"]] == ["alice", "bob"]
    assert (shown["notes"], shown["triage"], shown["sites"]) == (2, 1, 1)


def test_the_preview_counts_what_is_there_now_so_the_replacement_is_not_a_surprise(data):
    shown = preview(data)
    assert {c["id"]: c["present_files"] for c in shown["categories"]} == {
        "settings": 1, "config": 1, "accounts": 1, "certificates": 1, "notes": 1, "triage": 1, "snapshots": 2, "audit": 2}


def test_optional_categories_that_are_not_in_the_backup_are_kept_not_deleted(data):
    package = backup.unpack(pack_all(data, ()))
    shown = backup.preview(package, data, data / "hlp.toml", {})
    by_id = {c["id"]: c for c in shown["categories"]}
    assert not by_id["snapshots"]["included"] and by_id["snapshots"]["restore"] == "keep what is there"
    assert by_id["audit"]["restore"] == "keep what is there" and by_id["audit"]["present_files"] == 2


def test_a_setting_the_environment_sets_stays_overridden_and_is_said_by_name_only(data):
    shown = preview(data, environ={"UNIFI_API_KEY": "from-the-environment", "UNIFI_URL": "  ", "OTHER": "x"})
    assert shown["environment_overrides"] == ["UNIFI_API_KEY"]
    [warning] = shown["warnings"]
    assert warning["code"] == "environment_overrides" and "UNIFI_API_KEY" in warning["message"]
    assert "from-the-environment" not in json.dumps(shown) and KEY not in json.dumps(shown)
    assert preview(data)["environment_overrides"] == [] and preview(data)["warnings"] == []


def test_a_backup_from_a_newer_version_with_a_data_format_this_one_reads_is_said(data, monkeypatch):
    package = backup.unpack(pack_all(data))
    package.manifest["app_version"] = "99.0.0"
    [warning] = backup.preview(package, data, data / "hlp.toml", {})["warnings"]
    assert warning["code"] == "newer_application"
    package.manifest["app_version"] = "not-a-version"
    assert backup.preview(package, data, data / "hlp.toml", {})["warnings"] == []


def test_a_preview_changes_nothing_on_disk(data):
    before = {str(p): p.read_bytes() for p in data.rglob("*") if p.is_file() and not p.name.endswith(".lock")}
    preview(data)
    assert {str(p): p.read_bytes() for p in data.rglob("*") if p.is_file() and not p.name.endswith(".lock")} == before
    assert not os.path.exists(data / "restore")


# -- what is left alone, and what cannot be examined --------------------------------------------------------------

def test_files_and_links_that_belong_to_no_category_are_left_alone(data, tmp_path):
    (data / "snapshots" / "latest.txt").write_text("x")
    (data / "snapshots" / ".hidden").symlink_to(tmp_path)           # a name no site directory has: not ours
    (data / "certs" / "README.txt").write_text("x")
    (data / "snapshots" / "site-1" / "extra.txt").write_text("x")
    (data / "snapshots" / "site-1" / "subdir").mkdir()
    (data / "stray.log").write_text("x")
    names = set(backup.live_files(data, data / "hlp.toml", ("snapshots", "audit")))
    assert names == set(backup.live_files(make_data(tmp_path / "plain"), tmp_path / "plain" / "hlp.toml",
                                          ("snapshots", "audit")))


def test_a_file_that_cannot_be_examined_or_opened_refuses_the_backup(data, monkeypatch):
    real_lstat = os.lstat
    monkeypatch.setattr(os, "lstat", lambda path, *a, **k: (_ for _ in ()).throw(PermissionError()) if str(path).endswith(
        "users.json") else real_lstat(path, *a, **k))
    assert code(lambda: backup.live_files(data, data / "hlp.toml", ())) == "unsafe"
    monkeypatch.undo()
    real_open = os.open

    def refuse(path, *args, **kwargs):
        if str(path).endswith("hlp.toml"):
            raise PermissionError
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", refuse)
    assert code(lambda: backup.collect(data, data / "hlp.toml")) == "unsafe"


def test_a_file_that_vanishes_between_the_listing_and_the_read_refuses_the_backup(data):
    assert code(lambda: backup._listed(data / "vanished.json")) == "unsafe"
    assert backup._listed(data / "users.json") == data / "users.json"
