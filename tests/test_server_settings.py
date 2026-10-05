"""The settings of diagnose over the API (issue #186): GET and PUT /api/v1/settings."""

import datetime
import json
import os
import stat
import threading

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("tomlkit")

from fastapi.testclient import TestClient  # noqa: E402
from server_support import CONFIG, auth_for, logged_in  # noqa: E402

from homelab_probe.demo.session import DemoSession  # noqa: E402
from homelab_probe.server import settings_api  # noqa: E402
from homelab_probe.server.app import create_app  # noqa: E402
from homelab_probe.server.errors import ApiError  # noqa: E402
from homelab_probe.server.service import ControllerService  # noqa: E402
from homelab_probe.settings import DiagnoseSettings, load_settings  # noqa: E402

URL = "/api/v1/settings"
COMMENTED = """# My settings: keep this line
# Thresholds for the lab.

[thresholds]
resource_warn_pct = 85   # busy gateway
wifi_weak_signal_dbm = -70

# the spare switch is in a drawer
[[ignore]]
subject = "Spare *"
reason = "kept in a drawer"   # ask Sam before removing
until = 2999-01-01

[[ignore]]
code = "device.offline"
subject = "Old AP"
reason = "retired"
"""


def make_app(tmp_path, **kwargs):
    return create_app(CONFIG, state_dir=tmp_path, hosts=["testserver"], auth=auth_for(tmp_path),
                      service=ControllerService(CONFIG, session=DemoSession()), **kwargs)


@pytest.fixture
def app(tmp_path):
    return make_app(tmp_path)


@pytest.fixture
def admin(app):
    return logged_in(app, "alice")


@pytest.fixture
def viewer(app):
    return logged_in(app, "bob")


def settings_path(tmp_path):
    return tmp_path / "hlp.toml"


def put(client, version, **body):
    return client.put(URL, json={"version": version, **body})


def audit_lines(tmp_path):
    return [json.loads(line) for line in (tmp_path / "audit.log").read_text().splitlines()]


# -- reading ----------------------------------------------------------------------------------------------------

def test_without_a_file_the_defaults_are_shown_with_the_version_absent(viewer):
    body = viewer.get(URL).json()
    defaults = DiagnoseSettings()
    assert body["version"] == "absent" and body["exists"] is False and body["file"] == "hlp.toml"
    assert body["thresholds"]["resource_warn_pct"] == defaults.resource_warn_pct == body["defaults"]["resource_warn_pct"]
    assert body["set_in_file"] == [] and body["ignore"] == [] and body["read_only"] is False
    assert "device.offline" in body["codes"] and "audit.wifi_open" in body["codes"]
    assert body["notifications"] == {"ntfy": False, "webhook": False, "email": False}
    assert "ignore" not in body["thresholds"]


def test_the_file_is_shown_with_what_it_sets_and_which_rules_have_expired(tmp_path, viewer):
    settings_path(tmp_path).write_text(COMMENTED.replace("2999-01-01", "2001-01-01"))
    body = viewer.get(URL).json()
    assert body["thresholds"]["resource_warn_pct"] == 85 and body["thresholds"]["wifi_weak_signal_dbm"] == -70
    assert body["set_in_file"] == ["resource_warn_pct", "wifi_weak_signal_dbm"] and body["exists"] is True
    assert [(r["subject"], r["until"], r["expired"]) for r in body["ignore"]] == [
        ("Spare *", "2001-01-01", True), ("Old AP", None, False)]
    assert body["version"] != "absent" and len(body["version"]) == 64


def test_notification_destinations_are_only_reported_as_configured(tmp_path):
    from dataclasses import replace

    config = replace(CONFIG, notify_ntfy_url="https://ntfy.example/secret-topic-1234")
    app = create_app(config, state_dir=tmp_path, hosts=["testserver"], auth=auth_for(tmp_path),
                     service=ControllerService(config, session=DemoSession()))
    response = logged_in(app, "bob").get(URL)
    assert response.json()["notifications"] == {"ntfy": True, "webhook": False, "email": False}
    assert "secret-topic" not in response.text


