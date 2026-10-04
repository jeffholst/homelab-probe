"""`doctor`: is the tool itself set up right? (issue #158)"""

import json
import os
import re
import sys

import pytest
from conftest import FakeResponse
from docs_support import ROOT

from unifi_sentinel import cli
from unifi_sentinel.client import UniFiAPIError
from unifi_sentinel.config import Config
from unifi_sentinel.doctor import (
    CHECKS,
    FAIL,
    INFO,
    OK,
    READERS,
    SKIP,
    WARN,
    Check,
    Options,
    explain,
    make,
    render,
    run_checks,
    scrub,
    summary,
    to_dict,
)

KEY = "sekrit-api-key-0123456789"
HOST = "hostname.example.internal"
URL = f"https://{HOST}:8443"
NOTIFY = {"NOTIFY_NTFY_URL": "https://ntfy.example/topic-super-secret", "NOTIFY_NTFY_TOKEN": "tk_secret_token_value",
          "NOTIFY_WEBHOOK_URL": "https://hooks.example/in/abc123secret", "NOTIFY_WEBHOOK_TOKEN": "webhook-secret-token",
          "NOTIFY_SMTP_HOST": "smtp.private.example", "NOTIFY_SMTP_USER": "alerts-user-secret",
          "NOTIFY_SMTP_PASSWORD": "smtp-password-secret", "NOTIFY_EMAIL_FROM": "from-secret@example.org",
          "NOTIFY_EMAIL_TO": "to-secret@example.org"}


@pytest.fixture
def configured(monkeypatch, fake_client):
    monkeypatch.setenv("CONTROLLER_URL", URL)
    monkeypatch.setenv("API_KEY", KEY)
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, config: fake_client))
    return fake_client


def run(capsys, *argv):
    code = cli.main(["doctor", *argv])
    out = capsys.readouterr()
    return code, out.out, out.err


def checks_of(configured, **options):
    return {c.id: c for c in run_checks(Options(**options))}


def doc_json(capsys, *argv):
    code, out, _ = run(capsys, *argv, "--json")
    return code, json.loads(out)


def fail_reads(fake_client, *suffixes, status=503):
    get = fake_client.session.get

    def request(url, *args, **kwargs):
        if any(url.endswith(s) for s in suffixes):
            return FakeResponse(status, {})
        return get(url, *args, **kwargs)

    fake_client.session.get = request


# -- a healthy setup ---------------------------------------------------------------------------------------------

def test_a_healthy_setup_runs_every_check_once_in_order_and_exits_zero(configured, capsys):
    code, out, err = run(capsys)
    assert code == 0 and err == ""
    checks = run_checks(Options())
    assert [c.id for c in checks] == list(CHECKS)                           # every id once, in the documented order
    assert not [c for c in checks if c.status == FAIL]
    assert "Everything needed works." in out or "Nothing is broken, but see the warnings." in out


def test_the_statuses_of_a_healthy_setup_on_the_fake_controller(configured):
    found = checks_of(configured)
    assert found["install.version"].status == INFO and "unifi-sentinel" in found["install.version"].message
    assert found["config.env_file"].status == INFO and found["config.env_permissions"].status == SKIP
    assert found["config.settings_file"].status == INFO
    assert found["config.environment"].status == OK and found["config.tls"].status == OK
    assert found["notify.configured"].status == INFO and found["notify.dry_run"].status == SKIP
    assert found["controller.address"].message == "https, port 8443 (the host is not shown)"
    assert found["controller.reachable"].status == OK
    assert found["controller.version"].status == INFO and "10.0.0" in found["controller.version"].message
    assert found["controller.site"].status == OK and "1 site" in found["controller.site"].message
    assert all(found[f"endpoint.{r.id}"].status == OK for r in READERS) and found["endpoint.events"].status == OK
    assert found["endpoint.stat_alluser"].message == "3 records" and found["endpoint.integration_device_detail"].message == "answered"


