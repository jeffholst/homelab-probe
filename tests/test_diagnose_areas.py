"""``diagnose --only`` and ``--skip``: areas, what each reads, the options, and notifications for skipped areas."""

import ast
import json
from pathlib import Path

import pytest
from test_notify import FakePost, finding, make_env
from test_notify import diagnose as notify_args

from unifi_sentinel import cli
from unifi_sentinel import notify as notify_module
from unifi_sentinel.commands import diagnose_areas
from unifi_sentinel.diagnose import AREA_NAMES, AREAS, CODES, area_of, codes_of, diagnose, needs_for, parse_areas
from unifi_sentinel.diagnose import areas as areas_module
from unifi_sentinel.notify import baseline, plan
from unifi_sentinel.settings import DiagnoseSettings
from unifi_sentinel.snapshot import Needs, collect_snapshot

PACKAGE = Path(areas_module.__file__).parent
ALL = list(AREA_NAMES)


# -- areas and codes -------------------------------------------------------------------------------------------

def test_every_code_is_in_exactly_one_area_and_the_areas_cover_the_catalogue():
    assert all(area_of(code) in AREAS for code in CODES), [c for c in CODES if area_of(c) is None]
    parts = [codes_of(area) for area in AREA_NAMES]
    assert sorted(code for part in parts for code in part) == sorted(CODES) and all(parts)


def test_the_areas_are_the_documented_ones_in_order():
    assert ALL == ["devices", "health", "wan", "clients", "reservations", "ports", "wifi", "events"]
    assert area_of("controller.pending_adoption") == "devices" and area_of("internet.drops") == "health"
    assert area_of("ip.duplicate") == "clients" and area_of("link.below_capability") == "ports"
    assert area_of("") is None and area_of("firewall.forward_duplicate") is None and area_of("nothing") is None


def test_parse_areas_splits_commas_repeats_and_ignores_case_space_and_duplicates():
    assert parse_areas(["Ports, wifi", "ports", " "]) == (["ports", "wifi"], [])
    assert parse_areas(["wifi,bogus", "Other"]) == (["wifi"], ["bogus", "other"])


def _check_functions():
    """{function name: codes it can emit} from the source of the check modules."""
    emitted = {}
    for path in PACKAGE.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("_") and node.name.endswith("_findings"):
                emitted[node.name] = {n.value for n in ast.walk(node)
                                      if isinstance(n, ast.Constant) and n.value in CODES}
    return emitted


def _registered():
    """{function name: areas} from the CHECKS table in areas.py."""
    table = next(n for n in ast.walk(ast.parse((PACKAGE / "areas.py").read_text(encoding="utf-8")))
                 if isinstance(n, ast.AnnAssign) and getattr(n.target, "id", "") == "CHECKS")
    found = {}
    for entry in table.value.elts:
        call = entry.elts[0].body
        found[call.func.id] = tuple(a.value for a in entry.elts[1].elts)
    return found


def test_every_check_is_registered_with_every_area_it_can_emit():
    emitted, registered = _check_functions(), _registered()
    helpers = {"_conflict_findings"}                                               # called by _event_findings
    assert set(registered) | helpers == set(emitted), set(emitted) ^ (set(registered) | helpers)
    for name, areas in registered.items():
        assert {area_of(code) for code in emitted[name]} <= set(areas), name


def test_the_registry_runs_the_checks_in_the_documented_order():
    assert list(_registered()) == [
        "_offline_device_findings", "_resource_findings", "_overheating_findings", "_health_findings", "_wan_findings", "_client_ip_findings",
        "_reservation_findings", "_pool_findings", "_offline_reservation_findings", "_private_mac_findings",
        "_duplicate_ip_findings", "_legacy_unavailable_findings", "_port_basic_findings", "_port_health_findings",
        "_uplink_speed_findings", "_wifi_findings", "_event_findings"]


# -- selecting checks ------------------------------------------------------------------------------------------

def snap_for(fake_client, areas):
    return collect_snapshot(fake_client, "default", needs_for(areas, 86400))


def test_unrelated_checks_do_not_read_device_details_and_statistics(fake_client):
    snap_for(fake_client, ["wifi"])
    assert not any("/devices/" in path and not path.endswith("/devices") for path in fake_client.session.calls)


