"""Web accounts: hashing, the accounts file, the roles, the audit log (issue #182)."""

import hmac
import json
import os
import stat
import sys
import threading

import pytest

from homelab_probe import accounts, logs
from homelab_probe.accounts import (
    AccountError,
    AccountStore,
    AuditLog,
    LocalAccounts,
    Principal,
    ScryptParams,
    User,
)

PASSWORD = "correct horse battery"
OTHER = "another long password"
posix = pytest.mark.skipif(sys.platform == "win32", reason="file modes mean little on Windows")


@pytest.fixture(autouse=True)
def cheap_hashes(monkeypatch):
    """scrypt at the real cost is deliberate; the tests use the smallest one that still exercises every path."""
    monkeypatch.setattr(accounts, "PARAMS", ScryptParams(n=16, r=8, p=1))


@pytest.fixture
def store(tmp_path):
    return AccountStore(tmp_path / "data")


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


# -- passwords ---------------------------------------------------------------------------------------------------

def test_a_hash_carries_its_parameters_and_a_fresh_salt():
    first, second = accounts.hash_password(PASSWORD), accounts.hash_password(PASSWORD)
    kind, n, r, p, salt, digest = first.split("$")
    assert (kind, n, r, p) == ("scrypt", "16", "8", "1") and first != second
    assert "=" not in salt + digest and PASSWORD not in first
    assert accounts.verify_password(PASSWORD, first) and accounts.verify_password(PASSWORD, second)
    assert not accounts.verify_password(OTHER, first) and not accounts.verify_password("", first)


def test_the_comparison_is_constant_time(monkeypatch):
    seen = []
    real = hmac.compare_digest
    monkeypatch.setattr(accounts.hmac, "compare_digest", lambda a, b: seen.append((a, b)) or real(a, b))
    stored = accounts.hash_password(PASSWORD)
    assert accounts.verify_password(PASSWORD, stored) and len(seen) == 1


@pytest.mark.parametrize("stored", [
    "", "plain", "scrypt$16$8$1$AAAA", "bcrypt$16$8$1$AAAA$AAAA", "scrypt$x$8$1$AAAA$AAAA", "scrypt$16$8$1$!!$AAAA",
    "scrypt$15$8$1$AAAA$" + "A" * 43, "scrypt$16$0$1$AAAA$" + "A" * 43, "scrypt$16$8$0$AAAA$" + "A" * 43,
    f"scrypt${2 ** 21}$8$1$AAAA$" + "A" * 43, "scrypt$16$33$1$AAAA$" + "A" * 43,
    "scrypt$16$8$17$AAAA$" + "A" * 43, "scrypt$16$8$1$$" + "A" * 43, "scrypt$16$8$1$AAAA$AAAA", None, 5,
])
def test_a_stored_value_that_is_not_ours_never_verifies_and_costs_the_same_work(stored, monkeypatch):
    calls = []
    real = accounts._scrypt
    monkeypatch.setattr(accounts, "_scrypt", lambda *a, **k: calls.append(1) or real(*a, **k))
    assert accounts.parse_hash(stored) is None
    assert accounts.verify_password(PASSWORD, stored) is False and len(calls) == 1


def test_a_hash_made_with_other_parameters_is_flagged_for_an_upgrade(monkeypatch):
    stored = accounts.hash_password(PASSWORD)
    assert not accounts.needs_upgrade(stored)
    monkeypatch.setattr(accounts, "PARAMS", ScryptParams(n=32, r=8, p=1))
    assert accounts.needs_upgrade(stored) and accounts.needs_upgrade("garbage")
    assert accounts.verify_password(PASSWORD, stored)                  # still good: the parameters are in the hash


@pytest.mark.parametrize("password, ok", [("a" * 11, False), ("a" * 12, True), (" " * 12, True), ("é" * 12, True),
                                          ("a" * 1024, True), ("a" * 1025, False), ("", False)])
def test_the_only_rule_for_a_password_is_its_length(password, ok):
    if ok:
        accounts.check_password_policy(password)
    else:
        with pytest.raises(AccountError, match="characters"):
            accounts.check_password_policy(password)


def test_a_password_that_is_not_text_is_refused():
    with pytest.raises(AccountError):
        accounts.check_password_policy(None)


# -- usernames and roles -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, name", [("Alice", "alice"), ("  Bob.Smith  ", "bob.smith"), ("a-b_c@d.e", "a-b_c@d.e"),
                                        ("x" * 64, "x" * 64), ("123", "123")])
