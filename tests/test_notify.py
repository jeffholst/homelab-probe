"""Notifications: planning, wording, sending (with a fake transport), the state file, config, the CLI."""

import json
import re
import stat
from pathlib import Path

import pytest
import requests

from homelab_probe import cli
from homelab_probe import notify as notify_module
from homelab_probe.config import Config, ConfigError, load_config, validate_notify_token, validate_notify_url
from homelab_probe.diagnose import CODES, CRITICAL, INFO, WARNING, Finding
from homelab_probe.notify import (
    MAX_LINES,
    Destination,
    Event,
    baseline,
    destinations_from_config,
    empty_state,
    identity,
    load_state,
    plan,
    render_payload,
    render_text,
    save_state,
    send,
)
from homelab_probe.settings import DiagnoseSettings, load_settings

HOUR = 3600
NOW = 1_800_000_000.0
SECRET_TOPIC = "SECRETTOPIC-9f3a7c21"
NTFY = f"https://ntfy.example/{SECRET_TOPIC}"
HOOK = "https://hooks.example/in/SECRETHOOK-77aa?token=SECRETQUERY"
TOKEN = "SECRETTOKEN-1234"


def finding(severity, subject, message="m", code="device.offline", mac=None):
    return Finding(severity, subject, message, mac, code=code)


# -- planning ----------------------------------------------------------------------------------

def test_a_finding_not_in_the_state_is_new_and_gets_remembered():
    events, state = plan([finding(WARNING, "Garage AP", "device is offline")], empty_state(), NOW)
    assert events == [Event("new", WARNING, "device.offline", "Garage AP", "device is offline")]
    assert state["active"][identity("device.offline", "Garage AP")] == {
        "severity": WARNING, "first_notified": NOW, "last_notified": NOW}


def test_an_unchanged_situation_sends_nothing():
    found = [finding(WARNING, "Garage AP")]
    _, state = plan(found, empty_state(), NOW)
    events, again = plan(found, state, NOW + 5 * HOUR)
    assert events == [] and again["active"] == state["active"]          # last_notified is not touched


def test_a_changed_message_with_the_same_identity_is_quiet():
    _, state = plan([finding(WARNING, "Office Switch port 2", "4 rx/tx errors", code="port.errors")], empty_state(), NOW)
    events, _ = plan([finding(WARNING, "Office Switch port 2", "9 rx/tx errors", code="port.errors")], state, NOW + HOUR)
    assert events == []


def test_the_same_check_on_another_subject_is_a_separate_identity():
    _, state = plan([finding(WARNING, "A")], empty_state(), NOW)
    events, _ = plan([finding(WARNING, "A"), finding(WARNING, "B")], state, NOW + HOUR)
    assert [(e.kind, e.subject) for e in events] == [("new", "B")]


def test_two_findings_with_one_identity_are_one_event_at_the_worst_severity():
    events, state = plan([finding(WARNING, "A"), finding(CRITICAL, "A")], empty_state(), NOW)
    assert [(e.kind, e.severity) for e in events] == [("new", CRITICAL)] and len(state["active"]) == 1


def test_getting_worse_is_notified_and_getting_better_is_quiet():
    _, state = plan([finding(WARNING, "GW")], empty_state(), NOW)
    events, state = plan([finding(CRITICAL, "GW")], state, NOW + HOUR)
    assert [(e.kind, e.severity) for e in events] == [("worsened", CRITICAL)]
    assert state["active"][identity("device.offline", "GW")]["last_notified"] == NOW + HOUR
    events, state = plan([finding(WARNING, "GW")], state, NOW + 2 * HOUR)
    assert events == [] and state["active"][identity("device.offline", "GW")]["severity"] == WARNING
    events, _ = plan([finding(CRITICAL, "GW")], state, NOW + 3 * HOUR)       # worse again than the last known level
    assert [e.kind for e in events] == ["worsened"]


def test_a_critical_finding_is_repeated_after_the_interval_but_not_before():
    _, state = plan([finding(CRITICAL, "GW")], empty_state(), NOW)
    assert plan([finding(CRITICAL, "GW")], state, NOW + 23 * HOUR)[0] == []
    events, state = plan([finding(CRITICAL, "GW")], state, NOW + 24 * HOUR)
    assert [e.kind for e in events] == ["reminder"]
    assert plan([finding(CRITICAL, "GW")], state, NOW + 30 * HOUR)[0] == []             # the clock restarted
    assert [e.kind for e in plan([finding(CRITICAL, "GW")], state, NOW + 48 * HOUR)[0]] == ["reminder"]