def test_the_tested_version_is_ok_and_another_one_is_information(configured):
    configured.session.fx["info"]["applicationVersion"] = "10.6.106"
    assert checks_of(configured)["controller.version"].status == OK
    configured.session.fx["info"]["applicationVersion"] = "9.5.21"
    found = checks_of(configured)["controller.version"]
    assert found.status == INFO and "9.5.21" in found.message and "tested on 10.6.106 only" in found.message


def test_the_command_reads_only_with_get_plus_the_one_event_log_query(configured, capsys):
    run(capsys)
    assert len(configured.session.posts) == 1 and configured.session.posts[0][0].endswith("/system-log/all")
    assert all(not path.endswith("/system-log/all") for path in configured.session.calls)


def test_the_first_attempt_is_the_only_attempt(configured):
    assert configured.retries == 2                                        # the usual client retries twice
    run_checks(Options(), factory=lambda config: configured)
    assert configured.retries == 0                                        # doctor turns it off: a flaky link must show


# -- missing or broken settings ------------------------------------------------------------------------------------

def test_without_any_settings_it_still_reports_and_contacts_nothing(monkeypatch, fake_client, capsys):
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, config: fake_client))
    code, out, _ = run(capsys)
    found = {c.id: c for c in run_checks(Options())}
    assert code == 3 and found["config.environment"].status == FAIL
    assert "CONTROLLER_URL is not set" in found["config.environment"].message
    assert found["config.env_file"].status == WARN
    assert fake_client.session.calls == [] and fake_client.session.posts == []
    assert all(found[i].status == SKIP for i in found if i.startswith(("controller.", "endpoint.", "notify.dry")))
    assert found["config.tls"].status == SKIP and found["config.limits"].status == SKIP
    assert "1 check failed: fix those first." in out


def test_the_placeholder_key_fails_the_required_settings(monkeypatch, fake_client):
    monkeypatch.setenv("CONTROLLER_URL", URL)
    monkeypatch.setenv("API_KEY", "your-api-key-here")
    found = checks_of(fake_client)
    assert found["config.environment"].status == FAIL and "API_KEY" in found["config.environment"].message


@pytest.mark.parametrize("url", ["not a url", "ftp://host", "https://user:pw@host", "http://host", "https://host?x=1"])
def test_a_bad_controller_url_fails_without_echoing_it(monkeypatch, fake_client, url):
    monkeypatch.setenv("CONTROLLER_URL", url)
    monkeypatch.setenv("API_KEY", KEY)
    message = checks_of(fake_client)["config.environment"].message
    assert checks_of(fake_client)["config.environment"].status == FAIL
    for secret in ("user:pw", "?x=1"):
        assert secret not in message


def test_the_env_file_is_found_and_its_permissions_are_checked(configured, tmp_path):
    env = tmp_path / "cwd" / ".env"
    env.write_text(f"CONTROLLER_URL={URL}\nAPI_KEY={KEY}\n")
    os.chdir(env.parent)
    if sys.platform.startswith("win"):
        pytest.skip("modes mean little on Windows")
    env.chmod(0o644)
    found = checks_of(configured)
    assert found["config.env_file"].status == OK and str(env) in found["config.env_file"].message
    assert found["config.env_permissions"].status == WARN and "chmod 600" in found["config.env_permissions"].message
    env.chmod(0o600)
    assert checks_of(configured)["config.env_permissions"].status == OK


def test_on_windows_the_permissions_are_not_checked(configured, tmp_path, monkeypatch):
    env = tmp_path / "cwd" / ".env"
    env.write_text(f"CONTROLLER_URL={URL}\nAPI_KEY={KEY}\n")
    os.chdir(env.parent)
    env.chmod(0o644)
    monkeypatch.setattr(sys, "platform", "win32")
    found = checks_of(configured, offline=True)["config.env_permissions"]
    assert found.status == INFO and found.message == "not checked on Windows"


def test_an_env_file_named_but_missing_fails_and_skips_the_settings(monkeypatch, fake_client, tmp_path):
    found = checks_of(fake_client, env_file=tmp_path / "nope.env")
    assert found["config.env_file"].status == FAIL and "env file not found" in found["config.env_file"].message
    assert found["config.env_permissions"].status == SKIP and found["config.environment"].status == SKIP
    assert "could not be found" in found["config.environment"].message


