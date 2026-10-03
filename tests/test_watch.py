"""`diagnose --watch SECONDS`: repeat the checks and print only what changed."""

import re

import pytest

from unifi_sentinel import cli, commands
from unifi_sentinel.diagnose import CRITICAL, INFO, WARNING, Finding
from unifi_sentinel.notify import plan
from unifi_sentinel.watch import MAX_SECONDS, MIN_SECONDS, changes, start

STAMP = r"\d\d:\d\d:\d\d"


class Script:
    """Stands in for the wait between passes: each call runs the next scripted change of the fake controller and
    returns (so the next pass reads the changed network); when the script is used up it raises Ctrl-C."""

    def __init__(self, *steps):
        self.steps, self.waits = list(steps), []

    def __call__(self, seconds):
        self.waits.append(seconds)
        if not self.steps:
            raise KeyboardInterrupt
        self.steps.pop(0)()


def run(fake_client, monkeypatch, script, *argv, interval="30"):
    monkeypatch.setenv("CONTROLLER_URL", "https://controller.example")
    monkeypatch.setenv("API_KEY", "key")
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, c: fake_client))
    monkeypatch.setattr(commands, "WATCH_SLEEP", script)
    return cli.main(["diagnose", "--no-events", "--no-emoji", "--watch", interval, *argv])


def plain(fake_client, monkeypatch, capsys, *extra):
    monkeypatch.setenv("CONTROLLER_URL", "https://controller.example")
    monkeypatch.setenv("API_KEY", "key")
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, c: fake_client))
    code = cli.main(["diagnose", "--no-events", "--no-emoji", *extra])
    return code, capsys.readouterr().out


def set_state(fake_client, name, state):
    def step():
        next(d for d in fake_client.session.fx["devices"] if d["name"] == name)["state"] = state
    return step


def all_online(fake_client):
    def step():
        for d in fake_client.session.fx["devices"]:
            d["state"] = "ONLINE"
    return step


def cpu(fake_client, value):
    def step():
        fake_client.session.fx["device_stats"]["gw1"]["cpuUtilizationPct"] = value
    return step


def lines(captured):
    return [line for line in captured.out.splitlines() if re.match(STAMP, line)]


# -- the first pass and the quiet ones ------------------------------------------------------------------------------

def test_the_first_pass_prints_everything_exactly_as_diagnose_does(fake_client, monkeypatch, capsys):
    from conftest import FakeSession

    from unifi_sentinel.client import UniFiClient

    reference = UniFiClient("https://controller", "key")
    reference.session = FakeSession()
    code, expected = plain(reference, monkeypatch, capsys)
    assert run(fake_client, monkeypatch, Script()) == code
    captured = capsys.readouterr()
    assert captured.out == expected and "Warning:" not in captured.out
    assert "Watching every 30 s; only changes are printed (Ctrl-C to stop)." in captured.err
    assert captured.err.rstrip().endswith("Stopped.")


def test_when_nothing_changes_nothing_more_is_printed_and_each_pass_reads_again(fake_client, monkeypatch, capsys):
    script = Script(lambda: None, lambda: None)
    run(fake_client, monkeypatch, script)
    captured = capsys.readouterr()
    assert lines(captured) == [] and script.waits == [30, 30, 30]
    devices = [p for p in fake_client.session.calls if p.endswith("/sites/site-1/devices")]
    assert len(devices) == 3                                    # the first pass and two more, then Ctrl-C


def test_the_interval_is_the_one_given(fake_client, monkeypatch, capsys):
    script = Script(lambda: None)
    run(fake_client, monkeypatch, script, interval="600")
    assert script.waits == [600, 600] and "Watching every 600 s" in capsys.readouterr().err


# -- what is printed when something changes -------------------------------------------------------------------------

def test_a_fixed_problem_is_reported_once_with_the_time(fake_client, monkeypatch, capsys):
    run(fake_client, monkeypatch, Script(all_online(fake_client), lambda: None))
    got = lines(capsys.readouterr())
    assert len(got) == 1 and re.fullmatch(rf"{STAMP}  \[OK\] RECOVERED  Garage AP \(device\.offline\)", got[0])