def test_reading_needs_a_login(app):
    assert TestClient(app).get(URL).status_code == 401


def test_a_file_that_cannot_be_used_is_a_500_that_points_at_the_commands(tmp_path, viewer):
    settings_path(tmp_path).write_text("[thresholds]\nresource_warn_pct = 500\n")
    response = viewer.get(URL)
    assert response.status_code == 500 and response.json()["error"] == "settings_invalid"
    assert "500" not in response.json()["message"] and str(tmp_path) not in response.text


def test_a_file_that_cannot_be_read_is_a_500(tmp_path, viewer):
    settings_path(tmp_path).mkdir()                                   # a directory where the file should be
    assert viewer.get(URL).json()["error"] == "settings_unreadable"


def test_the_data_directory_not_the_working_directory_holds_the_default_file(tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "hlp.toml").write_text("[thresholds]\nresource_warn_pct = 11\n")
    monkeypatch.chdir(elsewhere)
    data = tmp_path / "data"
    app = make_app(data)
    assert logged_in(app, "bob").get(URL).json()["exists"] is False
    (data / "hlp.toml").write_text("[thresholds]\nresource_warn_pct = 12\n")
    assert logged_in(app, "bob").get(URL).json()["thresholds"]["resource_warn_pct"] == 12


def test_a_named_config_file_is_the_one_edited(tmp_path):
    named = tmp_path / "lab.toml"
    named.write_text("[thresholds]\nslow_link_mbps = 1000\n")
    app = create_app(CONFIG, named, tmp_path, hosts=["testserver"], auth=auth_for(tmp_path),
                     service=ControllerService(CONFIG, session=DemoSession()))
    admin = logged_in(app, "alice")
    body = admin.get(URL).json()
    assert body["file"] == "lab.toml" and body["thresholds"]["slow_link_mbps"] == 1000
    assert put(admin, body["version"], thresholds={"slow_link_mbps": 100}).status_code == 200
    assert "slow_link_mbps = 100" in named.read_text() and not settings_path(tmp_path).exists()


def test_the_report_routes_use_the_same_file(tmp_path):
    (tmp_path / "hlp.toml").write_text('[[ignore]]\ncode = "device.offline"\nreason = "all of them"\n')
    app = make_app(tmp_path)
    client = logged_in(app, "bob")
    body = client.get("/api/v1/unifi/sites/default/diagnose?no_events=true").json()
    assert body["findings"] and all(f["code"] != "device.offline" for f in body["findings"])
    (tmp_path / "hlp.toml").unlink()
    assert any(f["code"] == "device.offline" for f in client.get(
        "/api/v1/unifi/sites/default/diagnose?no_events=true&refresh=true").json()["findings"])


# -- who may change it ------------------------------------------------------------------------------------------

def test_only_an_administrator_with_the_csrf_token_and_an_origin_may_change_it(app, tmp_path, viewer, admin):
    version = admin.get(URL).json()["version"]
    assert TestClient(app).put(URL, json={"version": version}, headers={"Origin": "http://testserver"}).status_code == 401
    assert put(viewer, version).status_code == 403 and put(viewer, version).json()["error"] == "forbidden"
    plain = TestClient(app)
    assert plain.put(URL, json={"version": version}).status_code == 403                     # no Origin
    no_token = logged_in(app, "alice")
    del no_token.headers["X-CSRF-Token"]
    assert put(no_token, version).json()["error"] == "csrf_token"
    assert not settings_path(tmp_path).exists()


# -- changing it ------------------------------------------------------------------------------------------------