def test_client_checks_read_legacy_attachment_details(fake_client):
    snap_for(fake_client, ["clients"])
    assert any(path.endswith("/stat/sta") for path in fake_client.session.calls)


def test_wan_checks_skip_unrelated_legacy_reads(fake_client):
    snap_for(fake_client, ["wan"])
    assert not any(path.endswith(("/stat/device", "/stat/sta")) for path in fake_client.session.calls)
    assert not any(path.endswith(("/sites/site-1/devices", "/sites/site-1/clients"))
                   for path in fake_client.session.calls)


def test_port_checks_read_device_details_without_statistics(fake_client):
    snap_for(fake_client, ["ports"])
    assert any("/devices/" in path and not path.endswith("/devices") for path in fake_client.session.calls)
    assert not any(path.endswith("/statistics/latest") for path in fake_client.session.calls)


def make_every_area_report(fake_client):
    """Change the fake controller so that each of the eight areas has at least one finding."""
    fx = fake_client.session.fx
    fx["clients"][1]["ipAddress"] = ""                                             # clients: no IP
    next(h for h in fx["legacy"]["health"] if h["subsystem"] == "wan")["wan_ip"] = "192.168.1.5"   # wan: double NAT
    next(u for u in fx["legacy"]["sta"] if u["name"] == "phone")["signal"] = -88   # wifi: weak signal


@pytest.fixture
def everything(fake_client):
    make_every_area_report(fake_client)
    return diagnose(snap_for(fake_client, None), DiagnoseSettings())


def test_each_area_selects_exactly_its_codes_with_only_the_data_it_reads(fake_client, everything):
    for area in AREA_NAMES:
        got = diagnose(snap_for(fake_client, [area]), DiagnoseSettings(), areas=[area])
        want = [f for f in everything if area_of(f.code) == area]
        assert want, f"the fixture has no finding for {area}, so this check proves nothing about it"
        assert [(f.severity, f.code, f.subject) for f in got] == [(f.severity, f.code, f.subject) for f in want], area
        assert {f.message for f in got} == {f.message for f in want}, area


def test_a_selection_of_several_areas_and_the_full_run(fake_client, everything):
    chosen = ["ports", "wifi"]
    got = diagnose(snap_for(fake_client, chosen), DiagnoseSettings(), areas=chosen)
    assert {area_of(f.code) for f in got} <= set(chosen) and {area_of(f.code) for f in got} == set(chosen)
    assert [f.code for f in diagnose(snap_for(fake_client, None), DiagnoseSettings(), areas=ALL)] == [
        f.code for f in everything]
    assert [f.code for f in diagnose(snap_for(fake_client, None), DiagnoseSettings())] == [f.code for f in everything]


def test_findings_of_another_area_that_a_check_also_emits_are_dropped(fake_client):
    snap = snap_for(fake_client, ["health"])
    snap.health = [{"subsystem": "wlan", "status": "ok", "num_pending": 2}]
    assert not {f.code for f in diagnose(snap, DiagnoseSettings(), areas=["health"])}
    devices = {f.code for f in diagnose(snap, DiagnoseSettings(), areas=["devices"])}
    assert "controller.pending_adoption" in devices and {area_of(c) for c in devices} == {"devices"}


@pytest.mark.parametrize("areas, expected", [
    (["ports"], Needs(devices=True, clients=False, device_extras=True, device_stats=False, legacy_devices=True,
                      legacy_clients=False)),
    (["wifi"], Needs(devices=True, clients=False, device_extras=False, device_stats=False, legacy_devices=True,
                     legacy_clients=True)),
    (["clients"], Needs(devices=True, clients=True, device_extras=False, device_stats=False, legacy_devices=False,
                        legacy_clients=True)),
    (["health"], Needs(health=True, devices=False, clients=False, device_extras=False, device_stats=False,
                       legacy_devices=False, legacy_clients=False)),
    (["devices"], Needs(health=True, devices=True, clients=False, device_extras=True, device_stats=True,
                        legacy_devices=True, legacy_clients=False)),
    (["wan"], Needs(health=True, speedtests=True, devices=False, clients=False, device_extras=False,
                    device_stats=False, legacy_devices=False, legacy_clients=False)),
    (["reservations"], Needs(reservations=True, devices=True, clients=True, device_extras=False,
                             device_stats=False, legacy_devices=False, legacy_clients=False)),
])
def test_what_a_selection_reads(areas, expected):
    assert needs_for(areas, 3600) == expected