def test_reminders_can_be_turned_off_or_changed_and_only_apply_to_critical():
    _, state = plan([finding(CRITICAL, "GW"), finding(WARNING, "AP")], empty_state(), NOW)
    assert plan([finding(CRITICAL, "GW")], state, NOW + 99 * HOUR, repeat_hours=0)[0][0].kind == "recovered"
    events, _ = plan([finding(CRITICAL, "GW"), finding(WARNING, "AP")], state, NOW + 99 * HOUR, repeat_hours=0)
    assert events == []
    events, _ = plan([finding(CRITICAL, "GW"), finding(WARNING, "AP")], state, NOW + 7 * HOUR, repeat_hours=6)
    assert [(e.kind, e.subject) for e in events] == [("reminder", "GW")]            # the warning is never repeated


def test_a_finding_that_goes_away_is_recovered_once():
    _, state = plan([finding(CRITICAL, "GW", code="device.offline")], empty_state(), NOW)
    events, state = plan([], state, NOW + HOUR)
    assert events == [Event("recovered", CRITICAL, "device.offline", "GW", "")] and state["active"] == {}
    assert plan([], state, NOW + 2 * HOUR)[0] == []


def test_minimum_severity_filters_and_a_drop_below_it_counts_as_recovered():
    found = [finding(INFO, "i", code="port.slow_link"), finding(WARNING, "w"), finding(CRITICAL, "c")]
    assert [e.subject for e in plan(found, empty_state(), NOW, WARNING)[0]] == ["c", "w"]
    assert [e.subject for e in plan(found, empty_state(), NOW, CRITICAL)[0]] == ["c"]
    assert sorted(e.subject for e in plan(found, empty_state(), NOW, INFO)[0]) == ["c", "i", "w"]
    _, state = plan([finding(WARNING, "w")], empty_state(), NOW, WARNING)
    events, _ = plan([finding(INFO, "w")], state, NOW + HOUR, WARNING)               # fell below the floor
    assert [e.kind for e in events] == ["recovered"]


def test_events_are_ordered_new_first_then_worst_severity():
    _, state = plan([finding(WARNING, "gone", code="port.errors")], empty_state(), NOW)
    events, _ = plan([finding(WARNING, "w"), finding(CRITICAL, "c")], state, NOW + HOUR)
    assert [(e.kind, e.subject) for e in events] == [("new", "c"), ("new", "w"), ("recovered", "gone")]


def test_damaged_state_entries_are_ignored():
    state = {"version": 1, "active": {"device.offline|A": "junk", "device.offline|B": {"severity": "purple"},
                                      "device.offline|C": {"severity": WARNING, "last_notified": NOW},
                                      "device.offline|D": {"severity": WARNING, "last_notified": "bad"}}}
    events, new = plan([finding(WARNING, subject) for subject in "ABCD"], state, NOW + HOUR)
    assert sorted(e.subject for e in events) == ["A", "B", "D"] and set(new["active"]) == {
        "device.offline|A", "device.offline|B", "device.offline|C", "device.offline|D"}


def test_a_baseline_makes_everything_current_already_reported():
    found = [finding(WARNING, "A"), finding(CRITICAL, "B"), finding(INFO, "C", code="port.slow_link")]
    state = baseline(found, NOW)
    assert len(state["active"]) == 2 and plan(found, state, NOW + HOUR)[0] == []


# -- wording ---------------------------------------------------------------------------------------

def events_for(*findings):
    return plan(list(findings), empty_state(), NOW)[0]


def test_the_text_names_the_subjects_and_counts_in_an_ascii_title():
    title, body = render_text(events_for(finding(CRITICAL, "Gateway", "device is offline (gateway)"),
                                         finding(WARNING, "Office AP", "weak signal", code="wifi.weak_signal")))
    assert title == "hlp: 2 problem(s)" and title.isascii()
    assert body.splitlines() == ["[CRITICAL] NEW  Gateway: device is offline (gateway)",
                                 "[WARNING] NEW  Office AP: weak signal"]