def test_usernames_are_trimmed_and_case_insensitive(text, name):
    assert accounts.check_username(text) == name


@pytest.mark.parametrize("text", ["ab", "x" * 65, "-abc", ".abc", "a b c", "a/b", "ünï", "", "a\nbc", "a;drop"])
def test_bad_usernames_are_refused(text):
    with pytest.raises(AccountError, match="3 to 64 characters"):
        accounts.check_username(text)


def test_only_the_two_roles_exist():
    assert accounts.ROLES == ("viewer", "admin") and accounts.check_role("admin") == "admin"
    with pytest.raises(AccountError, match="viewer, admin"):
        accounts.check_role("root")


# -- the accounts file -------------------------------------------------------------------------------------------

def test_an_empty_directory_has_no_users(store):
    assert store.users() == [] and store.get("anyone") is None and not store.path.exists()


@posix
def test_adding_users_writes_an_owner_only_file_in_an_owner_only_directory(store):
    alice = store.add("Alice", "admin", PASSWORD)
    bob = store.add("bob", "viewer", OTHER)
    assert [u.username for u in store.users()] == ["alice", "bob"] and (alice.role, bob.role) == ("admin", "viewer")
    assert mode(store.path) == 0o600 and mode(store.path.with_name("users.json.lock")) == 0o600
    assert mode(store.directory) == 0o700
    data = json.loads(store.path.read_text())
    assert data["version"] == 1 and set(data["users"][0]) == {"username", "role", "password", "created_at",
                                                              "last_login", "disabled"}
    assert PASSWORD not in store.path.read_text() and data["users"][0]["password"].startswith("scrypt$")
    assert alice.created_at.endswith("Z") and alice.last_login is None and alice.disabled is False


@posix
def test_reading_an_existing_accounts_file_tightens_its_mode_even_on_a_cache_hit(store):
    store.add("alice", "admin", PASSWORD)
    store.users()
    os.chmod(store.path, 0o644)
    assert store.users()[0].username == "alice"
    assert mode(store.path) == 0o600


def test_a_username_that_exists_in_any_case_cannot_be_added_again(store):
    store.add("alice", "viewer", PASSWORD)
    with pytest.raises(AccountError, match="already exists"):
        store.add("ALICE", "admin", PASSWORD)
    assert len(store.users()) == 1


def test_a_bad_name_role_or_password_changes_nothing(store):
    for args in (("x", "viewer", PASSWORD), ("alice", "root", PASSWORD), ("alice", "viewer", "short")):
        with pytest.raises(AccountError):
            store.add(*args)
    assert not store.path.exists()


def test_roles_can_be_changed_users_disabled_enabled_and_passwords_reset(store):
    store.add("root", "admin", PASSWORD)
    store.add("alice", "viewer", PASSWORD)
    assert store.set_role("alice", "admin").role == "admin"
    assert store.set_disabled("ALICE", True).disabled is True and store.get("alice").disabled
    assert store.set_disabled("alice", False).disabled is False
    old = store.get("alice").password_hash
    store.reset_password("alice", OTHER)
    assert store.get("alice").password_hash != old and accounts.verify_password(OTHER, store.get("alice").password_hash)
    with pytest.raises(AccountError, match="at least 12"):
        store.reset_password("alice", "short")
    assert accounts.verify_password(OTHER, store.get("alice").password_hash)        # the refused one changed nothing
    assert store.remove("Alice").username == "alice" and store.get("alice") is None


def test_an_unknown_user_is_an_error_for_every_change(store):
    store.add("alice", "admin", PASSWORD)
    for call in (lambda: store.set_role("zed", "viewer"), lambda: store.set_disabled("zed", True),
                 lambda: store.reset_password("zed", PASSWORD), lambda: store.remove("zed")):
        with pytest.raises(AccountError, match="no user zed"):
            call()


def test_a_change_made_by_someone_else_is_seen_and_never_overwritten(tmp_path):
    ours, theirs = AccountStore(tmp_path), AccountStore(tmp_path)
    ours.add("alice", "admin", PASSWORD)
    assert [u.username for u in theirs.users()] == ["alice"]              # read, and cached
    theirs.add("bob", "viewer", PASSWORD)
    assert [u.username for u in ours.users()] == ["alice", "bob"]         # the changed file is read again
    ours.set_role("bob", "admin")                                         # a write starts from the file, not the cache
    assert {u.username: u.role for u in theirs.users()} == {"alice": "admin", "bob": "admin"}