def test_a_change_keeps_comments_order_and_the_old_file_as_a_backup(tmp_path, admin):
    path = settings_path(tmp_path)
    path.write_text(COMMENTED)
    os.chmod(path, 0o644)
    version = admin.get(URL).json()["version"]
    response = put(admin, version, thresholds={"resource_warn_pct": 80, "slow_link_mbps": 1000})
    assert response.status_code == 200 and response.json()["thresholds"]["resource_warn_pct"] == 80
    text = path.read_text()
    assert text.startswith("# My settings: keep this line\n# Thresholds for the lab.\n")
    assert "resource_warn_pct = 80   # busy gateway" in text and "# the spare switch is in a drawer" in text
    assert text.index("resource_warn_pct") < text.index("wifi_weak_signal_dbm") < text.index("slow_link_mbps")
    assert (tmp_path / "hlp.toml.bak").read_text() == COMMENTED
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o644 and stat.S_IMODE(os.stat(tmp_path / "hlp.toml.bak").st_mode) == 0o644
    assert load_settings(path).slow_link_mbps == 1000                                       # the CLI reads it
    assert response.json()["version"] != version
    assert response.json()["set_in_file"] == ["resource_warn_pct", "slow_link_mbps", "wifi_weak_signal_dbm"]


def test_a_new_file_starts_from_the_commented_stub_and_is_owner_only(tmp_path, admin):
    response = put(admin, "absent", thresholds={"wifi_weak_signal_dbm": -80})
    assert response.status_code == 200
    path = settings_path(tmp_path)
    text = path.read_text()
    assert text.startswith("# Homelab Probe settings.") and "wifi_weak_signal_dbm = -80" in text
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600 and not (tmp_path / "hlp.toml.bak").exists()
    assert load_settings(path).wifi_weak_signal_dbm == -80


def test_null_puts_a_threshold_back_to_its_default_and_removes_the_line(tmp_path, admin):
    path = settings_path(tmp_path)
    path.write_text("[thresholds]\nresource_warn_pct = 85\nslow_link_mbps = 1000\n")
    version = admin.get(URL).json()["version"]
    body = put(admin, version, thresholds={"slow_link_mbps": None}).json()
    assert "slow_link_mbps" not in path.read_text() and "resource_warn_pct = 85" in path.read_text()
    assert body["thresholds"]["slow_link_mbps"] == body["defaults"]["slow_link_mbps"]
    assert audit_lines(tmp_path)[-1]["thresholds"] == "slow_link_mbps"


def test_removing_a_line_that_equals_the_default_still_changes_the_file(tmp_path, admin):
    path = settings_path(tmp_path)
    path.write_text(f"[thresholds]\nslow_link_mbps = {DiagnoseSettings().slow_link_mbps}\n")
    version = admin.get(URL).json()["version"]
    put(admin, version, thresholds={"slow_link_mbps": None})
    assert "slow_link_mbps" not in path.read_text() and (tmp_path / "hlp.toml.bak").exists()


@pytest.mark.parametrize("thresholds, message", [
    ({"resource_warn_pct": 500}, "[thresholds] resource_warn_pct must be between 0 and 100"),
    ({"resource_warn_pct": 99}, "resource_warn_pct must not exceed resource_critical_pct"),
    ({"link_flap_count": 2.5}, "link_flap_count must be a whole number"),
    ({"nonsense_key": 1}, "unknown [thresholds] key(s): nonsense_key"),
])
def test_a_value_the_loader_refuses_is_a_422_with_its_message_and_nothing_is_written(
        tmp_path, admin, thresholds, message):
    path = settings_path(tmp_path)
    path.write_text("[thresholds]\nresource_warn_pct = 85\n")
    before = path.read_text()
    version = admin.get(URL).json()["version"]
    response = put(admin, version, thresholds=thresholds)
    assert response.status_code == 422 and response.json()["error"] == "invalid_settings"
    assert message in response.json()["message"] and str(tmp_path) not in response.text and "/hlp" not in response.text
    assert path.read_text() == before and not (tmp_path / "hlp.toml.bak").exists()


@pytest.mark.parametrize("body", [
    {"thresholds": {"resource_warn_pct": True}}, {"thresholds": {"resource_warn_pct": "80"}},
    {"nonsense": 1}, {"ignore": [{"reason": "x", "extra": 1}]},
    {"ignore": [{"reason": "x", "until": "x" * 30}]},
])
def test_a_body_of_the_wrong_shape_is_a_422(admin, body):
    assert put(admin, "absent", **body).status_code == 422


