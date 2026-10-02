"""The configuration audit: Wi-Fi settings, default device names, firmware updates and unnamed clients."""

import json
import re
from pathlib import Path

import pytest
from docs_support import all_docs_text

from unifi_sentinel import cli
from unifi_sentinel.audit import AUDIT_AREAS, AUDIT_CODES, MAX_LISTED, audit
from unifi_sentinel.client import UniFiAPIError
from unifi_sentinel.snapshot import Needs, collect_snapshot, describe_snapshot

NEEDS = Needs(offline=True, wlans=True, legacy_devices=False, device_extras=False)
ROOT = Path(__file__).resolve().parent.parent


def snapshot(fake_client, change=None):
    if change:
        change(fake_client.session.fx)
    return collect_snapshot(fake_client, "default", NEEDS)


def found(fake_client, change=None):
    return {(f.code, f.subject): f for f in audit(snapshot(fake_client, change))}


def wlan(fx, name):
    return next(w for w in fx["legacy_rest"]["wlanconf"] if w["name"] == name)


# -- Wi-Fi -----------------------------------------------------------------------------------------------

def test_the_fixture_triggers_each_wifi_finding_once(fake_client):
    got = found(fake_client)
    assert got["audit.wifi_open", "Lobby"].severity == "warning"
    assert got["audit.wifi_weak_encryption", "OldCam"].severity == "warning"
    assert got["audit.wifi_no_wpa3", "GuestNet"].severity == "info"
    assert got["audit.wifi_guest_no_isolation", "GuestNet"].severity == "warning"
    assert not any(subject == "HomeNet" for _, subject in got)                 # WPA2/WPA3 mixed: nothing to say


def test_a_disabled_network_is_not_audited(fake_client):
    assert not any(subject == "Retired" for _, subject in found(fake_client))     # open, but switched off


@pytest.mark.parametrize("change, code", [
    (lambda w: w.update(security="wpapsk", wpa3_support=True), "audit.wifi_open"),
    (lambda w: w.update(security="wpapsk", wpa3_support=True), "audit.wifi_weak_encryption"),
    (lambda w: w.update(wpa3_support=True), "audit.wifi_no_wpa3"),
    (lambda w: w.update(l2_isolation=True), "audit.wifi_guest_no_isolation"),
    (lambda w: w.update(is_guest=False), "audit.wifi_guest_no_isolation"),
    (lambda w: w.update(enabled=False), "audit.wifi_no_wpa3"),
])
def test_each_wifi_finding_goes_away_when_the_setting_is_fixed(fake_client, change, code):
    name = {"audit.wifi_open": "Lobby", "audit.wifi_weak_encryption": "OldCam"}.get(code, "GuestNet")
    got = found(fake_client, lambda fx: change(wlan(fx, name)))
    assert (code, name) not in got


def test_an_open_guest_network_is_information_not_a_warning(fake_client):
    got = found(fake_client, lambda fx: wlan(fx, "Lobby").update(is_guest=True, l2_isolation=True))
    assert got["audit.wifi_open", "Lobby"].severity == "info" and "guest" in got["audit.wifi_open", "Lobby"].message


def test_a_network_without_wpa3_information_counts_as_wpa2_only(fake_client):
    got = found(fake_client, lambda fx: wlan(fx, "HomeNet").pop("wpa3_support"))
    assert ("audit.wifi_no_wpa3", "HomeNet") in got


def test_a_network_missing_most_fields_is_audited_without_crashing(fake_client):
    def change(fx):
        fx["legacy_rest"]["wlanconf"] = [{}, {"name": None, "security": None, "is_guest": None}, {"security": "open"}]

    got = {key for key in found(fake_client, change) if key[0].startswith("audit.wifi")}
    assert got == {("audit.wifi_open", "(unnamed network)")}


def test_unreadable_wifi_settings_are_reported_not_hidden(fake_client, monkeypatch, capsys):
    real = fake_client.legacy_rest

    def broken(site_ref, resource):
        if resource == "wlanconf":
            raise UniFiAPIError("HTTP 500")
        return real(site_ref, resource)

    monkeypatch.setattr(fake_client, "legacy_rest", broken)
    snap = collect_snapshot(fake_client, "default", NEEDS)
    assert snap.wlans is None and "Wi-Fi network settings unavailable; the Wi-Fi checks were skipped" in \
        capsys.readouterr().err
    got = {(f.code, f.subject): f for f in audit(snap)}
    assert got["audit.wifi_unavailable", "Wi-Fi"].severity == "info"
    assert not any(code.startswith("audit.wifi_open") for code, _ in got)       # no Wi-Fi verdicts at all
    assert any(code == "audit.firmware_update" for code, _ in got)              # the other checks still ran


def test_the_snapshot_reads_wifi_only_when_asked(fake_client):
    assert collect_snapshot(fake_client, "default").wlans is None
    snap = collect_snapshot(fake_client, "default", Needs(wlans=True))
    assert len(snap.wlans) == 5 and "5 Wi-Fi networks" in describe_snapshot(snap)
    assert not any(path.endswith("/rest/wlanconf") for path in fake_client.session.calls[:0])


