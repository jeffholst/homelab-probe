"""Shared by the golden-file and docs-drift tests: run a command against the synthetic fixture and
normalise the parts of its output that legitimately change from run to run."""

import contextlib
import io
import os
import re
import time
from pathlib import Path
from unittest import mock

from unifi_sentinel import cli

GOLDEN = Path(__file__).parent / "golden"

# name -> argv. The README's sample blocks for these commands must equal these outputs.
CASES = {
    "topology": ["topology", "--no-emoji"],
    "topology_clients": ["topology", "--no-emoji", "--clients"],
    "wan": ["wan"],
    "wifi": ["wifi"],
    "wifi_all": ["wifi", "--all"],
    "client_desktop": ["client", "desktop", "--no-emoji"],
    "client_phone": ["client", "phone", "--no-emoji"],
    "client_old_printer": ["client", "old-printer", "--no-emoji"],
    "diagnose": ["diagnose", "--no-events", "--no-emoji"],
    "diagnose_with_events": ["diagnose", "--no-emoji"],
    "diagnose_json": ["diagnose", "--json", "--no-events"],
    "firewall": ["firewall", "--no-emoji"],
    "audit": ["audit", "--no-emoji"],
    "firewall_zones": ["firewall", "--zones", "--all", "--no-emoji"],
    "events": ["events", "--client", "phone", "--since", "6h"],
    "events_summary": ["events", "--summary"],
    "new_clients": ["new-clients"],
    "query_devices": ["query", "devices"],
    "query_clients": ["query", "clients", "--include-offline"],
    "query_ports": ["query", "ports"],
    "query_reservations": ["query", "reservations"],
}

_TIME = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?")
_AGE = re.compile(r"\b\d+[mhd] ago\b")
_SNAPSHOT = re.compile(r"snapshot-\d{8}-\d{6}Z?(?:-\d+)?\.json")


def normalise(text: str) -> str:
    """Replace times, ages and snapshot names by placeholders and collapse table padding, so output
    that only differs in *when* it ran (the fixture's times are relative to now) compares equal."""
    lines = []
    for raw in text.splitlines():
        line = _SNAPSHOT.sub("snapshot-<stamp>.json", _AGE.sub("<age> ago", _TIME.sub("<time>", raw.rstrip())))
        indent = len(line) - len(line.lstrip(" "))
        body = re.sub(r"-{3,}", "---", re.sub(r" {2,}", "  ", line.strip()))
        lines.append(" " * indent + body)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) + "\n"


def run_command(fake_client, argv):
    """(exit code, stdout, stderr) of the real CLI against the fake controller, in UTC."""
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    if hasattr(time, "tzset"):
        time.tzset()
    os.environ.update(CONTROLLER_URL="https://controller.example", API_KEY="key")
    out, err = io.StringIO(), io.StringIO()
    try:
        with mock.patch.object(cli.UniFiClient, "from_config", classmethod(lambda cls, cfg: fake_client)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        if hasattr(time, "tzset"):
            time.tzset()
        os.environ.pop("CONTROLLER_URL", None)
        os.environ.pop("API_KEY", None)
    return code, out.getvalue(), err.getvalue()
