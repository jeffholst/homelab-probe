"""The exit-code table in the README is a contract for scripts and cron: each documented code is
produced by a real scenario, and nothing else is."""

import re
import subprocess
import sys
from pathlib import Path

import pytest

from homelab_probe import cli
from homelab_probe.client import UniFiAPIError
from homelab_probe.diagnose import EXIT_CRITICAL, EXIT_OK, EXIT_WARNING

README = Path(__file__).resolve().parent.parent / "README.md"
DOCUMENTED = {0, 1, 2, 3, 4, 64}


def documented_codes():
    after = re.split(r"#{3,4} Exit codes\n", README.read_text(encoding="utf-8"), maxsplit=1)[1]
    section = re.split(r"\n#{2,4} ", after, maxsplit=1)[0]
    return {int(m) for m in re.findall(r"(?m)^\| (\d+) \|", section)}


def run(fake_client, monkeypatch, argv):
    fake_client.session.fx["legacy"]["device"][0]["overheating"] = False
    monkeypatch.setenv("UNIFI_URL", "https://controller.example")
    monkeypatch.setenv("UNIFI_API_KEY", "key")
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, c: fake_client))
    return cli.main(argv)


def test_the_readme_documents_exactly_these_codes():
    assert documented_codes() == DOCUMENTED


def test_the_constants_match_the_documented_numbers():
    assert (EXIT_OK, EXIT_WARNING, EXIT_CRITICAL) == (0, 1, 2)
    assert (cli.EXIT_ERROR, cli.EXIT_NO_MATCH, cli.EXIT_USAGE) == (3, 4, 64)
    assert len({EXIT_OK, EXIT_WARNING, EXIT_CRITICAL, cli.EXIT_ERROR, cli.EXIT_NO_MATCH, cli.EXIT_USAGE}) == 6


def test_0_success(fake_client, monkeypatch):
    assert run(fake_client, monkeypatch, ["info"]) == 0
    assert run(fake_client, monkeypatch, ["query", "devices"]) == 0
    assert run(fake_client, monkeypatch, ["diagnose", "--no-events", "--fail-on", "critical"]) == 0   # warnings only


def test_0_also_when_nothing_is_wrong(fake_client, monkeypatch):
    fx = fake_client.session.fx
    fx["devices"] = [d for d in fx["devices"] if d["name"] != "Garage AP"]
    assert run(fake_client, monkeypatch, ["diagnose", "--no-events", "--fail-on", "critical"]) == 0


def test_1_a_warning_is_found(fake_client, monkeypatch):
    assert run(fake_client, monkeypatch, ["diagnose", "--no-events"]) == 1
    assert run(fake_client, monkeypatch, ["diagnose", "--no-events", "--fail-on", "info"]) == 1


def test_2_a_critical_finding(fake_client, monkeypatch):
    for device in fake_client.session.fx["devices"]:
        if device["name"] == "Gateway":
            device["state"] = "OFFLINE"
    assert run(fake_client, monkeypatch, ["diagnose", "--no-events"]) == 2
    assert run(fake_client, monkeypatch, ["diagnose", "--no-events", "--fail-on", "critical"]) == 2   # always 2


def test_3_a_bad_configuration(fake_client, monkeypatch, tmp_path):
    monkeypatch.delenv("UNIFI_URL", raising=False)
    monkeypatch.delenv("UNIFI_API_KEY", raising=False)
    assert cli.main(["info"]) == 3
    assert cli.main(["--env-file", str(tmp_path / "missing.env"), "info"]) == 3


def test_3_the_controller_cannot_be_reached_or_answers_with_an_error(fake_client, monkeypatch):
    fake_client.session.status = 500
    assert run(fake_client, monkeypatch, ["info"]) == 3
    fake_client.session.status = 401
    assert run(fake_client, monkeypatch, ["diagnose", "--no-events"]) == 3     # never mistaken for a finding


def test_3_a_bad_settings_file_stops_diagnose_before_any_request(fake_client, monkeypatch, tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text("[thresholds]\nnot_a_setting = 1\n")
    assert run(fake_client, monkeypatch, ["diagnose", "--config", str(bad)]) == 3
    assert fake_client.session.calls == []


def test_3_a_required_read_failing(fake_client, monkeypatch, tmp_path):
    legacy_stat = fake_client.legacy_stat

    def fail(site_ref, resource):
        if resource == "alluser":
            raise UniFiAPIError("unavailable")
        return legacy_stat(site_ref, resource)

    monkeypatch.setattr(fake_client, "legacy_stat", fail)
    assert run(fake_client, monkeypatch, ["new-clients"]) == 3


def test_4_the_client_is_not_found_or_ambiguous(fake_client, monkeypatch):
    assert run(fake_client, monkeypatch, ["client", "no-such-client"]) == 4
    assert run(fake_client, monkeypatch, ["client", "bb:00:00:00:00"]) == 4          # a fragment matching several


@pytest.mark.parametrize("argv", [["bogus"], ["diagnose", "--fail-on", "loud"], ["events", "--since", "soon"],
                                  ["query", "devices", "--down"], ["--timeout", "-1", "info"],
                                  ["wifi", "--band", "9 GHz"], ["diff", "a", "b", "c"]])
def test_64_a_usage_error(fake_client, monkeypatch, argv):
    with pytest.raises(SystemExit) as caught:
        run(fake_client, monkeypatch, argv)
    assert caught.value.code == 64


def test_findings_and_errors_never_share_a_code():
    """1 and 2 are reserved for findings: no error path may return them."""
    source = Path(cli.__file__).read_text(encoding="utf-8")
    assert "return 1" not in source and "return 2" not in source
    assert "EXIT_WARNING" not in source.split("def main", 1)[1] and "EXIT_CRITICAL" not in source.split("def main", 1)[1]


def test_the_same_codes_come_out_of_a_real_process(tmp_path):
    for args, code in ((["--version"], 0), (["nonsense"], 64), (["info"], 3)):
        result = subprocess.run([sys.executable, "-m", "homelab_probe.cli", *args], cwd=tmp_path,
                                capture_output=True, text=True, timeout=60)
        assert result.returncode == code, args