def test_an_explicit_env_file_is_used(monkeypatch, fake_client, tmp_path):
    env = tmp_path / "lab.env"
    env.write_text(f"CONTROLLER_URL={URL}\nAPI_KEY={KEY}\nVERIFY_SSL=false\n")
    env.chmod(0o600)
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, config: fake_client))
    found = checks_of(fake_client, env_file=env, offline=True)
    assert found["config.env_file"].status == OK and found["config.tls"].status == WARN


def test_the_settings_file_valid_invalid_and_with_expired_rules(configured, tmp_path):
    cfg = tmp_path / "unifi-sentinel.toml"
    cfg.write_text('[[ignore]]\ncode = "device.offline"\nreason = "spare"\n')
    found = checks_of(configured, config=cfg)["config.settings_file"]
    assert found.status == OK and "1 ignore rule" in found.message
    cfg.write_text('[[ignore]]\ncode = "device.offline"\nreason = "old"\nuntil = 2000-01-01\n'
                   '[[ignore]]\ncode = "port.errors"\nreason = "x"\n')
    found = checks_of(configured, config=cfg)["config.settings_file"]
    assert found.status == WARN and "2 ignore rules, 1 of them expired" in found.message and "until date" in found.fix
    cfg.write_text("bogus = 1\n")
    assert checks_of(configured, config=cfg)["config.settings_file"].status == FAIL
    assert checks_of(configured, config=tmp_path / "missing.toml")["config.settings_file"].status == FAIL


def test_a_settings_file_in_the_current_directory_is_found(configured, tmp_path):
    os.chdir(tmp_path)
    (tmp_path / "unifi-sentinel.toml").write_text("[thresholds]\nresource_warn_pct = 80\n")
    assert checks_of(configured)["config.settings_file"].status == OK


def test_tls_warnings_for_no_checking_a_ca_file_and_plain_http(monkeypatch, configured, tmp_path):
    monkeypatch.setenv("VERIFY_SSL", "false")
    assert checks_of(configured)["config.tls"].status == WARN
    ca = tmp_path / "ca.pem"
    ca.write_text("x")
    monkeypatch.setenv("VERIFY_SSL", str(ca))
    found = checks_of(configured)["config.tls"]
    assert found.status == OK and str(ca) in found.message
    monkeypatch.delenv("VERIFY_SSL")
    monkeypatch.setenv("CONTROLLER_URL", "http://controller.lab")
    monkeypatch.setenv("ALLOW_INSECURE_HTTP", "true")
    found = checks_of(configured)["config.tls"]
    assert found.status == WARN and "clear text" in found.message


def test_the_limits_show_the_options_that_beat_the_file(configured):
    found = checks_of(configured, timeout=3, parallel=1, site="Lab")["config.limits"]
    assert found.message == "timeout 3 s, 1 request at once, site Lab"


# -- notifications ---------------------------------------------------------------------------------------------

def test_destinations_are_named_by_kind_and_nothing_is_sent(monkeypatch, configured, capsys):
    for name, value in NOTIFY.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("NOTIFY_SMTP_SECURITY", "starttls")
    import requests

    def no_post(*args, **kwargs):
        raise AssertionError("doctor must not send a notification")

    monkeypatch.setattr(requests, "post", no_post)
    found = checks_of(configured)
    assert found["notify.configured"].status == OK and found["notify.configured"].message == "configured: ntfy, webhook, email"
    assert found["notify.dry_run"].status == OK and "nothing was sent" in found["notify.dry_run"].message
    assert "unifi-sentinel: 1 problem(s)" in found["notify.dry_run"].message