def test_recoveries_and_reminders_are_worded():
    _, state = plan([finding(CRITICAL, "GW")], empty_state(), NOW)
    events, _ = plan([finding(CRITICAL, "GW", "still down"), ], state, NOW + 25 * HOUR)
    assert render_text(events)[1] == "[CRITICAL] STILL  GW: still down (reminder: still unresolved)"
    events, _ = plan([], state, NOW + HOUR)
    title, body = render_text(events)
    assert title == "hlp: 1 recovered" and body == "[OK] RECOVERED  GW (device.offline)"
    mixed = events_for(finding(WARNING, "new-one")) + events
    assert render_text(mixed)[0] == "hlp: 1 problem(s), 1 recovered"


def test_names_are_cleaned_and_a_long_list_is_cut():
    hostile = "evil\x1b[31m\n[CRITICAL] forged\u202e"
    body = render_text(events_for(finding(WARNING, "x" + hostile, "m" + hostile)))[1]
    assert "\x1b" not in body and "\u202e" not in body and len(body.splitlines()) == 1
    many = events_for(*[finding(WARNING, f"d{i:02}") for i in range(MAX_LINES + 7)])
    lines = render_text(many)[1].splitlines()
    assert len(lines) == MAX_LINES + 1 and lines[-1] == "... and 7 more"


def test_redaction_sends_only_the_generic_description_and_counts():
    found = [finding(WARNING, "kitchen-echo (10.0.0.77) BB:00:00:00:00:09", "weak signal", code="wifi.weak_signal"),
             finding(WARNING, "den-tv (10.0.0.78)", "weak signal", code="wifi.weak_signal"),
             finding(CRITICAL, "Gateway", "offline", code="device.offline")]
    title, body = render_text(events_for(*found), redact=True)
    assert title == "hlp: 3 problem(s)"
    assert body.splitlines() == ["[CRITICAL] NEW  " + CODES["device.offline"],
                                 "[WARNING] NEW  " + CODES["wifi.weak_signal"] + " (x2)"]
    for secret in ("kitchen", "echo", "10.0.0", "BB:00", "Gateway", "den-tv"):
        assert secret not in title + body


def test_redacted_recoveries_carry_no_name():
    _, state = plan([finding(WARNING, "kitchen-echo", code="wifi.weak_signal")], empty_state(), NOW)
    events, _ = plan([], state, NOW + HOUR)
    assert render_text(events, redact=True)[1] == "[WARNING] RECOVERED  " + CODES["wifi.weak_signal"]


def test_the_webhook_payload_shape_with_and_without_redaction():
    events = events_for(finding(CRITICAL, "Gateway", "device is offline", code="device.offline"))
    payload = render_payload(events)
    assert payload["source"] == "homelab-probe" and payload["version"] == 1 and payload["redacted"] is False
    assert payload["events"] == [{"event": "new", "severity": "critical", "code": "device.offline",
                                  "description": CODES["device.offline"], "subject": "Gateway",
                                  "message": "device is offline"}]
    assert payload["text"] == payload["title"] + "\n" + render_text(events)[1]
    redacted = render_payload(events, redact=True)
    assert redacted["redacted"] is True and "Gateway" not in json.dumps(redacted)
    assert set(redacted["events"][0]) == {"event", "severity", "code", "description"}


def test_ntfy_priority_and_tag_follow_the_worst_problem():
    prio = notify_module._priority
    assert prio(events_for(finding(CRITICAL, "a"), finding(WARNING, "b"))) == ("5", "rotating_light")
    assert prio(events_for(finding(WARNING, "b"))) == ("4", "warning")
    assert prio(events_for(finding(INFO, "i", code="port.slow_link")))[0] == "3"
    _, state = plan([finding(CRITICAL, "a")], empty_state(), NOW)
    assert prio(plan([], state, NOW + HOUR)[0]) == ("3", "white_check_mark")


# -- sending ------------------------------------------------------------------------------------------

class FakePost:
    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes) or [200], []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return type("Response", (), {"status_code": outcome})()


def destinations():
    return [Destination("ntfy", NTFY, TOKEN), Destination("webhook", HOOK, "")]