def test_a_number_that_is_not_finite_is_refused(admin):
    response = admin.put(URL, content='{"version": "absent", "thresholds": {"resource_warn_pct": Infinity}}',
                         headers={"Content-Type": "application/json"})
    assert response.status_code == 422


def test_ignore_rules_are_replaced_as_a_list_and_a_rule_that_did_not_change_keeps_its_comment(tmp_path, admin):
    path = settings_path(tmp_path)
    path.write_text(COMMENTED)
    version = admin.get(URL).json()["version"]
    rules = [{"subject": "Spare *", "reason": "kept in a drawer", "until": "2999-01-01"},
             {"code": "port.slow_link", "reason": "a known 100 Mbps camera"}]
    body = put(admin, version, ignore=rules).json()
    text = path.read_text()
    assert "# the spare switch is in a drawer" in text and 'subject = "Old AP"' not in text
    assert "# ask Sam before removing" in text                                      # the unchanged rule kept its comment
    assert 'code = "port.slow_link"' in text and "until = 2999-01-01" in text      # a TOML date, not a string
    assert [r["code"] or r["subject"] for r in body["ignore"]] == ["Spare *", "port.slow_link"]
    assert load_settings(path).ignore[0].until == datetime.date(2999, 1, 1)
    assert audit_lines(tmp_path)[-1]["ignore_rules"] == "2 rule(s)"


def test_an_empty_list_removes_every_rule(tmp_path, admin):
    path = settings_path(tmp_path)
    path.write_text(COMMENTED)
    put(admin, admin.get(URL).json()["version"], ignore=[])
    assert "[[ignore]]" not in path.read_text() and "resource_warn_pct = 85" in path.read_text()
    put(admin, admin.get(URL).json()["version"], ignore=[])                       # again: nothing to remove


@pytest.mark.parametrize("rule, message", [
    ({"code": "device.offlin", "reason": "typo"}, "unknown code 'device.offlin' (did you mean 'device.offline'?)"),
    ({"code": "device.offline"}, "a reason is required"),
    ({"reason": "no selector"}, "give a code, a subject and/or a message to match"),
    ({"subject": "x", "reason": "r", "until": "2026-13-45"}, "until must be a date like 2026-10-10"),
    ({"subject": "x", "reason": "r", "until": "tomorrow"}, "until must be a date like 2026-10-10"),
])
def test_an_ignore_rule_the_loader_refuses_is_a_422(tmp_path, admin, rule, message):
    response = put(admin, "absent", ignore=[rule])
    assert response.status_code == 422 and message in response.json()["message"]
    assert not settings_path(tmp_path).exists()


def test_a_change_is_audited_by_name_and_count_never_by_text(tmp_path, admin):
    put(admin, "absent", thresholds={"wifi_weak_signal_dbm": -80},
        ignore=[{"subject": "Secret printer name", "reason": "private reason"}])
    entry = audit_lines(tmp_path)[-1]
    assert entry["event"] == "settings.updated" and entry["actor"] == "alice"
    assert entry["thresholds"] == "wifi_weak_signal_dbm" and entry["ignore_rules"] == "1 rule(s)"
    text = (tmp_path / "audit.log").read_text()
    assert "Secret printer" not in text and "private reason" not in text


# -- not overwriting somebody else's edit ----------------------------------------------------------------------

def test_a_file_that_changed_since_it_was_read_is_not_overwritten(tmp_path, admin):
    path = settings_path(tmp_path)
    path.write_text("[thresholds]\nresource_warn_pct = 85\n")
    version = admin.get(URL).json()["version"]
    path.write_text("[thresholds]\nresource_warn_pct = 70   # edited by hand\n")
    response = put(admin, version, thresholds={"slow_link_mbps": 1000})
    assert response.status_code == 409 and response.json()["error"] == "settings_changed"
    assert path.read_text().endswith("# edited by hand\n") and not (tmp_path / "hlp.toml.bak").exists()


def test_a_wrong_version_for_a_file_that_is_not_there_is_a_409(admin):
    assert put(admin, "0" * 64).status_code == 409