def test_the_default_reads_everything_and_events_use_the_window():
    needs = needs_for(None, 7200)
    assert (needs.reservations, needs.health, needs.speedtests) == (True, True, True)
    assert needs.events is not None and needs.events.since_seconds == 7200
    assert needs_for(["events"], 60).events.since_seconds == 60 and needs_for(["ports"], 60).events is None
    assert needs_for(["events"], 60).reservations                  # an IP conflict names who holds the reservation


# -- the options -----------------------------------------------------------------------------------------------

def run(fake_client, monkeypatch, argv):
    monkeypatch.setenv("CONTROLLER_URL", "https://controller.example")
    monkeypatch.setenv("API_KEY", "key")
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, c: fake_client))
    return cli.main(argv)


def only(areas):
    class Args:
        pass

    args = Args()
    args.only, args.skip, args.no_events = areas[0], areas[1], areas[2]
    return args


@pytest.mark.parametrize("given, expected", [
    ((["ports"], [], False), ["ports"]),
    ((["wifi,PORTS", "ports"], [], False), ["ports", "wifi"]),             # canonical order, no repeats
    (([], ["events"], False), ALL[:-1]),
    (([], [], True), ALL[:-1]),
    (([], ["events,wifi"], True), ALL[:-2]),
    ((["ports", "events"], [], False), ["ports", "events"]),
    (([], [], False), None),
    ((ALL, [], False), None),                                              # naming everything is the full run
    (([], ["ports"], False), [a for a in ALL if a != "ports"]),
])
def test_the_areas_chosen_by_the_options(given, expected):
    assert diagnose_areas(only(given)) == expected


@pytest.mark.parametrize("given, message", [
    ((["nope"], [], False), "unknown area 'nope'; the areas are devices, health"),
    (([], ["ports,nope", "x"], False), "unknown area 'nope', 'x'"),
    ((["ports"], ["wifi"], False), "--only and --skip cannot be combined"),
    ((["events"], [], True), "--no-events contradicts --only events"),
    ((ALL, [], False), None),
    (([","], [], False), "--only names no area"),
    (([], [",".join(ALL)], False), "every area is skipped"),
    (([], [], False), None),
])
def test_options_that_cannot_work_say_why(given, message):
    if message is None:
        assert diagnose_areas(only(given)) is None
        return
    with pytest.raises(ValueError, match=message):
        diagnose_areas(only(given))


@pytest.mark.parametrize("argv, message", [
    (["--only", "nope"], "unknown area 'nope'; the areas are devices, health, wan, clients, reservations, ports"),
    (["--only", "ports", "--skip", "wifi"], "--only and --skip cannot be combined"),
    (["--skip", ",".join(ALL)], "every area is skipped"),
    (["--only", "events", "--no-events"], "contradicts"),
])
def test_bad_selections_are_usage_errors_before_any_request(fake_client, monkeypatch, capsys, argv, message):
    with pytest.raises(SystemExit) as stop:
        run(fake_client, monkeypatch, ["diagnose", *argv])
    assert stop.value.code == cli.EXIT_USAGE
    assert message in capsys.readouterr().err and fake_client.session.calls == []


def test_only_and_skip_are_repeatable_and_comma_separated(fake_client, monkeypatch, capsys):
    assert run(fake_client, monkeypatch, ["diagnose", "--json", "--only", "ports,wifi", "--only", "wan"]) in (0, 1, 2)
    assert json.loads(capsys.readouterr().out)["areas"] == ["wan", "ports", "wifi"]
    run(fake_client, monkeypatch, ["diagnose", "--json", "--skip", "events", "--skip", "wifi,ports"])
    assert json.loads(capsys.readouterr().out)["areas"] == ["devices", "health", "wan", "clients", "reservations"]