def test_ntfy_gets_the_text_with_priority_tags_and_a_bearer_token():
    post = FakePost(200)
    events = events_for(finding(CRITICAL, "Gateway — café", "offline"))
    results = send([Destination("ntfy", NTFY, TOKEN)], events, False, 12.0, post=post)
    assert results == [("ntfy", True, "HTTP 200")]
    (url, kwargs), = post.calls
    assert url == NTFY and kwargs["timeout"] == 12.0 and kwargs["allow_redirects"] is False and kwargs["verify"] is True
    assert kwargs["data"].decode("utf-8").startswith("[CRITICAL] NEW  Gateway — café: offline")
    assert kwargs["headers"] == {"Authorization": f"Bearer {TOKEN}", "Title": "hlp: 1 problem(s)",
                                 "Priority": "5", "Tags": "rotating_light"}


def test_the_webhook_gets_json_and_only_sends_a_token_when_there_is_one():
    post = FakePost(204)
    events = events_for(finding(WARNING, "Garage AP"))
    assert send([Destination("webhook", HOOK, "")], events, False, 5, post=post) == [("webhook", True, "HTTP 204")]
    (url, kwargs), = post.calls
    assert url == HOOK and kwargs["json"]["events"][0]["subject"] == "Garage AP" and "data" not in kwargs
    assert "Authorization" not in kwargs["headers"]
    post = FakePost(200)
    send([Destination("webhook", HOOK, TOKEN)], events, True, 5, post=post)
    assert post.calls[0][1]["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert "Garage AP" not in json.dumps(post.calls[0][1]["json"])


def test_every_destination_is_tried_and_reported():
    post = FakePost(200, 500)
    results = send(destinations(), events_for(finding(WARNING, "A")), False, 5, post=post)
    assert results == [("ntfy", True, "HTTP 200"), ("webhook", False, "HTTP 500")]


@pytest.mark.parametrize("status, ok", [(200, True), (201, True), (204, True), (301, False), (302, False),
                                        (400, False), (403, False), (404, False), (429, False), (500, False)])
def test_only_a_2xx_answer_is_delivery(status, ok):
    (kind, delivered, reason), = send([Destination("ntfy", NTFY)], events_for(finding(WARNING, "A")), False, 5,
                                      post=FakePost(status))
    assert delivered is ok and reason == f"HTTP {status}"


@pytest.mark.parametrize("failure, reason", [
    (requests.exceptions.ConnectTimeout(f"HTTPSConnectionPool: {NTFY} {TOKEN}"), "timed out"),
    (requests.exceptions.ReadTimeout(f"slow {NTFY}"), "timed out"),
    (requests.exceptions.SSLError(f"bad cert for {NTFY}"), "TLS certificate verification failed"),
    (requests.exceptions.ConnectionError(f"Max retries exceeded with url: /{SECRET_TOPIC} {TOKEN}"), "connection error"),
    (requests.exceptions.InvalidURL(f"{NTFY} {HOOK}"), "request error"),
])
def test_failures_give_a_fixed_reason_that_never_contains_the_secrets(failure, reason):
    lines = []
    results = send(destinations(), events_for(finding(WARNING, "A")), False, 5, post=FakePost(failure),
                   trace=lines.append)
    assert [(k, ok, why) for k, ok, why in results] == [("ntfy", False, reason), ("webhook", False, reason)]
    everything = repr(results) + "".join(lines)
    for secret in (SECRET_TOPIC, TOKEN, "SECRETHOOK", "SECRETQUERY", "hooks.example", "ntfy.example"):
        assert secret not in everything


def test_trace_lines_name_the_destination_and_the_outcome_only():
    lines = []
    send(destinations(), events_for(finding(WARNING, "A")), False, 5, post=FakePost(200, 503), trace=lines.append)
    assert [re.sub(r"\d+ ms", "N ms", line) for line in lines] == [
        "notify ntfy -> HTTP 200 (N ms)", "notify webhook -> HTTP 503 (N ms)"]


def test_the_default_transport_is_requests_post(monkeypatch):
    seen = []
    monkeypatch.setattr(notify_module.requests, "post",
                        lambda url, **kw: seen.append((url, kw)) or type("R", (), {"status_code": 200})())
    assert send([Destination("ntfy", NTFY)], events_for(finding(WARNING, "A")), False, 5)[0][1] is True
    assert seen[0][0] == NTFY


def test_destinations_come_from_the_config_and_hide_their_secrets_in_repr():
    config = Config("https://c.example", "k", notify_ntfy_url=NTFY, notify_ntfy_token=TOKEN, notify_webhook_url=HOOK)
    found = destinations_from_config(config)
    assert [(d.kind, d.url, d.token) for d in found] == [("ntfy", NTFY, TOKEN), ("webhook", HOOK, "")]
    for secret in (SECRET_TOPIC, TOKEN, "SECRETHOOK"):
        assert secret not in repr(found) and secret not in repr(config)
    assert destinations_from_config(Config("https://c.example", "k")) == []


# -- the state file ----------------------------------------------------------------------------------------

@pytest.mark.skipif(not hasattr(__import__("os"), "fchmod"), reason="POSIX modes")
def test_the_state_is_saved_owner_only_and_replaced_atomically(tmp_path):
    path = tmp_path / "deep" / "state.json"
    _, state = plan([finding(WARNING, "A")], empty_state(), NOW)
    save_state(path, state)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and path.parent.is_dir()
    assert load_state(path) == (state, "")
    save_state(path, empty_state())
    assert load_state(path)[0]["active"] == {} and sorted(p.name for p in path.parent.iterdir()) == ["state.json"]


def test_missing_and_damaged_state_files_start_empty(tmp_path):
    assert load_state(tmp_path / "none.json") == (empty_state(), "")
    for name, text in (("junk.json", "not json"), ("list.json", "[1]"), ("old.json", '{"version": 99, "active": {}}'),
                       ("shape.json", '{"version": 1, "active": []}')):
        (tmp_path / name).write_text(text)
        state, warning = load_state(tmp_path / name)
        assert state == empty_state() and "starting" in warning, name


def test_an_unreadable_state_is_reported_not_fatal(tmp_path):
    (tmp_path / "dir.json").mkdir()
    state, warning = load_state(tmp_path / "dir.json")
    assert state == empty_state() and "cannot read the notification state" in warning


# -- configuration -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("url", [NTFY, HOOK, "https://ntfy.example:8443/topic?auth=x", "HTTPS://Host.example/a/b"])
def test_good_destination_urls_are_kept_as_given(url):
    assert validate_notify_url("NOTIFY_NTFY_URL", f"  {url}  ") == url


@pytest.mark.parametrize("text", [None, "", "   "])
def test_an_unset_destination_is_empty(text):
    assert validate_notify_url("NOTIFY_NTFY_URL", text) == ""


@pytest.mark.parametrize("url, message", [
    (f"http://ntfy.example/{SECRET_TOPIC}", "clear text"),
    (f"ntfy.example/{SECRET_TOPIC}", "scheme and a host"),
    (f"https:///{SECRET_TOPIC}", "scheme and a host"),
    (f"ftp://ntfy.example/{SECRET_TOPIC}", "scheme and a host"),
    (f"https://user:{TOKEN}@ntfy.example/{SECRET_TOPIC}", "user name or password"),
    (f"https://ntfy.example/{SECRET_TOPIC}#frag", "fragment"),
    (f"https://ntfy.example/{SECRET_TOPIC} x", "spaces, backslashes or control"),
    (f"https://ntfy.example\\{SECRET_TOPIC}", "spaces, backslashes or control"),
    (f"https://ntfy.example:99999/{SECRET_TOPIC}", "not a valid URL"),
])
def test_bad_destination_urls_are_refused_without_repeating_the_secret(url, message):
    with pytest.raises(ConfigError, match=message) as caught:
        validate_notify_url("NOTIFY_NTFY_URL", url)
    for secret in (SECRET_TOPIC, TOKEN):
        assert secret not in str(caught.value)


def test_plain_http_needs_the_lab_opt_in():
    assert validate_notify_url("NOTIFY_WEBHOOK_URL", "http://hook.lan/x", allow_http=True) == "http://hook.lan/x"


def test_tokens_are_trimmed_and_checked():
    assert validate_notify_token("T", "  abc  ") == "abc" and validate_notify_token("T", None) == ""
    with pytest.raises(ConfigError, match="spaces or control"):
        validate_notify_token("NOTIFY_NTFY_TOKEN", "a b")


def test_load_config_reads_the_destinations(monkeypatch):
    monkeypatch.setenv("UNIFI_URL", "https://controller.example")
    monkeypatch.setenv("UNIFI_API_KEY", "key")
    assert load_config().notify_ntfy_url == "" and load_config().notify_webhook_url == ""
    monkeypatch.setenv("NOTIFY_NTFY_URL", NTFY)
    monkeypatch.setenv("NOTIFY_NTFY_TOKEN", TOKEN)
    monkeypatch.setenv("NOTIFY_WEBHOOK_URL", HOOK)
    cfg = load_config()
    assert (cfg.notify_ntfy_url, cfg.notify_ntfy_token, cfg.notify_webhook_url) == (NTFY, TOKEN, HOOK)
    monkeypatch.setenv("NOTIFY_NTFY_URL", "http://ntfy.example/t")
    with pytest.raises(ConfigError, match="NOTIFY_NTFY_URL uses http"):
        load_config()
    monkeypatch.setenv("ALLOW_INSECURE_HTTP", "true")
    assert load_config().notify_ntfy_url == "http://ntfy.example/t"


def test_the_repeat_interval_is_a_validated_setting(tmp_path):
    assert DiagnoseSettings().notify_repeat_hours == 24
    path = tmp_path / "s.toml"
    path.write_text("[thresholds]\nnotify_repeat_hours = 6\n")
    assert load_settings(path).notify_repeat_hours == 6
    path.write_text("[thresholds]\nnotify_repeat_hours = 0\n")
    assert load_settings(path).notify_repeat_hours == 0
    for bad in ("-1", "'daily'", "true"):
        path.write_text(f"[thresholds]\nnotify_repeat_hours = {bad}\n")
        with pytest.raises(ConfigError, match="notify_repeat_hours"):
            load_settings(path)


# -- the guard: the one outbound channel ---------------------------------------------------------------------

def test_notify_py_is_the_only_outbound_channel_and_never_touches_the_controller_client():
    source = Path(notify_module.__file__).read_text(encoding="utf-8")
    assert len(re.findall(r"requests\.post", source)) == 1
    assert "UniFiClient" not in source and "controller_url" not in source and "api_key" not in source
    client_source = (Path(notify_module.__file__).parent / "client.py").read_text(encoding="utf-8")
    assert "notify" not in client_source


# -- the command line ------------------------------------------------------------------------------------------

def make_env(monkeypatch, ntfy=NTFY, webhook="", token=""):
    monkeypatch.setenv("UNIFI_URL", "https://controller.example")
    monkeypatch.setenv("UNIFI_API_KEY", "sekret-api-key-0123456789")
    for name, value in (("NOTIFY_NTFY_URL", ntfy), ("NOTIFY_WEBHOOK_URL", webhook), ("NOTIFY_NTFY_TOKEN", token)):
        if value:
            monkeypatch.setenv(name, value)


def run(fake_client, monkeypatch, argv):
    fake_client.session.fx["legacy"]["device"][0]["overheating"] = False
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, c: fake_client))
    return cli.main(argv)