def test_a_new_problem_is_reported(fake_client, monkeypatch, capsys):
    run(fake_client, monkeypatch, Script(set_state(fake_client, "Office AP", "OFFLINE")))
    got = lines(capsys.readouterr())
    assert len(got) == 1 and re.fullmatch(rf"{STAMP}  \[(WARNING|CRITICAL)\] NEW  Office AP: .*offline.*", got[0])


def untimed(captured):
    return [re.sub(rf"^{STAMP}  ", "", line) for line in lines(captured)]


def test_a_problem_that_gets_worse_is_reported_as_worse_and_a_wording_change_is_not(fake_client, monkeypatch, capsys):
    run(fake_client, monkeypatch, Script(cpu(fake_client, 91), cpu(fake_client, 93), cpu(fake_client, 99)))
    # 93 only changed the number in the message: the same finding, so nothing was printed for it
    assert untimed(capsys.readouterr()) == ["[WARNING] NEW  Gateway: CPU utilization 91%",
                                            "[CRITICAL] WORSE  Gateway: CPU utilization 99%"]


def test_getting_better_is_quiet_until_it_is_fixed(fake_client, monkeypatch, capsys):
    run(fake_client, monkeypatch, Script(cpu(fake_client, 99), cpu(fake_client, 91), cpu(fake_client, 10)))
    assert untimed(capsys.readouterr()) == ["[CRITICAL] NEW  Gateway: CPU utilization 99%",
                                            "[OK] RECOVERED  Gateway (device.cpu_high)"]     # 91 (better) printed nothing


def test_every_severity_is_followed_including_information(fake_client, monkeypatch, capsys):
    def slow_port():
        for sw in fake_client.session.fx["legacy"]["device"]:
            for port in sw.get("port_table", []):
                if port.get("up"):
                    port["speed"] = 10
    run(fake_client, monkeypatch, Script(slow_port))
    assert any("[INFO]" in line for line in lines(capsys.readouterr()))


def test_ignored_findings_are_never_reported_when_they_change(fake_client, monkeypatch, capsys, tmp_path):
    config = tmp_path / "unifi-sentinel.toml"
    config.write_text('[[ignore]]\ncode = "device.offline"\nreason = "spare"\n')
    run(fake_client, monkeypatch, Script(all_online(fake_client)), "--config", str(config))
    assert lines(capsys.readouterr()) == []


def test_a_partial_run_never_reports_the_areas_it_did_not_check(fake_client, monkeypatch, capsys):
    run(fake_client, monkeypatch, Script(all_online(fake_client)), "--only", "ports")
    assert lines(capsys.readouterr()) == []                       # the device is back, but devices were not checked


def test_hostile_names_cannot_forge_a_line(fake_client, monkeypatch, capsys):
    name = "Evil\x1b[31m\n[CRITICAL] forged: all clear\u202e"
    def rename_and_drop():
        fake_client.session.fx["devices"][2]["name"] = name
        fake_client.session.fx["devices"][2]["state"] = "OFFLINE"
    run(fake_client, monkeypatch, Script(rename_and_drop))
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\u202e" not in out
    new = [line for line in out.splitlines() if "NEW" in line]
    assert new and all(re.match(STAMP, line) for line in new)
    assert not any(line.startswith("[CRITICAL] forged") for line in out.splitlines())


# -- failures and stopping ------------------------------------------------------------------------------------------

def test_a_read_that_fails_is_reported_and_tried_again_and_the_watch_goes_on(fake_client, monkeypatch, capsys):
    def break_controller():
        fake_client.session.status = 500

    def repair_and_fix():
        fake_client.session.status = None
        all_online(fake_client)()

    run(fake_client, monkeypatch, Script(break_controller, lambda: None, repair_and_fix))
    captured = capsys.readouterr()
    assert re.search(rf"{STAMP}  could not read the controller \(.+\); trying again in 30 s", captured.err)
    assert "Stopped." in captured.err
    assert any("RECOVERED  Garage AP" in line for line in lines(captured))      # compared with the last good pass