def test_json_says_which_areas_ran_and_only_their_findings_are_there(fake_client, monkeypatch, capsys):
    code = run(fake_client, monkeypatch, ["diagnose", "--json", "--only", "ports"])
    doc = json.loads(capsys.readouterr().out)
    assert doc["areas"] == ["ports"] and doc["findings"] and all(area_of(f["code"]) == "ports" for f in doc["findings"])
    assert code == 1 and doc["version"] == 1                       # warnings still give exit code 1


def test_exit_codes_and_fail_on_follow_the_findings_that_ran(fake_client, monkeypatch, capsys):
    assert run(fake_client, monkeypatch, ["diagnose", "--only", "clients", "--no-emoji"]) == 0   # nothing in that area
    assert "No issues found." in capsys.readouterr().out
    assert run(fake_client, monkeypatch, ["diagnose", "--only", "ports"]) == 1                    # warnings
    make_every_area_report(fake_client)
    assert run(fake_client, monkeypatch, ["diagnose", "--only", "wifi"]) == 1
    assert run(fake_client, monkeypatch, ["diagnose", "--only", "devices"]) in (1, 2)
    assert run(fake_client, monkeypatch, ["diagnose", "--only", "wifi", "--fail-on", "critical"]) == 0
    capsys.readouterr()


def test_the_text_says_what_was_checked_when_a_subset_ran(fake_client, monkeypatch, capsys):
    run(fake_client, monkeypatch, ["diagnose", "--only", "ports,wifi", "--no-emoji"])
    out = capsys.readouterr().out
    assert out.rstrip().endswith("Checked: ports, wifi (not checked: devices, health, wan, clients, reservations, "
                                 "events)")
    run(fake_client, monkeypatch, ["diagnose", "--no-emoji", "--no-events"])
    assert "Checked:" not in capsys.readouterr().out                # --no-events alone prints what it always did


def test_no_events_is_the_same_as_skip_events(fake_client, monkeypatch, capsys):
    run(fake_client, monkeypatch, ["diagnose", "--json", "--no-events"])
    first = json.loads(capsys.readouterr().out)
    run(fake_client, monkeypatch, ["diagnose", "--json", "--skip", "events"])
    assert json.loads(capsys.readouterr().out) == first and fake_client.session.posts == []


# -- what each selection reads from the controller ---------------------------------------------------------------

@pytest.mark.parametrize("argv, kinds, posts", [
    (["--only", "ports"], {"devices", "legacy-devices"}, False),
    (["--only", "wifi"], {"devices", "legacy-devices", "legacy-clients"}, False),
    (["--only", "clients"], {"devices", "clients", "legacy-clients"}, False),
    (["--only", "health"], {"health"}, False),
    (["--only", "devices"], {"devices", "health", "legacy-devices"}, False),
    (["--only", "wan"], {"health", "speedtests"}, False),
    (["--only", "reservations"], {"devices", "clients", "alluser", "networkconf"}, False),
    (["--only", "events"], {"devices", "alluser", "networkconf"}, True),
    (["--skip", "events"], {"devices", "clients", "alluser", "networkconf", "health", "speedtests",
                             "legacy-devices", "legacy-clients"}, False),
    (["--skip", "wan,reservations"], {"devices", "clients", "health", "alluser", "networkconf",
                                      "legacy-devices", "legacy-clients"}, True),   # events still need them
])
def test_a_selection_reads_only_what_its_checks_need(fake_client, monkeypatch, capsys, argv, kinds, posts):
    from test_needs import reads

    run(fake_client, monkeypatch, ["diagnose", *argv])
    capsys.readouterr()
    assert reads(fake_client) - {"events"} == kinds
    assert bool(fake_client.session.posts) is posts


# -- notifications: skipped areas are neither new nor recovered ---------------------------------------------------

def state_with(*findings, now=1.0):
    return plan(list(findings), {"version": 1, "active": {}}, now)[1]