@pytest.fixture
def post(monkeypatch):
    fake = FakePost(200)
    monkeypatch.setattr(notify_module.requests, "post", fake)
    return fake


def diagnose(*extra, state=None):
    return ["diagnose", "--no-events", "--json", "--notify", "--notify-state", str(state), *extra]


def test_notify_without_a_destination_is_a_config_error_before_any_request(fake_client, monkeypatch, capsys, post,
                                                                           tmp_path):
    make_env(monkeypatch, ntfy="")
    assert run(fake_client, monkeypatch, diagnose(state=tmp_path / "s.json")) == cli.EXIT_ERROR
    error = capsys.readouterr().err
    assert "--notify needs a destination" in error
    assert "https://github.com/jeffholst/homelab-probe/blob/main/docs/notifications.md" in error
    assert fake_client.session.calls == [] and post.calls == [] and not (tmp_path / "s.json").exists()


@pytest.mark.parametrize("extra", [["--notify-min", "critical"], ["--notify-redact"], ["--notify-dry-run"],
                                   ["--notify-baseline"], ["--notify-state", "x.json"]])
def test_the_notify_options_need_notify(fake_client, monkeypatch, extra):
    make_env(monkeypatch)
    with pytest.raises(SystemExit) as caught:
        run(fake_client, monkeypatch, ["diagnose", "--no-events", *extra])
    assert caught.value.code == cli.EXIT_USAGE


