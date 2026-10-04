"""``--demo``: every command on the synthetic network, with nothing read from the machine and nothing sent."""

import json
import smtplib
import socket
from pathlib import Path

import pytest
import requests
from golden_support import run_command

from unifi_sentinel import cli, demo
from unifi_sentinel.client import UniFiClient
from unifi_sentinel.demo import DEMO_URL, DemoSession, demo_client, demo_config
from unifi_sentinel.demo.session import FIXTURE
from unifi_sentinel.settings import load_settings as real_load_settings

ROOT = Path(__file__).resolve().parent.parent
POISON_URL = "https://real-controller.example.net"
POISON_KEY = "real-api-key-0123456789abcdef"
POISON_HOOK = "https://hooks.real.example.net/secret-path"

COMMANDS = [
    ["info"], ["query", "devices"], ["query", "clients"], ["query", "reservations"], ["query", "ports"],
    ["query", "networks"], ["query", "wlans"], ["client", "desktop"], ["new-clients"], ["diagnose"],
    ["events"], ["wan"], ["firewall"], ["audit"], ["wifi"], ["topology"], ["completion", "bash"],
]


@pytest.fixture
def hostile_machine(monkeypatch, tmp_path):
    """A machine on which any attempt to read the user's settings or to reach a network fails loudly."""
    (Path.cwd() / ".env").write_text(f"CONTROLLER_URL={POISON_URL}\nAPI_KEY={POISON_KEY}\n")
    (Path.cwd() / "unifi-sentinel.toml").write_text("this is [not valid toml\n")
    for name, value in (("CONTROLLER_URL", POISON_URL), ("API_KEY", POISON_KEY), ("NOTIFY_WEBHOOK_URL", POISON_HOOK),
                        ("UNIFI_SENTINEL_ENV", str(tmp_path / "elsewhere.env")), ("SITE_ID", "real-site")):
        monkeypatch.setenv(name, value)

    def forbidden(*args, **kwargs):
        raise AssertionError("a demo must not read settings or use the network")

    for target in ("unifi_sentinel.cli.load_config", "unifi_sentinel.config.load_dotenv",
                   "unifi_sentinel.settings.load_settings"):
        monkeypatch.setattr(target, forbidden)
    monkeypatch.setattr("unifi_sentinel.cli.load_settings", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(requests.Session, "request", forbidden)
    monkeypatch.setattr(requests, "post", forbidden)
    monkeypatch.setattr(smtplib.SMTP, "__init__", forbidden)
    monkeypatch.setattr(smtplib.SMTP_SSL, "__init__", forbidden)


def run(capsys, *argv):
    code = cli.main(["--demo", *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# -- every command works, offline ------------------------------------------------------------------------------

@pytest.mark.parametrize("argv", COMMANDS, ids=lambda argv: " ".join(argv))
def test_every_command_runs_on_the_demo_data_with_nothing_read_or_sent(argv, hostile_machine, capsys):
    code, out, err = run(capsys, *argv)
    assert code in (0, 1, 2), err                    # diagnose and audit exit 1 or 2 for their findings
    assert out.strip()
    for text in (out, err):
        assert POISON_URL not in text and POISON_KEY not in text and POISON_HOOK not in text
        assert "Traceback" not in text and "ERROR" not in text
    if argv[0] != "completion":
        assert "Demo mode: synthetic data, no controller is contacted." in err


def test_export_writes_its_files_from_the_demo_data(hostile_machine, capsys, tmp_path):
    code, _, err = run(capsys, "export", "-o", str(tmp_path / "out"))
    assert code == 0, err
    assert (tmp_path / "out" / "unifi_clients.csv").read_text().count("\n") >= 2


@pytest.mark.parametrize("argv", [["query", "devices"], ["topology"], ["wifi"], ["audit"], ["firewall"], ["info"]],
                         ids=lambda argv: " ".join(argv))
def test_the_output_is_what_the_fake_controller_gives(argv, fake_client):
    expected = run_command(fake_client, argv)
    shown = run_command(fake_client, ["--demo", *argv])
    assert shown[0] == expected[0] and shown[1] == expected[1]


def test_json_output_stays_clean_and_the_notice_is_on_stderr(hostile_machine, capsys):
    code, out, err = run(capsys, "wan", "--json")
    assert code == 0 and json.loads(out)["version"] == 1
    assert err.splitlines()[0] == "Demo mode: synthetic data, no controller is contacted."


def test_the_verbose_header_says_demo_and_names_no_address(hostile_machine, capsys):
    code = cli.main(["--demo", "--verbose", "info"])
    err = capsys.readouterr().err
    assert code == 0
    assert "[verbose] unifi-sentinel " in err and "demo mode, synthetic data, no controller is contacted" in err
    assert "demo.invalid" not in err and "settings from" not in err
    assert "GET /proxy/network/integration/v1/info" in err


# -- nothing of the user's reaches a demo ------------------------------------------------------------------------------

def test_the_settings_file_in_the_working_directory_is_not_the_demos(hostile_machine, capsys):
    code, out, err = run(capsys, "diagnose")           # the toml next to it is invalid: reading it would fail
    assert code in (1, 2) and "Garage AP" in out and "toml" not in err.lower()


def test_a_named_settings_file_is_used(hostile_machine, capsys, monkeypatch, tmp_path):
    monkeypatch.setattr("unifi_sentinel.cli.load_settings", real_load_settings)
    named = tmp_path / "mine.toml"
    named.write_text('[[ignore]]\nmessage = "offline"\nreason = "demo"\n')
    code, out, _ = run(capsys, "diagnose", "--config", str(named), "--no-emoji")
    assert "Garage AP: device is offline" not in out and "ignored)" in out


@pytest.mark.parametrize("argv, fragment", [
    (["--env-file", "x.env", "info"], "--demo does not read a settings file"),
    (["doctor"], "cannot be used with doctor"),
    (["snapshot"], "cannot be used with snapshot"),
    (["diff"], "cannot be used with diff"),
    (["diagnose", "--notify"], "cannot be combined with --notify"),
    (["diagnose", "--notify", "--notify-dry-run"], "cannot be combined with --notify"),
    (["diagnose", "--notify", "--notify-baseline"], "cannot be combined with --notify"),
])
def test_what_would_touch_the_users_files_or_send_something_is_refused(argv, fragment, hostile_machine, capsys):
    with pytest.raises(SystemExit) as caught:
        cli.main(["--demo", *argv])
    assert caught.value.code == cli.EXIT_USAGE
    assert fragment in capsys.readouterr().err
    assert not (Path.cwd() / "snapshots").exists()


def test_the_demo_client_and_configuration_are_synthetic():
    config = demo_config("default")
    assert config.controller_url == DEMO_URL and DEMO_URL.endswith(".invalid")
    assert config.notify_smtp is None and not config.notify_ntfy_url and not config.notify_webhook_url
    client = demo_client(config)
    assert isinstance(client.session, DemoSession) and client.info() == FIXTURE["info"]
    assert demo_config("Lab", timeout=3, parallel=2).site == "Lab"
    assert not isinstance(client.session, requests.Session)


def test_the_demo_is_the_fake_controller_of_the_tests(fake_client):
    from conftest import FakeSession

    assert FakeSession is DemoSession and isinstance(fake_client.session, DemoSession)
    assert demo.FIXTURE is FIXTURE
    assert (ROOT / "unifi_sentinel" / "demo" / "controller.json").is_file()
    assert not (ROOT / "tests" / "fixtures" / "controller.json").exists()      # one copy only


def test_a_demo_can_be_a_parallel_read(hostile_machine, capsys):
    code, out, err = run(capsys, "--parallel", "8", "query", "devices")
    assert code == 0, err and out.strip()
    assert UniFiClient("https://x", "k", workers=8).workers == 8


def test_the_demo_event_log_honors_the_controllers_filters():
    from unifi_sentinel.demo.session import SYSTEM_LOG

    session = DemoSession()
    everything = session.post(f"{DEMO_URL}{SYSTEM_LOG}", json={"pageSize": 500}).json()["data"]
    assert everything
    key = everything[0]["key"]
    chosen = session.post(f"{DEMO_URL}{SYSTEM_LOG}", json={"keys": [key], "pageSize": 500}).json()["data"]
    assert chosen and {e["key"] for e in chosen} == {key} and len(chosen) <= len(everything)
    none = session.post(f"{DEMO_URL}{SYSTEM_LOG}", json={"keys": ["no.such.key"]}).json()
    assert none["data"] == [] and none["total_element_count"] == 0
    assert session.post(f"{DEMO_URL}/somewhere/else", json={}).status_code == 404     # the only POST route