# -- the controller ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("error, fragment", [
    (UniFiAPIError("x", kind="tls"), "TLS certificate was not accepted"),
    (UniFiAPIError("x", kind="unauthorized", status=401), "rejected the API key (401)"),
    (UniFiAPIError("x", kind="forbidden", status=403), "403"),
    (UniFiAPIError("x", kind="timeout"), "no answer within 15 s"),
    (UniFiAPIError("x", kind="connection"), "could not connect"),
    (UniFiAPIError("x", kind="bad_body"), "not JSON"),
    (UniFiAPIError("x", kind="http", status=500), "HTTP 500"),
    (UniFiAPIError("x", kind="request"), "could not be made"),
    (UniFiAPIError("x"), "could not be made"),
])
def test_an_unreachable_controller_fails_with_the_cause_explained_and_the_rest_skipped(configured, monkeypatch,
                                                                                     error, fragment):
    def broken():
        raise error

    monkeypatch.setattr(configured, "info", broken)
    found = checks_of(configured)
    assert found["controller.reachable"].status == FAIL and fragment in found["controller.reachable"].message
    assert found["controller.version"].status == SKIP and found["controller.site"].status == SKIP
    assert all(found[f"endpoint.{r.id}"].status == SKIP for r in READERS) and found["endpoint.events"].status == SKIP
    assert configured.session.posts == []                                  # nothing after the failure was tried


def test_the_certificate_explanation_depends_on_whether_a_ca_file_is_set():
    config = Config(controller_url=URL, api_key=KEY, verify_ssl=True)
    assert "self-signed" in explain(UniFiAPIError("", kind="tls"), config)[1]
    pinned = Config(controller_url=URL, api_key=KEY, verify_ssl="/x/ca.pem")
    message, fix = explain(UniFiAPIError("", kind="tls"), pinned)
    assert "CA bundle" in message and "/x/ca.pem" not in message and "CA that signed it" in fix


def test_every_failure_kind_has_a_fix_where_the_user_can_act(configured):
    config = Config(controller_url=URL, api_key=KEY)
    for kind in ("tls", "unauthorized", "forbidden", "timeout", "connection", "bad_body"):
        assert explain(UniFiAPIError("", kind=kind), config)[1], kind


def test_a_missing_site_fails_with_the_count_and_skips_the_reads(configured):
    found = checks_of(configured, site="Nope")
    assert found["controller.reachable"].status == OK
    assert found["controller.site"].status == FAIL and "'Nope' was not found; the controller has 1 site" in found["controller.site"].message
    assert "unifi-sentinel info" in found["controller.site"].fix
    assert all(found[f"endpoint.{r.id}"].status == SKIP for r in READERS) and found["endpoint.events"].status == SKIP


def test_another_error_while_finding_the_site_is_explained_too(configured, monkeypatch):
    def broken(site):
        raise UniFiAPIError("x", kind="http", status=500)

    monkeypatch.setattr(configured, "resolve_site", broken)
    found = checks_of(configured)["controller.site"]
    assert found.status == FAIL and found.message == "HTTP 500"


def test_an_optional_endpoint_that_is_unavailable_is_a_warning_with_what_is_lost(configured, capsys):
    fail_reads(configured, "/stat/alluser", "/rest/wlanconf", status=404)
    found = checks_of(configured)
    alluser = found["endpoint.stat_alluser"]
    assert alluser.status == WARN and "unavailable (HTTP 404): without it, offline clients, reservations" in alluser.message
    assert found["endpoint.rest_wlanconf"].status == WARN and found["endpoint.stat_device"].status == OK
    code, _, _ = run(capsys)
    assert code == 0                                                       # warnings do not fail the command


def test_a_required_endpoint_that_is_unavailable_fails_the_command(configured, capsys):
    fail_reads(configured, "/sites/site-1/devices", status=500)
    found = checks_of(configured)
    assert found["endpoint.integration_devices"].status == FAIL
    assert "every command needs it" in found["endpoint.integration_devices"].message
    assert found["endpoint.integration_device_detail"].status == SKIP       # no device to look at
    assert run(capsys)[0] == 3


def test_a_controller_with_no_devices_skips_the_per_device_reads(configured):
    configured.session.fx["devices"] = []
    found = checks_of(configured)
    assert found["endpoint.integration_devices"].message == "0 records"
    for name in ("integration_device_detail", "integration_device_stats"):
        assert found[f"endpoint.{name}"].status == SKIP and "no devices" in found[f"endpoint.{name}"].message