def test_dry_run_and_baseline_cannot_be_combined(fake_client, monkeypatch):
    make_env(monkeypatch)
    with pytest.raises(SystemExit) as caught:
        run(fake_client, monkeypatch, ["diagnose", "--notify", "--notify-dry-run", "--notify-baseline"])
    assert caught.value.code == cli.EXIT_USAGE


def test_nothing_is_sent_without_the_flag(fake_client, monkeypatch, capsys, post):
    make_env(monkeypatch)
    assert run(fake_client, monkeypatch, ["diagnose", "--no-events"]) == 1
    assert post.calls == []
    assert "Notification" not in capsys.readouterr().err


def test_first_run_sends_one_message_and_the_second_sends_none(fake_client, monkeypatch, capsys, post, tmp_path):
    make_env(monkeypatch, token=TOKEN)
    state = tmp_path / "state.json"
    assert run(fake_client, monkeypatch, diagnose(state=state)) == 1               # findings exit code, unchanged
    captured = capsys.readouterr()
    assert json.loads(captured.out)["summary"]["warning"] >= 1                     # stdout is still just the JSON
    assert "Notification to ntfy: sent" in captured.err
    (url, kwargs), = post.calls
    body = kwargs["data"].decode("utf-8")
    assert url == NTFY and "[WARNING] NEW  Garage AP: device is offline" in body
    assert stat.S_IMODE(state.stat().st_mode) == 0o600 and json.loads(state.read_text())["active"]
    assert run(fake_client, monkeypatch, diagnose(state=state)) == 1
    assert "nothing new, worse or fixed" in capsys.readouterr().err and len(post.calls) == 1