def test_the_file_is_not_read_again_while_it_has_not_changed(store, monkeypatch):
    store.add("alice", "admin", PASSWORD)
    store.users()
    monkeypatch.setattr(AccountStore, "_read", lambda self: pytest.fail("read again"))
    assert [u.username for u in store.users()] == ["alice"]


def test_a_crash_while_writing_leaves_the_old_file_and_no_temporary_file(store, monkeypatch):
    store.add("alice", "admin", PASSWORD)
    before = store.path.read_bytes()

    def crash(source, target):
        raise OSError("disk gone")

    monkeypatch.setattr(accounts.os, "replace", crash)
    with pytest.raises(OSError, match="disk gone"):
        store.add("bob", "viewer", PASSWORD)
    monkeypatch.undo()
    assert store.path.read_bytes() == before
    assert sorted(p.name for p in store.directory.iterdir()) == ["users.json", "users.json.lock"]


def test_two_writers_at_once_do_not_lose_an_update(tmp_path):
    names = [f"user{i:02d}" for i in range(12)]
    errors = []

    def add(name):
        try:
            AccountStore(tmp_path).add(name, "viewer", PASSWORD)
        except Exception as e:        # noqa: BLE001 (the test reports whatever went wrong in a thread)
            errors.append(e)

    threads = [threading.Thread(target=add, args=(n,)) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and sorted(u.username for u in AccountStore(tmp_path).users()) == names


@pytest.mark.parametrize("content, message", [
    ("not json", "not valid JSON"), ('{"version": 2, "users": []}', "format 1"), ("[]", "format 1"),
    ('{"version": 1, "users": {}}', "format 1"), ('{"version": 1, "users": [5]}', "not an object"),
    ('{"version": 1, "users": [{"username": "al", "role": "admin", "password": "x"}]}', "damaged"),
    ('{"version": 1, "users": [{"username": "alice", "role": "root", "password": "x"}]}', "damaged"),
    ('{"version": 1, "users": [{"username": "alice", "role": "admin"}]}', "damaged"),
    ('{"version": 1, "users": [{"username": "alice", "role": "admin", "password": "x", "disabled": "no"}]}',
     "damaged"),
    ('{"version": 1, "users": [{"username": "alice", "role": "admin", "password": "x"},'
     '{"username": "alice", "role": "viewer", "password": "y"}]}', "twice"),
])
def test_a_damaged_file_is_refused_with_a_reason_and_left_alone(store, content, message):
    store.directory.mkdir()
    store.path.write_text(content)
    with pytest.raises(AccountError, match=message):
        store.users()
    with pytest.raises(AccountError, match=message):
        store.add("bob", "viewer", PASSWORD)
    assert store.path.read_text() == content


def test_a_file_with_odd_but_valid_optional_fields_is_read(store):
    store.directory.mkdir()
    store.path.write_text(json.dumps({"version": 1, "users": [
        {"username": "alice", "role": "admin", "password": "x", "last_login": 5, "created_at": None}]}))
    (alice,) = store.users()
    assert alice.last_login is None and alice.created_at == "" and alice.disabled is False


def test_a_user_round_trips_through_its_dict():
    user = User("alice", "admin", "scrypt$x", "2026-01-01T00:00:00Z", "2026-02-02T00:00:00Z", True)
    assert User.from_dict(user.to_dict()) == user


# -- the last administrator --------------------------------------------------------------------------------------

def test_the_last_administrator_cannot_be_deleted_demoted_or_disabled(store):
    store.add("root", "admin", PASSWORD)
    store.add("viewer", "viewer", PASSWORD)
    for call in (lambda: store.remove("root"), lambda: store.set_role("root", "viewer"),
                 lambda: store.set_disabled("root", True)):
        with pytest.raises(AccountError, match="last administrator"):
            call()
    assert store.get("root").role == "admin" and not store.get("root").disabled


def test_another_administrator_makes_each_of_those_allowed(store):
    store.add("one", "admin", PASSWORD)
    store.add("two", "admin", PASSWORD)
    store.set_role("one", "viewer")
    store.set_role("one", "admin")
    store.set_disabled("one", True)
    store.set_disabled("one", False)
    store.remove("one")
    assert [u.username for u in store.users()] == ["two"]


def test_a_disabled_administrator_does_not_count(store):
    store.add("one", "admin", PASSWORD)
    store.add("two", "admin", PASSWORD)
    store.set_disabled("one", True)
    with pytest.raises(AccountError, match="last administrator"):
        store.remove("two")
    store.remove("one")                         # the disabled one can go: it was not protecting anything
    assert [u.username for u in store.users()] == ["two"]


def test_without_any_administrator_nothing_is_protected(store):
    store.add("abc", "viewer", PASSWORD)
    store.remove("abc")
    assert store.users() == []


def test_the_last_enabled_administrator_stays_protected_in_a_file_edited_by_hand(store):
    store.add("root", "admin", PASSWORD)
    store.add("other", "viewer", PASSWORD)
    with pytest.raises(AccountError, match="last administrator"):
        store._change(lambda users: ([u for u in users if u.username != "root"], None))


# -- logging in --------------------------------------------------------------------------------------------------

@pytest.fixture
def local(store, tmp_path):
    store.add("alice", "admin", PASSWORD)
    store.add("bob", "viewer", OTHER)
    store.set_disabled("bob", True)
    return LocalAccounts(store, AuditLog(tmp_path / "data"))


def test_a_good_password_gives_a_principal_and_a_last_login(local):
    assert local.authenticate("Alice", PASSWORD) == Principal("alice", "admin", "local")
    assert local.store.get("alice").last_login.endswith("Z")


def test_authentication_revalidates_the_enabled_state_and_role_under_the_lock(store, monkeypatch):
    stale = store.add("alice", "admin", PASSWORD)
    store.add("other", "admin", OTHER)
    local = LocalAccounts(store)
    store.set_disabled("alice", True)
    monkeypatch.setattr(store, "get", lambda username: stale)
    assert local.authenticate("alice", PASSWORD) is None

    store.set_disabled("alice", False)
    store.set_role("alice", "viewer")
    assert local.authenticate("alice", PASSWORD) == Principal("alice", "viewer", "local")


def test_a_disabled_user_is_compared_with_the_decoy_hash(local, monkeypatch):
    seen = []
    real_verify = accounts.verify_password
    monkeypatch.setattr(accounts, "verify_password",
                        lambda password, stored: seen.append(stored) or real_verify(password, stored))
    assert local.authenticate("bob", OTHER) is None
    assert seen == [local._decoy]


def test_a_wrong_password_an_unknown_user_and_a_disabled_one_all_give_the_same_answer_for_the_same_work(local,
                                                                                                      monkeypatch):
    calls = []
    real = accounts._scrypt
    monkeypatch.setattr(accounts, "_scrypt", lambda *a, **k: calls.append(1) or real(*a, **k))
    attempts = [("alice", "wrong password!"), ("nobody", PASSWORD), ("bob", OTHER), ("alice", ""), ("alice", "x" * 5000),
                (None, PASSWORD), ("alice", None)]
    for username, password in attempts:
        assert local.authenticate(username, password) is None
    assert len(calls) == len(attempts)                                   # one scrypt each, whatever the reason
    assert local.store.get("alice").last_login is None


def test_a_good_password_upgrades_an_old_hash_and_the_upgrade_is_audited(local, monkeypatch, tmp_path):
    monkeypatch.setattr(accounts, "PARAMS", ScryptParams(n=32, r=8, p=1))
    old = local.store.get("alice").password_hash
    assert local.authenticate("alice", PASSWORD) is not None
    new = local.store.get("alice").password_hash
    assert new != old and new.startswith("scrypt$32$") and accounts.verify_password(PASSWORD, new)
    assert local.authenticate("alice", PASSWORD) is not None
    assert local.store.get("alice").password_hash == new                      # only once
    lines = [json.loads(line) for line in (tmp_path / "data" / "audit.log").read_text().splitlines()]
    assert [(e["event"], e["user"]) for e in lines] == [("user.password_upgraded", "alice")]


def test_an_upgrade_without_an_audit_log_still_happens(store, monkeypatch):
    store.add("alice", "admin", PASSWORD)
    monkeypatch.setattr(accounts, "PARAMS", ScryptParams(n=32, r=8, p=1))
    assert LocalAccounts(store).authenticate("alice", PASSWORD) is not None
    assert store.get("alice").password_hash.startswith("scrypt$32$")


def test_a_login_is_checked_against_the_current_record_not_a_stale_one(store):
    store.add("alice", "admin", PASSWORD)
    decoy = accounts.hash_password("decoy decoy decoy")
    store.reset_password("alice", OTHER)                                      # changed after the caller read the user
    assert store.authenticate_login("alice", PASSWORD, decoy) is None         # the old password no longer works
    assert accounts.verify_password(OTHER, store.get("alice").password_hash)
    assert store.authenticate_login("ghost", PASSWORD, decoy) is None         # a user deleted meanwhile is not an error
    assert store.authenticate_login("alice", OTHER, decoy).username == "alice"


def test_an_audit_callback_that_fails_when_nothing_changed_rolls_back_nothing_and_says_so(store):
    store.add("alice", "admin", PASSWORD)
    before = store.path.read_bytes()

    def audit_down(user):
        raise OSError("disk full")

    with pytest.raises(AccountError, match="audit log could not be written; the account change was rolled back"):
        store.set_role("alice", "admin", on_change=audit_down)                # the role was already admin: no write
    assert store.path.read_bytes() == before


def test_when_the_rollback_itself_cannot_be_written_the_error_says_so(store, monkeypatch):
    store.add("alice", "admin", PASSWORD)
    real_write, calls = store._write, []

    def write(users):
        calls.append(1)
        if len(calls) == 2:                                                   # the first write is the change, the second the rollback
            raise OSError("read-only file system")
        real_write(users)

    def audit_down(user):
        raise OSError("disk full")

    monkeypatch.setattr(store, "_write", write)
    with pytest.raises(AccountError, match="could not be rolled back"):
        store.add("bob", "viewer", OTHER, on_change=audit_down)


def test_the_authenticator_interface_is_what_the_routes_will_take(local):
    authenticator: accounts.Authenticator = local
    assert authenticator.authenticate("alice", PASSWORD).role == "admin"


# -- the audit log -----------------------------------------------------------------------------------------------

@posix
def test_an_audit_line_is_json_and_the_file_is_owner_only(tmp_path):
    with AuditLog(tmp_path / "d") as audit:
        audit.write("user.added", "cli:jeff", user="alice", role="admin")
    path = tmp_path / "d" / "audit.log"
    (line,) = path.read_text().splitlines()
    entry = json.loads(line)
    assert list(entry)[:3] == ["time", "event", "actor"] and entry["time"].endswith("Z")
    assert (entry["event"], entry["actor"], entry["user"], entry["role"]) == ("user.added", "cli:jeff", "alice", "admin")
    assert mode(path) == 0o600 and mode(path.parent) == 0o700


@posix
def test_a_file_made_earlier_with_looser_rights_is_tightened(tmp_path):
    (tmp_path / "audit.log").write_text("")
    os.chmod(tmp_path / "audit.log", 0o644)
    AuditLog(tmp_path).close()
    assert mode(tmp_path / "audit.log") == 0o600


def test_a_field_that_looks_like_a_secret_is_hidden_in_the_file(tmp_path):
    with AuditLog(tmp_path) as audit:
        audit.write("user.added", "cli", user="alice", password="hunter2hunter2", api_token="abc123abc123")
    text = (tmp_path / "audit.log").read_text()
    assert "hunter2" not in text and "abc123" not in text and "[redacted]" in text


def test_the_request_id_is_in_the_line_only_when_there_is_one(tmp_path):
    with AuditLog(tmp_path) as audit, logs.bind(request_id="abc123def456"):
        audit.write("user.deleted", "cli", user="x")
    with AuditLog(tmp_path) as audit:
        audit.write("user.deleted", "cli", user="y")
    first, second = (json.loads(line) for line in (tmp_path / "audit.log").read_text().splitlines())
    assert first["request_id"] == "abc123def456" and "request_id" not in second


@posix
def test_the_log_rotates_by_size_keeps_the_given_number_of_files_and_never_deletes_by_age(tmp_path):
    tmp_path = tmp_path / "audit"
    audit = AuditLog(tmp_path, max_mb=1, files=3)
    audit._handler.maxBytes = 300                                           # a megabyte would make the test slow
    for i in range(40):
        audit.write("user.added", "cli", user=f"user{i:02d}", role="viewer")
    audit.close()
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["audit.log", "audit.log.1", "audit.log.2"]               # 3 files in all, the oldest dropped
    assert all(mode(tmp_path / n) == 0o600 for n in names)
    newest = json.loads((tmp_path / "audit.log").read_text().splitlines()[-1])
    assert newest["user"] == "user39"
    assert all(os.path.getsize(tmp_path / n) <= 300 + 200 for n in names)


def test_the_same_record_is_logged_at_info_on_the_module_logger(tmp_path):
    import io

    stream = io.StringIO()
    logs.configure("json", "INFO", stream=stream)
    with AuditLog(tmp_path) as audit:
        audit.write("user.deleted", "cli:jeff", user="alice", role="viewer")
    record = json.loads(stream.getvalue())
    assert record["level"] == "INFO" and record["logger"] == "homelab_probe.audit"
    assert record["event"] == "audit.event" and record["action"] == "user.deleted" and record["account"] == "alice"
    assert "password" not in stream.getvalue()


def test_a_closed_log_can_be_opened_again(tmp_path):
    first = AuditLog(tmp_path)
    first.write("user.added", "cli", user="a")
    first.close()
    with AuditLog(tmp_path) as second:
        second.write("user.added", "cli", user="b")
    assert [json.loads(line)["user"] for line in (tmp_path / "audit.log").read_text().splitlines()] == ["a", "b"]


def test_only_if_no_admin_refuses_a_second_administrator_under_the_lock(tmp_path):
    store = AccountStore(tmp_path)
    store.add("viewer1", "viewer", PASSWORD, only_if_no_admin=True)            # a viewer is no administrator
    store.add("alice", "admin", PASSWORD, only_if_no_admin=True)
    with pytest.raises(AccountError, match="administrator exists already"):
        store.add("bob", "admin", PASSWORD, only_if_no_admin=True)
    store.add("carol", "admin", PASSWORD)                                       # without the flag nothing changes
    assert [u.username for u in store.users()] == ["viewer1", "alice", "carol"]


def test_update_changes_role_and_disabled_in_one_step_and_calls_back_only_when_something_changed(tmp_path):
    store = AccountStore(tmp_path)
    store.add("alice", "admin", PASSWORD)
    store.add("bob", "viewer", PASSWORD)
    seen = []
    store.update("bob", on_change=lambda old, new: seen.append((old, new)))                       # nothing to change
    store.update("bob", "viewer", False, on_change=lambda old, new: seen.append((old, new)))     # the same values
    assert seen == []
    user = store.update("bob", "admin", True, on_change=lambda old, new: seen.append((old, new)))
    assert (user.role, user.disabled) == ("admin", True) and store.get("bob") == user
    assert [(o.role, o.disabled, n.role, n.disabled) for o, n in seen] == [("viewer", False, "admin", True)]


def test_update_applies_the_last_administrator_rule_to_the_whole_change_and_rolls_back_on_a_failed_audit(tmp_path):
    store = AccountStore(tmp_path)
    store.add("alice", "admin", PASSWORD)
    store.add("bob", "viewer", PASSWORD)
    with pytest.raises(accounts.LastAdministratorError):
        store.update("alice", "viewer", False)
    with pytest.raises(accounts.NoSuchUserError):
        store.update("nobody", "admin")
    with pytest.raises(accounts.PolicyError):
        store.update("bob", "root")

    def broken(old, new):
        raise OSError("disk full")

    with pytest.raises(accounts.AuditWriteError):
        store.update("bob", "admin", on_change=broken)
    assert store.get("bob").role == "viewer" and store.get("alice").role == "admin"


def test_every_refusal_has_its_own_kind(tmp_path):
    store = AccountStore(tmp_path)
    store.add("alice", "admin", PASSWORD)
    with pytest.raises(accounts.UserExistsError):
        store.add("ALICE", "viewer", PASSWORD)
    with pytest.raises(accounts.AdministratorExistsError):
        store.add("bob", "admin", PASSWORD, only_if_no_admin=True)
    with pytest.raises(accounts.PolicyError):
        store.add("bob", "viewer", "short")
    with pytest.raises(accounts.PolicyError):
        store.add("a", "viewer", PASSWORD)
    with pytest.raises(accounts.NoSuchUserError):
        store.reset_password("nobody", PASSWORD)
    assert all(issubclass(kind, AccountError) for kind in (accounts.PolicyError, accounts.UserExistsError,
                                                          accounts.NoSuchUserError, accounts.LastAdministratorError,
                                                          accounts.AdministratorExistsError, accounts.AuditWriteError))


def test_an_accounts_file_that_is_not_text_is_a_clear_error_not_a_traceback(tmp_path):
    (tmp_path / "users.json").write_bytes(b"\xff\xfe\x00")
    with pytest.raises(AccountError, match="not valid JSON"):
        AccountStore(tmp_path).users()
