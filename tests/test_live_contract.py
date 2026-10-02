"""Does a real controller still return the fields the code reads? Opt in with ``uv run pytest -m live``.

Skipped by default and in CI. It reads the controller named by ``CONTROLLER_URL``/``API_KEY`` (environment or
``./.env``) once, the way ``diagnose`` does, with GET requests and the one approved read-only event log query
(``tests/contract.py`` ``RecordingSession`` refuses anything else), then checks every endpoint against the
contract. Failures name the endpoint and field, never a value: no address, name or key is printed.
"""

import pytest
from contract import CONTRACT, RecordingSession, problems

from unifi_sentinel.client import UniFiClient
from unifi_sentinel.config import ConfigError, load_config
from unifi_sentinel.snapshot import EventQuery, Needs, collect_snapshot

pytestmark = pytest.mark.live

EVERYTHING = Needs(offline=True, reservations=True, groups=True, health=True, speedtests=True, neighbors=True, firewall=True, wlans=True,
                   events=EventQuery(since_seconds=7 * 86400))


@pytest.fixture(scope="module")
def session():
    try:
        config = load_config()
    except ConfigError as e:
        pytest.skip(f"no controller configured: {e}")
    client = UniFiClient.from_config(config)
    client.workers = 1                                    # one request at a time; the recording is not racy
    recording = RecordingSession(client.session)
    client.session = recording
    version = client.info().get("applicationVersion", "unknown")
    collect_snapshot(client, config.site, EVERYTHING)
    return recording, version


@pytest.mark.parametrize("name", sorted(CONTRACT))
def test_the_controller_returns_the_fields_the_code_reads(session, name):
    recording, version = session
    records = recording.records().get(name)
    if not records:
        pytest.skip(f"this controller returned no {name} records (nothing to check)")
    assert problems(name, records) == [], f"controller version: {version}"


def test_only_reads_were_made(session):
    recording, _version = session
    methods = {(method, path.endswith("/system-log/all")) for method, path, _ in recording.exchanges}
    assert methods <= {("GET", False), ("POST", True)}
    assert ("GET", False) in methods