def test_a_fixed_problem_is_announced_once(fake_client, monkeypatch, capsys, post, tmp_path):
    make_env(monkeypatch)
    state = tmp_path / "state.json"
    run(fake_client, monkeypatch, diagnose(state=state))
    for device in fake_client.session.fx["devices"]:
        device["state"] = "ONLINE"
    run(fake_client, monkeypatch, diagnose(state=state))
    capsys.readouterr()
    recovered = [c for c in post.calls[1:] if "RECOVERED" in c[1]["data"].decode("utf-8")]
    assert len(recovered) == 1 and "[OK] RECOVERED  Garage AP (device.offline)" in recovered[0][1]["data"].decode("utf-8")


def test_a_critical_problem_is_repeated_after_a_day(fake_client, monkeypatch, capsys, post, tmp_path):
    make_env(monkeypatch)
    for device in fake_client.session.fx["devices"]:
        if device["name"] == "Gateway":
            device["state"] = "OFFLINE"
    state = tmp_path / "state.json"
    assert run(fake_client, monkeypatch, diagnose(state=state)) == 2
    assert len(post.calls) == 1
    saved = json.loads(state.read_text())
    for entry in saved["active"].values():
        entry["last_notified"] -= 25 * HOUR
    state.write_text(json.dumps(saved))
    run(fake_client, monkeypatch, diagnose(state=state))
    body = post.calls[-1][1]["data"].decode("utf-8")
    assert len(post.calls) == 2 and "[CRITICAL] STILL  Gateway" in body and "reminder" in body
    assert "Garage AP" not in body                                   # the unchanged warning is not repeated


def test_redaction_reaches_the_wire(fake_client, monkeypatch, capsys, post, tmp_path):
    make_env(monkeypatch, webhook=HOOK)
    run(fake_client, monkeypatch, diagnose("--notify-redact", state=tmp_path / "s.json"))
    sent = json.dumps([c[1].get("json") or c[1]["data"].decode("utf-8") for c in post.calls])
    assert post.calls and "Garage AP" not in sent and "Office Switch" not in sent and "10.0.0" not in sent
    assert "AA:00:00:00" not in sent


def test_dry_run_prints_what_would_be_sent_and_changes_nothing(fake_client, monkeypatch, capsys, post, tmp_path):
    make_env(monkeypatch, ntfy="")
    state = tmp_path / "state.json"
    assert run(fake_client, monkeypatch, diagnose("--notify-dry-run", state=state)) == 1
    err = capsys.readouterr().err
    assert "Notification dry run (nothing sent, state unchanged): hlp:" in err and "Garage AP" in err
    assert post.calls == [] and not state.exists()


def test_baseline_records_the_current_findings_and_sends_nothing(fake_client, monkeypatch, capsys, post, tmp_path):
    make_env(monkeypatch, ntfy="")                                   # no destination is needed to record a baseline
    state = tmp_path / "state.json"
    assert run(fake_client, monkeypatch, diagnose("--notify-baseline", state=state)) == 1
    assert "Notification baseline saved:" in capsys.readouterr().err and post.calls == []
    make_env(monkeypatch)
    run(fake_client, monkeypatch, diagnose(state=state))
    assert "nothing new, worse or fixed" in capsys.readouterr().err and post.calls == []