def test_a_finding_of_an_area_that_did_not_run_is_not_recovered_and_keeps_its_entry():
    stored = state_with(finding("warning", "Switch", code="port.errors"), finding("critical", "Garage", code="device.offline"))
    events, new = plan([], stored, 99.0, areas=["ports"])
    assert [(e.kind, e.code) for e in events] == [("recovered", "port.errors")]
    assert new["active"] == {k: v for k, v in stored["active"].items() if k.startswith("device.offline")}
    assert new["active"]["device.offline|Garage"]["last_notified"] == 1.0


def test_a_skipped_area_is_not_reminded_and_a_run_area_still_is():
    stored = state_with(finding("critical", "Garage", code="device.offline"), finding("critical", "Sw", code="port.stp"))
    events, new = plan([finding("critical", "Sw", code="port.stp")], stored, 1.0 + 30 * 3600, areas=["ports"])
    assert [(e.kind, e.code) for e in events] == [("reminder", "port.stp")]
    assert new["active"]["device.offline|Garage"]["last_notified"] == 1.0


def test_nothing_is_new_in_an_area_that_did_not_run_and_a_full_run_still_recovers_everything():
    stored = state_with(finding("warning", "x", code="wan.cgnat"))
    assert plan([], stored, 5.0, areas=["wifi"])[0] == []
    assert [e.kind for e in plan([], stored, 5.0)[0]] == ["recovered"]
    assert [e.kind for e in plan([], stored, 5.0, areas=ALL)[0]] == ["recovered"]


def test_an_entry_whose_code_belongs_to_no_area_is_treated_as_checked():
    stored = {"version": 1, "active": {"old.thing|x": {"severity": "warning", "first_notified": 1.0,
                                                       "last_notified": 1.0}}}
    assert [e.kind for e in plan([], stored, 5.0, areas=["ports"])[0]] == ["recovered"]


def test_a_baseline_after_a_partial_run_keeps_the_other_areas():
    stored = state_with(finding("warning", "Garage", code="device.offline"), finding("warning", "Sw", code="port.errors"))
    saved = baseline([finding("warning", "Sw2", code="port.errors")], 9.0, areas=["ports"], state=stored)
    assert set(saved["active"]) == {"device.offline|Garage", "port.errors|Sw2"}
    assert saved["active"]["device.offline|Garage"]["last_notified"] == 1.0
    assert set(baseline([finding("warning", "Sw2", code="port.errors")], 9.0)["active"]) == {"port.errors|Sw2"}
    assert set(baseline([], 9.0, areas=["ports"], state={"version": 1, "active": {"a|b": "damaged"}})["active"]) == set()


def test_a_partial_notified_run_does_not_announce_other_areas_recoveries(fake_client, monkeypatch, capsys, tmp_path):
    fake = FakePost(200)
    monkeypatch.setattr(notify_module.requests, "post", fake)
    make_env(monkeypatch)
    state = tmp_path / "state.json"
    run(fake_client, monkeypatch, notify_args(state=state))                      # everything is reported once
    first = json.loads(state.read_text())["active"]
    assert "device.offline|Garage AP" in first
    for device in fake_client.session.fx["devices"]:
        device["state"] = "ONLINE"                                               # the Garage AP is back...
    sent = len(fake.calls)
    run(fake_client, monkeypatch, notify_args("--only", "ports", state=state))   # ...but only ports are checked
    capsys.readouterr()
    assert len(fake.calls) == sent                                               # nothing is announced
    assert json.loads(state.read_text())["active"] == first                      # and the state is untouched
    run(fake_client, monkeypatch, notify_args(state=state))                      # a full run notices the recovery
    assert any("RECOVERED  Garage AP (device.offline)" in c[1]["data"].decode() for c in fake.calls[sent:])


def test_a_partial_baseline_keeps_what_was_recorded_for_other_areas(fake_client, monkeypatch, capsys, tmp_path):
    make_env(monkeypatch, ntfy="")
    state = tmp_path / "state.json"
    run(fake_client, monkeypatch, notify_args("--notify-baseline", state=state))
    full = json.loads(state.read_text())["active"]
    run(fake_client, monkeypatch, notify_args("--notify-baseline", "--only", "wifi", state=state))
    capsys.readouterr()
    assert {k for k in json.loads(state.read_text())["active"] if not k.startswith("wifi.")} == {
        k for k in full if not k.startswith("wifi.")}