# -- devices -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name, expected", [
    ("", "has no name"), ("   ", "has no name"), (None, "has no name"),
    ("u7 pro", "is still named after its model"), ("U7 PRO", "is still named after its model"),
    ("AA:00:00:00:00:03", "is named after its MAC address"), ("aa-00-00-00-00-03", "is named after its MAC address"),
    ("U7-Pro-00:00:03", "is named after its MAC address"), ("U7 Pro 000003", "is named after its MAC address"),
    ("Office AP", None), ("Hallway", None), ("AP 000004", None), ("U7-Pro", None),
])
def test_default_device_names(fake_client, name, expected):
    def change(fx):
        fx["devices"][2]["name"] = name

    got = found(fake_client, change)
    subject = name.strip() if name and name.strip() else "AA:00:00:00:00:03"
    finding = got.get(("audit.default_device_name", subject))
    assert (finding.message.split(";")[0] if finding else None) == expected
    if finding:
        assert finding.severity == "info" and finding.target_mac == "AA:00:00:00:00:03"


def test_a_device_without_a_model_or_mac_does_not_break_the_name_check(fake_client):
    def change(fx):
        fx["devices"][2].pop("model", None)
        fx["devices"][2].pop("macAddress", None)
        fx["devices"][2]["name"] = "Hallway"

    assert ("audit.default_device_name", "Hallway") not in found(fake_client, change)


def test_a_firmware_update_is_listed_per_device_and_only_when_true(fake_client):
    got = found(fake_client)
    assert got["audit.firmware_update", "Office Switch"].message == "firmware update available"
    assert got["audit.firmware_update", "Office Switch"].target_mac == "AA:00:00:00:00:02"
    assert sum(code == "audit.firmware_update" for code, _ in got) == 1          # False and missing are not updates

    def change(fx):
        for d in fx["devices"]:
            d["firmwareUpdatable"] = "yes" if d["name"] == "Gateway" else d.get("firmwareUpdatable")

    assert ("audit.firmware_update", "Gateway") not in found(fake_client, change)     # only a real True counts


# -- clients -------------------------------------------------------------------------------------------------

def unnamed(n, **extra):
    return {"mac": f"cc:00:00:00:00:{n:02x}", "is_wired": False, **extra}


def test_clients_with_neither_name_nor_hostname_make_one_summary(fake_client):
    def change(fx):
        fx["legacy"]["alluser"] += [unnamed(1, oui="Example Co"), unnamed(2, name="  ", hostname=""), unnamed(3)]

    finding = found(fake_client, change)["audit.unnamed_clients", "clients"]
    assert finding.severity == "info"
    assert finding.message == ("3 known clients have neither a name nor a hostname: CC:00:00:00:00:01 (Example Co), "
                               "CC:00:00:00:00:02, CC:00:00:00:00:03")


def test_one_unnamed_client_is_worded_in_the_singular(fake_client):
    assert found(fake_client, lambda fx: fx["legacy"]["alluser"].append(unnamed(1)))[
        "audit.unnamed_clients", "clients"].message.startswith("1 known client has neither")


def test_a_long_list_of_unnamed_clients_is_cut(fake_client):
    many = found(fake_client, lambda fx: fx["legacy"]["alluser"].extend(unnamed(i) for i in range(MAX_LISTED + 4)))
    message = many["audit.unnamed_clients", "clients"].message
    assert message.startswith(f"{MAX_LISTED + 4} known clients") and message.endswith(" and 4 more")
    assert message.count("CC:00") == MAX_LISTED


def test_named_clients_and_the_controllers_own_devices_are_not_unnamed(fake_client):
    def change(fx):
        fx["legacy"]["alluser"] += [unnamed(1, name="Kitchen"), unnamed(2, hostname="tv"),
                                    {"mac": "aa-00-00-00-00-03"}]                # a UniFi device, in another spelling

    assert ("audit.unnamed_clients", "clients") not in found(fake_client, change)


def test_without_the_client_history_there_is_nothing_to_say_about_clients(fake_client):
    snap = snapshot(fake_client)
    snap.all_users = []
    assert not any(f.code == "audit.unnamed_clients" for f in audit(snap))


# -- the report and the command ----------------------------------------------------------------------------

def test_findings_are_sorted_warnings_first_then_by_subject(fake_client):
    result = audit(snapshot(fake_client))
    assert [f.severity for f in result] == ["warning"] * 3 + ["info"] * 2
    assert [f.subject for f in result if f.severity == "warning"] == ["GuestNet", "Lobby", "OldCam"]


def test_every_code_is_listed_documented_and_used():
    source = (ROOT / "unifi_sentinel" / "audit.py").read_text()
    assert set(re.findall(r'code="(audit\.[a-z_0-9]+)"', source)) == set(AUDIT_CODES)
    readme = all_docs_text()
    assert all(f"`{code}`" in readme for code in AUDIT_CODES)