def test_minimum_severity_from_the_command_line(fake_client, monkeypatch, capsys, post, tmp_path):
    make_env(monkeypatch)
    run(fake_client, monkeypatch, diagnose("--notify-min", "critical", state=tmp_path / "s.json"))
    assert "nothing new" in capsys.readouterr().err and post.calls == []


def test_a_failed_delivery_keeps_the_state_and_is_retried_next_run(fake_client, monkeypatch, capsys, tmp_path):
    make_env(monkeypatch, token=TOKEN)
    state = tmp_path / "state.json"
    failing = FakePost(requests.exceptions.ConnectionError(f"refused {NTFY} {TOKEN}"))
    monkeypatch.setattr(notify_module.requests, "post", failing)
    assert run(fake_client, monkeypatch, diagnose(state=state)) == 1                # findings still decide the code
    err = capsys.readouterr().err
    assert "Notification to ntfy: FAILED (connection error)" in err
    assert SECRET_TOPIC not in err and TOKEN not in err and not state.exists()
    working = FakePost(200)
    monkeypatch.setattr(notify_module.requests, "post", working)
    run(fake_client, monkeypatch, diagnose(state=state))
    assert len(working.calls) == 1 and state.exists()


def test_a_failed_delivery_is_exit_3_only_when_nothing_else_says_so(fake_client, monkeypatch, capsys, tmp_path):
    make_env(monkeypatch)
    monkeypatch.setattr(notify_module.requests, "post", FakePost(500))
    argv = diagnose("--fail-on", "critical", state=tmp_path / "s.json")        # only warnings: findings give 0
    assert run(fake_client, monkeypatch, argv) == cli.EXIT_ERROR
    assert "FAILED (HTTP 500)" in capsys.readouterr().err
    for device in fake_client.session.fx["devices"]:
        if device["name"] == "Gateway":
            device["state"] = "OFFLINE"
    assert run(fake_client, monkeypatch, diagnose(state=tmp_path / "s2.json")) == 2   # a critical finding keeps its code


def test_one_working_destination_is_enough(fake_client, monkeypatch, capsys, tmp_path):
    make_env(monkeypatch, webhook=HOOK)
    monkeypatch.setattr(notify_module.requests, "post", FakePost(200, 503))
    state = tmp_path / "state.json"
    assert run(fake_client, monkeypatch, diagnose("--fail-on", "critical", state=state)) == 0
    err = capsys.readouterr().err
    assert "Notification to ntfy: sent" in err and "Notification to webhook: FAILED (HTTP 503)" in err
    assert state.exists()


def test_nothing_secret_or_controller_related_is_ever_sent_or_printed(fake_client, monkeypatch, capsys, post, tmp_path):
    make_env(monkeypatch, webhook=HOOK, token=TOKEN)
    run(fake_client, monkeypatch, ["--verbose", *diagnose("--notify-redact", state=tmp_path / "s.json")])
    captured = capsys.readouterr()
    shown = captured.out + captured.err
    sent = json.dumps([(c[1].get("json"), c[1]["data"].decode("utf-8") if "data" in c[1] else None) for c in post.calls])
    for secret in ("sekret-api-key", SECRET_TOPIC, TOKEN, "SECRETHOOK", "SECRETQUERY"):
        assert secret not in shown and secret not in sent, secret
    assert "controller.example" not in sent and "site-1" not in sent
    assert re.search(r"\[verbose\] notify ntfy -> HTTP 200 \(\d+ ms\)", captured.err)


def test_a_state_that_cannot_be_saved_after_sending_is_reported(fake_client, monkeypatch, capsys, post, tmp_path):
    make_env(monkeypatch)

    def refuse(path, state):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(notify_module, "save_state", refuse)
    assert run(fake_client, monkeypatch, diagnose(state=tmp_path / "state.json")) == cli.EXIT_ERROR
    assert "the notification was sent but its state could not be saved" in capsys.readouterr().err


def test_a_state_that_cannot_be_locked_stops_the_run_before_anything_is_sent(fake_client, monkeypatch, capsys, post,
                                                                           tmp_path):
    make_env(monkeypatch)
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory")
    assert run(fake_client, monkeypatch, diagnose(state=blocker / "state.json")) == cli.EXIT_ERROR
    assert "cannot be locked" in capsys.readouterr().err and post.calls == []
