"""One MAC address, however a source spells it: the controller's endpoints are not consistent about it.

Every comparison of MACs goes through ``util.normalize_mac``. These tests re-spell the MACs of one data source
(dashes, dots, no separators, either side of a join) and require every command to say the same as before; a
comparison that still uses a bare ``.upper()`` shows up as a different answer. The source scan stops a new one.
"""

import json
import re
from pathlib import Path

import pytest

from unifi_sentinel import cli
from unifi_sentinel.util import normalize_mac

PACKAGE = Path(__file__).resolve().parent.parent / "unifi_sentinel"
MAC = re.compile(r"(?i)\b[0-9a-f]{2}(?::[0-9a-f]{2}){5}\b")

SPELLINGS = {
    "dashes": lambda m: m.upper().replace(":", "-"),
    "dots": lambda m: ".".join(m.replace(":", "")[i:i + 4] for i in (0, 4, 8)),
    "bare": lambda m: m.replace(":", "").upper(),
}


def respell(value, how):
    if isinstance(value, dict):
        return {k: respell(v, how) for k, v in value.items()}
    if isinstance(value, list):
        return [respell(v, how) for v in value]
    if isinstance(value, str) and MAC.fullmatch(value):
        return SPELLINGS[how](value)
    return value


@pytest.mark.parametrize("value, expected", [
    ("aa:bb:cc:dd:ee:ff", "AA:BB:CC:DD:EE:FF"), ("AA-BB-CC-DD-EE-FF", "AA:BB:CC:DD:EE:FF"),
    ("aabb.ccdd.eeff", "AA:BB:CC:DD:EE:FF"), ("aabbccddeeff", "AA:BB:CC:DD:EE:FF"),
    ("  aa:bb:cc:dd:ee:ff ", "AA:BB:CC:DD:EE:FF"),
    ("Office AP", "OFFICE AP"), ("aa:bb:cc", "AA:BB:CC"), ("", ""), ("  x ", "X"),
    (None, ""), (260000000000, ""), (["aa"], ""),
])
def test_normalize_mac(value, expected):
    assert normalize_mac(value) == expected


def test_no_comparison_uses_a_bare_upper_on_a_mac():
    """`(d.get("mac") or "").upper()` was copied into dozens of places and each copy compared spellings of one
    address as different (found three times in review). Use util.normalize_mac."""
    pattern = re.compile(r"\.upper\(\)")
    offenders = []
    for path in sorted(PACKAGE.rglob("*.py")):
        if path.name == "util.py":
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line) and re.search(r"(?i)mac", line):
                offenders.append(f"{path.relative_to(PACKAGE.parent)}:{number}: {line.strip()}")
    assert not offenders, "use normalize_mac instead of .upper() on a MAC:\n" + "\n".join(offenders)


COMMANDS = [
    ["diagnose", "--no-events", "--json"], ["diagnose", "--no-emoji"], ["topology", "--clients", "--no-emoji"],
    ["wifi", "--all"], ["query", "ports"], ["query", "reservations"], ["query", "reservations", "--offline"],
    ["query", "clients", "--include-offline"], ["query", "devices"], ["new-clients"],
    ["client", "desktop", "--no-emoji"], ["client", "phone", "--no-emoji"], ["client", "old-printer", "--no-emoji"],
    ["events", "--summary"], ["firewall", "--no-emoji"],
]


def output(fake_client, monkeypatch, capsys, argv):
    monkeypatch.setenv("CONTROLLER_URL", "https://controller.example")
    monkeypatch.setenv("API_KEY", "key")
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, c: fake_client))
    code = cli.main(argv)
    out = capsys.readouterr().out
    return code, MAC.sub(lambda m: m.group().upper(), out)


def sides(fx, how, side):
    if side in ("legacy", "both"):
        fx["legacy"] = respell(fx["legacy"], how)
    if side in ("integration", "both"):
        for key in ("devices", "clients"):
            fx[key] = respell(fx[key], how)
    return fx


@pytest.mark.parametrize("argv", COMMANDS, ids=lambda a: " ".join(a))
@pytest.mark.parametrize("side", ["legacy", "integration", "both"])
@pytest.mark.parametrize("how", sorted(SPELLINGS))
def test_every_command_says_the_same_whatever_spelling_a_source_uses(fake_client, monkeypatch, capsys, argv, side,
                                                                      how):
    from conftest import FakeSession

    from unifi_sentinel.client import UniFiClient

    baseline_client = UniFiClient("https://controller", "key")
    baseline_client.session = FakeSession()
    expected = output(baseline_client, monkeypatch, capsys, argv)
    sides(fake_client.session.fx, how, side)
    assert output(fake_client, monkeypatch, capsys, argv) == expected


def test_a_reservation_still_matches_its_client_in_every_spelling(fake_client, monkeypatch, capsys):
    for how in SPELLINGS:
        fx = fake_client.session.fx
        for user in fx["legacy"]["alluser"]:
            user["mac"] = SPELLINGS[how](normalize_mac(user["mac"]).lower())
        code, out = output(fake_client, monkeypatch, capsys, ["diagnose", "--no-events", "--json"])
        findings = json.loads(out)["findings"]
        assert not any(f["code"] == "reservation.ip_mismatch" or "desktop" in f["subject"] and
                       f["code"] == "reservation.offline" for f in findings), how