def test_the_event_log_is_a_warning_when_it_cannot_be_read(configured, monkeypatch):
    def broken(site, query):
        raise UniFiAPIError("x", kind="http", status=500)

    monkeypatch.setattr(configured, "system_log", broken)
    found = checks_of(configured)["endpoint.events"]
    assert found.status == WARN and "unavailable (HTTP 500)" in found.message and "`events`" in found.message


def test_offline_contacts_nothing_and_no_events_sends_no_post(configured, capsys):
    code, out, _ = run(capsys, "--offline")
    assert code == 0 and configured.session.calls == [] and configured.session.posts == []
    assert "skipped (--offline)" in out
    code, out, _ = run(capsys, "--no-events")
    assert code == 0 and configured.session.posts == [] and "skipped (--no-events)" in out
    assert configured.session.calls                                         # everything else was read


def test_a_failed_check_makes_the_exit_code_three_and_a_warning_does_not(configured, monkeypatch, capsys):
    monkeypatch.setenv("VERIFY_SSL", "false")
    assert run(capsys)[0] == 0
    monkeypatch.setattr(configured, "info", lambda: (_ for _ in ()).throw(UniFiAPIError("x", kind="timeout")))
    assert run(capsys)[0] == 3


# -- what must never be printed --------------------------------------------------------------------------------

def every_output(configured, monkeypatch, capsys):
    outputs = []
    for argv in ([], ["--json"], ["--offline"], ["--no-events", "--json"]):
        outputs.append(run(capsys, *argv)[1:])
    monkeypatch.setattr(configured, "info", lambda: (_ for _ in ()).throw(
        UniFiAPIError(f"Connection error for {URL}/x: {KEY} {HOST}", kind="connection")))
    outputs.append(run(capsys)[1:])
    outputs.append(run(capsys, "--json")[1:])
    return " ".join(part for pair in outputs for part in pair)


def test_no_secret_and_no_host_is_ever_printed(monkeypatch, configured, capsys):
    for name, value in NOTIFY.items():
        monkeypatch.setenv(name, value)
    text = every_output(configured, monkeypatch, capsys)
    assert "doctor" in text
    for secret in (KEY, HOST, URL, *NOTIFY.values()):
        assert secret not in text, secret
    assert "smtp.private.example" not in text


def test_the_scrubber_is_a_second_safety_net_for_any_text(monkeypatch):
    monkeypatch.setenv("API_KEY", KEY)
    monkeypatch.setenv("CONTROLLER_URL", URL)
    monkeypatch.setenv("NOTIFY_NTFY_URL", NOTIFY["NOTIFY_NTFY_URL"])
    text = f"{KEY} {URL}/proxy {HOST}:8443 {HOST} {NOTIFY['NOTIFY_NTFY_URL']}"
    cleaned = scrub(text)
    assert cleaned == "*** <controller>/proxy <controller> <controller> ***"
    assert make("config.env_file", OK, text).message == cleaned
    assert scrub("nothing to hide") == "nothing to hide"


def test_short_values_are_not_scrubbed_into_the_text(monkeypatch):
    monkeypatch.setenv("API_KEY", "ab")
    monkeypatch.setenv("CONTROLLER_URL", "x")
    assert scrub("a b c ab x") == "a b c ab x"


def test_text_from_the_controller_is_cleaned_to_one_line(configured, capsys):
    configured.session.fx["info"]["applicationVersion"] = "10.\x1b[31m0\n[FAIL] forged\u202e"
    code, out, _ = run(capsys)
    assert "\x1b" not in out and "\u202e" not in out
    assert not any(line.lstrip().startswith("[FAIL] forged") for line in out.splitlines())
    found = checks_of(configured)["controller.version"]
    assert "\n" not in found.message and "\x1b" not in found.message


def test_a_file_name_with_control_characters_cannot_break_a_line_either(configured, tmp_path):
    odd = tmp_path / "set\x1b[31mtings\n[FAIL] forged.toml"
    odd.write_text("[thresholds]\nresource_warn_pct = 80\n")
    found = checks_of(configured, config=odd)["config.settings_file"]
    assert found.status == OK and "\n" not in found.message and "\x1b" not in found.message
    assert "forged.toml" in found.message


