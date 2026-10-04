"""One MAC address, however a source spells it: the controller's endpoints are not consistent about it.

Every comparison of MACs goes through ``util.normalize_mac``. These tests re-spell the MACs of one data source
(dashes, dots, no separators, either side of a join) and require every command to say the same as before; a
comparison that still uses a bare ``.upper()`` shows up as a different answer. The source scan stops a new one.
"""

import ast
import json
import re
from collections import Counter
from pathlib import Path

import pytest
from golden_support import normalise

from homelab_probe import cli
from homelab_probe.util import normalize_mac

PACKAGE = Path(__file__).resolve().parent.parent / "homelab_probe"
MAC = re.compile(r"(?i)\b[0-9a-f]{2}(?::[0-9a-f]{2}){5}\b")

SPELLINGS = {
    "dashes": lambda m: m.upper().replace(":", "-"),
    "dots": lambda m: ".".join(m.replace(":", "")[i:i + 4] for i in (0, 4, 8)),
    "bare": lambda m: m.replace(":", "").upper(),
}

NON_MAC_UPPER_CALLS = {
    "diagnose/output.py": ("severity.upper()",),
    "export.py": ('(device.get("model") or "").upper()', "legacy_type.upper()",
                  'dev.get("type", "").upper()', 'dev.get("type", "").upper()'),
    "firewall.py": ("protocol.upper()", "protocol.upper()", "protocol.upper()"),
    "notify.py": ("severity.upper()", "e.severity.upper()"),
    "snapshot.py": ("c.upper()", "s.upper()"),
    "topology.py": ("severity.upper()", "f['severity'].upper()"),
    "util.py": ('":".join(digits[i:i + 2] for i in range(0, 12, 2)).upper()', "value.strip().upper()"),
}


def respell(value, how):
    if isinstance(value, dict):
        return {k: respell(v, how) for k, v in value.items()}
    if isinstance(value, list):
        return [respell(v, how) for v in value]
    if isinstance(value, str) and MAC.fullmatch(value):
        return SPELLINGS[how](value)
    return value


def unapproved_upper_calls(package):
    allowed = Counter((path, expression) for path, expressions in NON_MAC_UPPER_CALLS.items()
                      for expression in expressions)
    found = Counter()
    locations = {}
    for path in sorted(package.rglob("*.py")):
        relative = path.relative_to(package).as_posix()
        source = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "upper":
                expression = ast.get_source_segment(source, node) or ""
                key = (relative, expression)
                found[key] += 1
                locations.setdefault(key, []).append(node.lineno)
    offenders = []
    for (path, expression), count in found.items():
        offenders.extend(f"{path}:{line}: {expression}"
                         for line in locations[(path, expression)][:max(0, count - allowed[(path, expression)])])
    return offenders


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
    """`.upper()` calls are rejected unless explicitly allowlisted as non-MAC conversions."""
    offenders = unapproved_upper_calls(PACKAGE)
    assert not offenders, "use normalize_mac instead of .upper() on a MAC:\n" + "\n".join(offenders)


def test_mac_upper_guard_catches_a_multiline_alias(tmp_path):
    package = tmp_path / "homelab_probe"
    package.mkdir()
    (package / "bad.py").write_text('value = row.get(\n    "mac"\n)\nvalue.upper()\n', encoding="utf-8")
    assert unapproved_upper_calls(package) == ["bad.py:4: value.upper()"]


COMMANDS = [
    ["diagnose", "--no-events", "--json"], ["diagnose", "--no-emoji"], ["topology", "--clients", "--no-emoji"],
    ["wifi", "--all"], ["query", "ports"], ["query", "reservations"], ["query", "reservations", "--offline"],
    ["query", "clients", "--include-offline"], ["query", "devices"], ["new-clients"],
    ["client", "desktop", "--no-emoji"], ["client", "phone", "--no-emoji"], ["client", "old-printer", "--no-emoji"],
    ["events", "--summary"], ["firewall", "--no-emoji"],
]


def output(fake_client, monkeypatch, capsys, argv):
    monkeypatch.setenv("UNIFI_URL", "https://controller.example")
    monkeypatch.setenv("UNIFI_API_KEY", "key")
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, c: fake_client))
    code = cli.main(argv)
    out = normalise(capsys.readouterr().out)       # times come from when each fake session was made
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

    from homelab_probe.client import UniFiClient

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