def test_a_file_that_appears_while_the_request_works_is_not_overwritten(tmp_path, admin, monkeypatch):
    path = settings_path(tmp_path)
    real = settings_api._read
    calls = []

    def racing(target):
        calls.append(True)
        if len(calls) == 2:                                     # the check just before the write
            target.write_text("[thresholds]\nresource_warn_pct = 1\n")
        return real(target)

    monkeypatch.setattr(settings_api, "_read", racing)
    response = put(admin, "absent", thresholds={"slow_link_mbps": 1000})
    assert response.status_code == 409 and path.read_text() == "[thresholds]\nresource_warn_pct = 1\n"


def test_two_changes_with_the_same_version_do_not_both_win(tmp_path, app):
    first, second = logged_in(app, "alice"), logged_in(app, "alice")
    version = first.get(URL).json()["version"]
    results = []

    def go(client, value):
        results.append(put(client, version, thresholds={"slow_link_mbps": value}).status_code)

    threads = [threading.Thread(target=go, args=(first, 100)), threading.Thread(target=go, args=(second, 1000))]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == [200, 409]


def test_a_change_that_changes_nothing_writes_nothing(tmp_path, admin):
    path = settings_path(tmp_path)
    path.write_text(COMMENTED)
    version = admin.get(URL).json()["version"]
    before = os.stat(path).st_mtime_ns
    for body in ({}, {"thresholds": {}}, {"thresholds": {"resource_warn_pct": 85}}, {"ignore": None}):
        response = put(admin, version, **body)
        assert response.status_code == 200 and response.json()["version"] == version
    assert os.stat(path).st_mtime_ns == before and not (tmp_path / "hlp.toml.bak").exists()
    assert not (tmp_path / "audit.log").exists() or audit_lines(tmp_path) == [] or all(
        e["event"] != "settings.updated" for e in audit_lines(tmp_path))


def test_nothing_is_created_by_a_change_that_changes_nothing(tmp_path, admin):
    assert put(admin, "absent").status_code == 200 and not settings_path(tmp_path).exists()


# -- files that cannot be edited -------------------------------------------------------------------------------

def test_a_file_the_loader_refuses_is_not_edited_here(tmp_path, admin):
    path = settings_path(tmp_path)
    path.write_text("[thresholds]\nresource_warn_pct = 500\n")
    response = put(admin, settings_api.version_of(path.read_bytes()), thresholds={"slow_link_mbps": 1000})
    assert response.status_code == 409 and response.json()["error"] == "settings_file_invalid"
    assert path.read_text() == "[thresholds]\nresource_warn_pct = 500\n"


def test_text_that_is_not_toml_is_refused_by_the_editor_itself():
    with pytest.raises(ApiError) as caught:
        settings_api.apply_change("[thresholds\n", settings_api.SettingsBody(version="x"))
    assert caught.value.code == "settings_file_invalid"


def test_a_symbolic_link_is_left_alone(tmp_path, admin):
    real = tmp_path / "real.toml"
    real.write_text("[thresholds]\nslow_link_mbps = 1000\n")
    settings_path(tmp_path).symlink_to(real)
    response = put(admin, admin.get(URL).json()["version"], thresholds={"slow_link_mbps": 10})
    assert response.status_code == 409 and response.json()["error"] == "settings_is_link"
    assert real.read_text() == "[thresholds]\nslow_link_mbps = 1000\n"


def test_a_failed_write_is_a_500_with_a_fixed_message_and_leaves_no_temporary_file(tmp_path, admin, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", broken)
    response = put(admin, "absent", thresholds={"slow_link_mbps": 1000})
    assert response.status_code == 500 and response.json()["error"] == "settings_not_written"
    assert "No space" not in response.text and list(tmp_path.glob("hlp.toml*")) == []


def test_the_response_after_a_change_is_the_new_document_with_the_new_version(tmp_path, admin):
    first = admin.get(URL).json()
    changed = put(admin, first["version"], thresholds={"slow_link_mbps": 1000}).json()
    assert changed["version"] == admin.get(URL).json()["version"] != first["version"]
    assert put(admin, changed["version"], thresholds={"slow_link_mbps": 100}).status_code == 200