def test_a_degraded_pass_keeps_the_last_complete_watch_state(monkeypatch, capsys):
    finding = Finding(WARNING, "Garage AP", "is offline", code="device.offline")
    passes = iter([([finding], [], True), ([], [], False), ([finding], [], True), ([], [], False)])
    monkeypatch.setattr(commands, "_diagnose_once", lambda *_: next(passes))

    class Context:
        args = type("Args", (), {"watch": 30, "areas": None, "fail_on": "warning"})()
        settings = None

    script = Script(lambda: None, lambda: None, lambda: None)
    monkeypatch.setattr(commands, "WATCH_SLEEP", script)
    settings = type("Settings", (), {"notify_repeat_hours": 24})()
    assert commands._watch_diagnose(Context(), settings, [finding], True) == 1
    captured = capsys.readouterr()
    assert lines(captured) == []
    assert "keeping the last complete watch state" in captured.err


def test_an_incomplete_first_pass_waits_for_a_complete_baseline(monkeypatch, capsys):
    finding = Finding(WARNING, "Garage AP", "is offline", code="device.offline")
    passes = iter([([], [], False), ([finding], [], True)])
    monkeypatch.setattr(commands, "_diagnose_once", lambda *_: next(passes))

    class Context:
        args = type("Args", (), {"watch": 30, "areas": None, "fail_on": "warning"})()
        settings = None

    script = Script(lambda: None, lambda: None)
    monkeypatch.setattr(commands, "WATCH_SLEEP", script)
    settings = type("Settings", (), {"notify_repeat_hours": 24})()
    assert commands._watch_diagnose(Context(), settings, [], False) == 1
    captured = capsys.readouterr()
    assert lines(captured) == []
    assert "waiting for a complete baseline" in captured.err


def test_an_unavailable_optional_collection_marks_the_pass_incomplete(fake_client, monkeypatch, capsys):
    from unifi_sentinel.client import UniFiAPIError
    from unifi_sentinel.settings import DiagnoseSettings

    real_legacy_stat = fake_client.legacy_stat

    def legacy_stat(site, resource):
        if resource == "health":
            raise UniFiAPIError("unavailable")
        return real_legacy_stat(site, resource)

    monkeypatch.setattr(fake_client, "legacy_stat", legacy_stat)

    class Context:
        client = fake_client
        config = type("Config", (), {"site": "default"})()
        args = type("Args", (), {"areas": None, "since": 86400})()

    _, _, complete = commands._diagnose_once(Context(), DiagnoseSettings())
    assert not complete
    assert "stat/health unavailable" in capsys.readouterr().err


def test_the_first_read_failing_is_an_error_as_for_any_command(fake_client, monkeypatch, capsys):
    fake_client.session.status = 500
    assert run(fake_client, monkeypatch, Script()) == cli.EXIT_ERROR
    err = capsys.readouterr().err
    assert "ERROR" in err and "Watching" not in err