# -- the output --------------------------------------------------------------------------------------------------

def test_the_json_document_has_the_checks_and_a_consistent_summary(configured, capsys):
    code, doc = doc_json(capsys)
    assert code == 0 and list(doc) == ["version", "tool", "checks", "summary"] and doc["version"] == 1
    assert [c["id"] for c in doc["checks"]] == list(CHECKS)
    assert all(set(c) == {"id", "section", "status", "title", "message", "fix"} for c in doc["checks"])
    assert all(c["section"] == c["id"].partition(".")[0] for c in doc["checks"])
    assert doc["summary"] == {s: sum(c["status"] == s for c in doc["checks"]) for s in doc["summary"]}
    assert sum(doc["summary"].values()) == len(CHECKS)
    assert doc["tool"]["platform"] == sys.platform


def test_the_text_groups_the_checks_in_sections_and_shows_the_fixes(configured, monkeypatch, capsys):
    monkeypatch.setenv("VERIFY_SSL", "false")
    _, out, _ = run(capsys)
    headings = [line for line in out.splitlines() if line and not line.startswith(" ")]
    assert headings[:6] == ["UniFi Sentinel doctor", "Installation", "Configuration", "Notifications", "Controller",
                            "What the controller offers"]
    assert "[WARN] TLS verification: certificate checking is off" in out and "         -> trust the controller's" in out
    assert out.rstrip().splitlines()[-1] == "Nothing is broken, but see the warnings."
    assert re.search(r"\d+ ok, \d+ warnings?, \d+ failed, \d+ skipped, \d+ for your information", out)


def test_the_summary_wording_for_one_warning_and_one_failure():
    one = [Check("config.tls", WARN, "m"), Check("config.limits", OK, "m")]
    assert "1 warning," in render(one) and summary(one) == {OK: 1, WARN: 1, FAIL: 0, SKIP: 0, INFO: 0}
    failed = [Check("config.environment", FAIL, "m")]
    assert "1 check failed: fix those first." in render(failed)
    assert render([Check("config.tls", OK, "m")]).rstrip().endswith("Everything needed works.")


def test_a_check_must_have_a_known_id_and_status():
    with pytest.raises(AssertionError):
        make("config.nothing", OK, "m")
    with pytest.raises(AssertionError):
        make("config.tls", "great", "m")


def test_the_dictionary_form_has_the_title_of_the_id():
    assert to_dict([Check("config.tls", OK, "m")])["checks"][0]["title"] == CHECKS["config.tls"]


# -- the command line and the documentation --------------------------------------------------------------------

def test_the_global_options_reach_doctor(monkeypatch, configured, capsys):
    out = cli.main(["--site", "Lab", "--timeout", "7", "--parallel", "2", "doctor", "--json", "--offline"])
    doc = json.loads(capsys.readouterr().out)
    limits = next(c for c in doc["checks"] if c["id"] == "config.limits")
    assert out == 0 and limits["message"] == "timeout 7 s, 2 requests at once, site Lab"


def test_every_check_id_is_documented_and_every_documented_id_exists():
    page = (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"`((?:install|config|notify|controller|endpoint)\.[a-z_0-9]+)`", page))
    assert documented == set(CHECKS), (sorted(documented ^ set(CHECKS)))


def test_every_reader_has_a_title_an_impact_and_a_unique_id():
    ids = [r.id for r in READERS]
    assert len(ids) == len(set(ids)) and all(r.title and r.impact for r in READERS)
    assert [r.id for r in READERS if r.required] == ["integration_devices", "integration_clients"]


def test_the_check_ids_have_the_shape_the_schema_expects():
    schema = json.loads((ROOT / "docs" / "schemas" / "doctor.v1.schema.json").read_text(encoding="utf-8"))
    pattern = schema["properties"]["checks"]["items"]["properties"]["id"]["pattern"]
    assert all(re.fullmatch(pattern, check_id) for check_id in CHECKS)