def run(fake_client, monkeypatch, argv):
    monkeypatch.setenv("CONTROLLER_URL", "https://controller.example")
    monkeypatch.setenv("API_KEY", "key")
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, c: fake_client))
    return cli.main(argv)


def test_the_command_prints_the_findings_and_follows_the_exit_code_rules(fake_client, monkeypatch, capsys):
    assert run(fake_client, monkeypatch, ["audit", "--no-emoji"]) == 1                 # warnings, like diagnose
    out = capsys.readouterr().out
    assert "[WARNING ] Lobby: is an open network" in out and out.rstrip().endswith("3 warnings, 2 info")
    assert run(fake_client, monkeypatch, ["audit", "--fail-on", "info", "--no-emoji"]) == 1


def test_only_information_exits_zero_unless_fail_on_info_is_given(fake_client, monkeypatch, capsys):
    def change(fx):
        fx["legacy_rest"]["wlanconf"] = [w for w in fx["legacy_rest"]["wlanconf"] if w["name"] == "HomeNet"]

    change(fake_client.session.fx)
    assert run(fake_client, monkeypatch, ["audit", "--no-emoji"]) == 0
    assert run(fake_client, monkeypatch, ["audit", "--no-emoji", "--fail-on", "info"]) == 1
    fake_client.session.fx["devices"][1]["firmwareUpdatable"] = False
    assert run(fake_client, monkeypatch, ["audit", "--no-emoji", "--fail-on", "info"]) == 0
    assert "No issues found." in capsys.readouterr().out


def test_json_has_the_same_shape_as_diagnose_with_audit_areas(fake_client, monkeypatch, capsys):
    assert run(fake_client, monkeypatch, ["audit", "--json"]) == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["version"] == 1 and doc["areas"] == list(AUDIT_AREAS)
    assert doc["summary"] == {"critical": 0, "warning": 3, "info": 2, "ignored": 0}
    assert {f["code"] for f in doc["findings"]} <= set(AUDIT_CODES)
    assert all(set(f) == {"severity", "code", "subject", "message", "mac"} for f in doc["findings"])


def test_the_ignore_list_applies_and_show_ignored_lists_what_it_hid(fake_client, monkeypatch, capsys, tmp_path):
    config = tmp_path / "unifi-sentinel.toml"
    config.write_text('[[ignore]]\nsubject = "Lobby"\nmessage = "open network"\nreason = "lobby is open on purpose"\n')
    assert run(fake_client, monkeypatch, ["audit", "--no-emoji", "--config", str(config), "--show-ignored"]) == 1
    out = capsys.readouterr().out
    assert "[WARNING ] Lobby" not in out and "(1 ignored)" in out
    assert "Lobby: is an open network" in out.split("Ignored (1):")[1] and "lobby is open on purpose" in out


def test_the_command_reads_wifi_and_the_client_history_but_no_devices_details_or_posts(fake_client, monkeypatch,
                                                                                         capsys):
    run(fake_client, monkeypatch, ["audit"])
    capsys.readouterr()
    calls = fake_client.session.calls
    assert any(c.endswith("/rest/wlanconf") for c in calls) and any(c.endswith("/stat/alluser") for c in calls)
    assert not any(c.endswith("/stat/device") for c in calls) and not fake_client.session.posts
    assert not [c for c in calls if "/devices/" in c and not c.endswith("/devices")]


def test_hostile_network_and_device_names_cannot_forge_output(fake_client, monkeypatch, capsys):
    def change(fx):
        wlan(fx, "Lobby")["name"] = "Lobby\x1b[31m\n[CRITICAL] forged: all clear\u202e"
        fx["devices"][1]["name"] = "Switch\x07\r\n[CRITICAL] forged"

    change(fake_client.session.fx)
    run(fake_client, monkeypatch, ["audit", "--no-emoji"])
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\u202e" not in out and "\x07" not in out
    assert not any(line.startswith("[CRITICAL]") for line in out.splitlines())


def test_the_audit_never_makes_a_request_the_other_commands_do_not(fake_client, monkeypatch, capsys):
    run(fake_client, monkeypatch, ["audit"])
    capsys.readouterr()
    assert all(not path.endswith(("/firewall-policies", "/stat/health", "/speedtest")) for path in
               fake_client.session.calls)


def test_a_wifi_passphrase_in_the_controllers_data_never_reaches_any_output(fake_client, monkeypatch, capsys):
    """rest/wlanconf carries each network's passphrase (x_passphrase) and the audit must never read or print it."""
    for w in fake_client.session.fx["legacy_rest"]["wlanconf"]:
        w.update(x_passphrase="SECRET-PASSPHRASE-1234", x_iapp_key="SECRET-IAPP-KEY", private_preshared_keys=[
            {"password": "SECRET-PPSK-5678"}])
    for argv in (["audit"], ["audit", "--json"], ["--verbose", "audit", "--json"]):
        run(fake_client, monkeypatch, argv)
        captured = capsys.readouterr()
        assert "SECRET" not in captured.out + captured.err, argv