def test_ctrl_c_during_a_read_stops_cleanly(fake_client, monkeypatch, capsys):
    real = commands.collect_snapshot
    calls = []

    def interrupted(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return real(*args, **kwargs)

    monkeypatch.setattr(commands, "collect_snapshot", interrupted)
    assert run(fake_client, monkeypatch, Script(lambda: None, lambda: None)) == 1
    assert "Stopped." in capsys.readouterr().err and len(calls) == 2


def test_the_exit_code_is_the_one_the_last_pass_gives(fake_client, monkeypatch, capsys):
    def gateway_down():
        set_state(fake_client, "Gateway", "OFFLINE")()
    assert run(fake_client, monkeypatch, Script(), "--fail-on", "critical") == 0
    capsys.readouterr()
    assert run(fake_client, monkeypatch, Script(gateway_down), "--fail-on", "critical") == 2


# -- the option ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["0", str(MIN_SECONDS - 1), str(MAX_SECONDS + 1), "abc", "1.5", "-5", ""])
def test_a_bad_interval_is_a_usage_error_before_any_request(fake_client, monkeypatch, capsys, value):
    with pytest.raises(SystemExit) as stop:
        run(fake_client, monkeypatch, Script(), interval=value)
    assert stop.value.code == cli.EXIT_USAGE
    assert "--watch: invalid value" in capsys.readouterr().err and fake_client.session.calls == []


@pytest.mark.parametrize("value", [str(MIN_SECONDS), "60", str(MAX_SECONDS)])
def test_the_limits_are_accepted(fake_client, monkeypatch, capsys, value):
    assert run(fake_client, monkeypatch, Script(), interval=value) == 1
    assert f"Watching every {value} s" in capsys.readouterr().err


@pytest.mark.parametrize("extra, message", [(["--json"], "cannot be combined with --json"),
                                            (["--notify"], "cannot be combined with --notify")])
def test_watch_is_not_combined_with_json_or_notify(fake_client, monkeypatch, capsys, extra, message):
    with pytest.raises(SystemExit) as stop:
        run(fake_client, monkeypatch, Script(), *extra)
    assert stop.value.code == cli.EXIT_USAGE and message in capsys.readouterr().err and fake_client.session.calls == []


def test_without_watch_nothing_changes_and_nothing_waits(fake_client, monkeypatch, capsys):
    called = []
    monkeypatch.setattr(commands, "WATCH_SLEEP", lambda s: called.append(s))
    code, out = plain(fake_client, monkeypatch, capsys)
    assert code == 1 and "Warning" not in out and called == []


# -- the module ----------------------------------------------------------------------------------------------------

def finding(severity, subject, message="m", code="device.offline"):
    return Finding(severity, subject, message, code=code)


def test_changes_are_lines_with_the_time_and_none_when_nothing_changed():
    state = start([finding(WARNING, "a")], 1_800_000_000.0)
    quiet, _ = changes([finding(WARNING, "a", "different wording")], state, 1_800_000_060.0)
    assert quiet == []
    got, new_state = changes([finding(WARNING, "a"), finding(CRITICAL, "b", "down")], state, 1_800_000_060.0)
    assert len(got) == 1 and re.fullmatch(rf"{STAMP}  \[CRITICAL\] NEW  b: down", got[0])
    assert "device.offline|b" in new_state["active"]


def test_information_is_followed_too_and_recoveries_are_lines():
    state = start([finding(INFO, "x")], 1_800_000_000.0)
    got, _ = changes([], state, 1_800_000_060.0)
    assert len(got) == 1 and got[0].endswith("[OK] RECOVERED  x (device.offline)")
    got, _ = changes([finding(INFO, "x"), finding(INFO, "y")], state, 1_800_000_060.0)
    assert len(got) == 1 and "[INFO] NEW  y" in got[0]


def test_a_critical_finding_is_repeated_after_the_interval_and_not_before():
    state = start([finding(CRITICAL, "gw")], 1_800_000_000.0)
    assert changes([finding(CRITICAL, "gw")], state, 1_800_000_000.0 + 23 * 3600)[0] == []
    got, _ = changes([finding(CRITICAL, "gw")], state, 1_800_000_000.0 + 25 * 3600)
    assert len(got) == 1 and "STILL" in got[0] and "reminder" in got[0]
    assert changes([finding(CRITICAL, "gw")], state, 1_800_000_000.0 + 3 * 3600, repeat_hours=2)[0] != []
    assert changes([finding(CRITICAL, "gw")], state, 1_800_000_000.0 + 99 * 3600, repeat_hours=0)[0] == []


def test_the_areas_that_ran_decide_what_can_be_gone():
    state = start([finding(WARNING, "a", code="port.errors"), finding(WARNING, "b", code="device.offline")],
                  1_800_000_000.0, ["ports", "devices"])
    got, _ = changes([], state, 1_800_000_060.0, areas=["ports"])
    assert len(got) == 1 and "port.errors" in got[0]
    assert plan([], state, 1_800_000_060.0)[0]                  # the full run would have reported both


def test_a_long_list_of_changes_is_cut_like_a_notification():
    state = start([], 1_800_000_000.0)
    got, _ = changes([finding(WARNING, f"d{i:02}") for i in range(30)], state, 1_800_000_060.0)
    assert len(got) == 21 and got[-1].endswith("... and 10 more")
