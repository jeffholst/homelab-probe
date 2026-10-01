"""Reading the controller in parallel: same result as one by one, in the same order, degrading the same way."""

import dataclasses
import random
import re
import threading
import time
import warnings

import pytest
import requests
import urllib3

from unifi_sentinel import cli
from unifi_sentinel.client import UniFiAPIError, UniFiClient
from unifi_sentinel.config import DEFAULT_PARALLEL, MAX_PARALLEL, Config, ConfigError, load_config, parse_parallel
from unifi_sentinel.snapshot import EventQuery, Needs, collect_snapshot, extend_snapshot

FULL = Needs(reservations=True, groups=True, health=True, speedtests=True, neighbors=True, events=EventQuery())


def same(a, b):
    """Two snapshots hold the same data (the fake stamps events and speedtests when it is created, so compare their ids)."""
    for field in dataclasses.fields(a):
        x, y = getattr(a, field.name), getattr(b, field.name)
        if field.name in ("events", "speedtests"):          # their times are made when each fake is created
            x, y = [e["id"] for e in x], [e["id"] for e in y]
        assert x == y, field.name


def other_client(workers):
    from conftest import FakeSession
    client = UniFiClient("https://controller", "key", workers=workers)
    client.session = FakeSession()
    client._sleep = lambda seconds: None
    return client


class Slow:
    """Wraps a fake session: each request takes a random short time and the number in flight is recorded."""

    def __init__(self, session, seed=1, pause=0.02):
        self.session, self.pause, self.rng = session, pause, random.Random(seed)
        self.lock, self.now, self.peak = threading.Lock(), 0, 0
        self.headers = session.headers

    def _around(self, call):
        with self.lock:
            self.now += 1
            self.peak = max(self.peak, self.now)
            delay = self.rng.random() * self.pause
        try:
            time.sleep(delay)
            return call()
        finally:
            with self.lock:
                self.now -= 1

    def get(self, *args, **kwargs):
        return self._around(lambda: self.session.get(*args, **kwargs))

    def post(self, *args, **kwargs):
        return self._around(lambda: self.session.post(*args, **kwargs))

    def __getattr__(self, name):
        return getattr(self.session, name)


# -- same result, same order --------------------------------------------------------------------------------

@pytest.mark.parametrize("needs", [Needs(), Needs(offline=True), Needs(reservations=True, groups=True), FULL])
def test_parallel_and_sequential_snapshots_are_identical(needs):
    one, many = other_client(1), other_client(4)
    same(collect_snapshot(one, "default", needs), collect_snapshot(many, "default", needs))


def test_results_keep_the_order_of_the_controller_whatever_finishes_first():
    client = other_client(8)
    client.session = Slow(client.session, seed=7)
    snap = collect_snapshot(client, "default", FULL)
    assert [d["id"] for d in snap.devices] == ["gw1", "sw1", "ap1", "ap2"]
    assert list(snap.device_details) == ["gw1", "sw1", "ap1", "ap2"]       # insertion order follows the devices
    assert list(snap.device_stats) == ["gw1", "sw1", "ap1"]
    assert [c["id"] for c in snap.clients] == ["c1", "c2"]


def test_requests_really_overlap_with_workers_and_not_without():
    parallel, serial = other_client(6), other_client(1)
    slow_parallel, slow_serial = Slow(parallel.session), Slow(serial.session)
    parallel.session, serial.session = slow_parallel, slow_serial
    collect_snapshot(parallel, "default", FULL)
    collect_snapshot(serial, "default", FULL)
    assert slow_parallel.peak > 1 and slow_parallel.peak <= 6
    assert slow_serial.peak == 1


def test_the_same_requests_are_made_either_way():
    one, many = other_client(1), other_client(5)
    collect_snapshot(one, "default", FULL)
    collect_snapshot(many, "default", FULL)
    assert sorted(one.session.calls) == sorted(many.session.calls)
    assert sorted(p for p, _ in one.session.posts) == sorted(p for p, _ in many.session.posts)
    assert one.attempts_made == many.attempts_made == len(many.session.calls) + len(many.session.posts)


def test_the_guards_hold_in_parallel(fake_client):
    """Only GETs, and the one approved event-log POST, whatever the number of workers."""
    client = other_client(6)
    collect_snapshot(client, "default", FULL)
    assert {path for path, _ in client.session.posts} == {"/proxy/network/v2/api/site/default/system-log/all"}
    assert all(call.startswith("/proxy/network") for call in client.session.calls)


# -- degrading ----------------------------------------------------------------------------------------------

def break_reads(session, *kinds):
    """Make the named legacy reads of a (fake) session fail with a 500."""
    original = session.get

    def get(url, params=None, verify=True, timeout=None):
        if any(url.endswith(kind) for kind in kinds):
            return type("R", (), {"status_code": 500, "ok": False, "text": "boom", "json": lambda self: {}})()
        return original(url, params=params, verify=verify, timeout=timeout)

    session.get = get


def test_warnings_come_in_a_fixed_order_in_both_modes(capsys):
    shown = {}
    for workers in (1, 6):
        client = other_client(workers)
        client.session = Slow(client.session, seed=workers)
        break_reads(client.session.session, "/stat/alluser", "/rest/networkconf", "/stat/health", "/speedtest")
        snap = collect_snapshot(client, "default", FULL)
        shown[workers] = [line.split(" unavailable")[0] for line in capsys.readouterr().err.splitlines()]
        assert snap.all_users == [] and snap.networks == [] and snap.health == [] and snap.speedtests == []
    assert shown[1] == shown[6]
    assert [line for line in shown[6] if "detail/statistics" not in line] == [
        "Warning: legacy stat/alluser", "Warning: legacy rest/networkconf", "Warning: legacy stat/health",
        "Warning: speedtest history"]


def test_a_device_without_statistics_degrades_the_same_way_in_parallel(capsys):
    one, many = other_client(1), other_client(4)
    a, b = collect_snapshot(one, "default"), collect_snapshot(many, "default")
    assert set(a.device_details) == set(b.device_details) == {"gw1", "sw1", "ap1", "ap2"}
    assert set(a.device_stats) == set(b.device_stats) == {"gw1", "sw1", "ap1"}
    assert capsys.readouterr().err.count("detail/statistics unavailable for 1 device(s)") == 2


def test_a_device_without_detail_is_not_asked_for_statistics(capsys):
    for workers in (1, 4):
        client = other_client(workers)
        del client.session.fx["device_detail"]["ap1"]
        snap = collect_snapshot(client, "default")
        assert "ap1" not in snap.device_details and "ap1" not in snap.device_stats
        assert not any(call.endswith("/devices/ap1/statistics/latest") for call in client.session.calls)
    assert capsys.readouterr().err.count("detail/statistics unavailable for 2 device(s)") == 2


def test_a_required_read_failing_raises_in_parallel_too():
    for workers in (1, 4):
        client = other_client(workers)
        client.session.status = 500
        with pytest.raises(UniFiAPIError):
            collect_snapshot(client, "default", Needs(groups=True, users_required=True))


def test_a_required_client_history_fails_in_parallel():
    client = other_client(4)
    break_reads(client.session, "/stat/alluser")
    with pytest.raises(UniFiAPIError, match="HTTP 500"):
        collect_snapshot(client, "default", Needs(groups=True, users_required=True))


# -- the client under threads ---------------------------------------------------------------------------------

def test_the_trace_and_counters_survive_many_threads():
    client = other_client(8)
    lines = []
    client.trace = lines.append
    for _ in range(5):
        collect_snapshot(client, "default", FULL)
    total = len(client.session.calls) + len(client.session.posts)
    assert client.attempts_made == total == len([line for line in lines if " -> " in line])
    assert all(re.fullmatch(r"(GET|POST) \S.* -> \d{3} \(\d+ ms\)", line) or line.startswith("read ") for line in lines)


def test_workers_one_means_no_pool_and_more_means_a_pool():
    one, many = other_client(1), other_client(3)
    with one.parallel() as pool:
        assert pool is None
    with many.parallel() as pool:
        assert pool is not None and many._in_parallel is True
    assert many._in_parallel is False


def test_a_real_session_gets_a_connection_pool_sized_for_the_workers():
    client = UniFiClient("https://controller.example", "k", workers=7)
    with client.parallel():
        adapter = client.session.get_adapter("https://controller.example/x")
        assert adapter._pool_maxsize == 7
    assert UniFiClient("https://controller.example", "k", workers=0).workers == 1


def test_the_insecure_warning_is_hidden_inside_a_parallel_section_only():
    client = UniFiClient("https://controller.example", "k", verify_ssl=False, workers=4)
    before = list(warnings.filters)
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        with client.parallel():
            warnings.warn("x", urllib3.exceptions.InsecureRequestWarning, stacklevel=1)
        warnings.warn("y", urllib3.exceptions.InsecureRequestWarning, stacklevel=1)
    assert [str(w.message) for w in seen] == ["y"]
    assert list(warnings.filters) == before


def test_a_fake_session_is_left_alone():
    client = other_client(4)
    session = client.session
    with client.parallel():
        assert client.session is session and not isinstance(client.session, requests.Session)


# -- extend_snapshot and the two-phase client lookup --------------------------------------------------------------

def test_extending_reads_only_the_extras_and_matches_a_single_collection():
    one, two = other_client(1), other_client(3)
    wanted = Needs(reservations=True, groups=True, events=EventQuery())
    whole = collect_snapshot(one, "default", wanted)
    partial = collect_snapshot(two, "default", Needs(offline=True))
    before = len(two.session.calls)
    extend_snapshot(two, partial, wanted)
    extra = two.session.calls[before:]
    assert sorted(p.rsplit("/", 1)[-1] for p in extra) == ["network-members-groups", "networkconf"]
    assert len(two.session.posts) == 1
    same(whole, partial)


def test_extending_with_nothing_asked_reads_nothing():
    client = other_client(1)
    snap = collect_snapshot(client, "default", Needs(offline=True))
    calls = len(client.session.calls)
    extend_snapshot(client, snap, Needs(offline=True))
    assert len(client.session.calls) == calls and not client.session.posts


def test_extending_degrades_with_a_warning(capsys):
    client = other_client(2)
    snap = collect_snapshot(client, "default", Needs(offline=True))
    break_reads(client.session, "/rest/networkconf")
    extend_snapshot(client, snap, Needs(reservations=True))
    assert snap.networks == [] and "legacy rest/networkconf unavailable" in capsys.readouterr().err


# -- configuration ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [(None, DEFAULT_PARALLEL), ("", DEFAULT_PARALLEL), (" 3 ", 3), ("1", 1),
                                            (str(MAX_PARALLEL), MAX_PARALLEL)])
def test_parallel_values(text, expected):
    assert parse_parallel(text) == expected


@pytest.mark.parametrize("text", ["0", "-2", str(MAX_PARALLEL + 1), "many", "2.5"])
def test_bad_parallel_values_are_config_errors(text):
    with pytest.raises(ConfigError, match="PARALLEL_REQUESTS must be"):
        parse_parallel(text)


def test_the_setting_reaches_the_client(monkeypatch):
    monkeypatch.setenv("CONTROLLER_URL", "https://controller.example")
    monkeypatch.setenv("API_KEY", "key")
    assert load_config().parallel == DEFAULT_PARALLEL
    monkeypatch.setenv("PARALLEL_REQUESTS", "4")
    config = load_config()
    assert config.parallel == 4 and UniFiClient.from_config(config).workers == 4
    assert UniFiClient.from_config(Config("https://c", "k", parallel=1)).workers == 1
    assert UniFiClient("https://c", "k").workers == 1                         # direct construction stays sequential


def test_the_option_beats_the_environment_and_a_bad_one_is_a_usage_error(fake_client, monkeypatch, capsys):
    seen = []

    def from_config(cls, config):
        seen.append(config.parallel)
        return fake_client

    monkeypatch.setenv("CONTROLLER_URL", "https://controller.example")
    monkeypatch.setenv("API_KEY", "key")
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(from_config))
    assert cli.main(["info"]) == 0 and seen[-1] == DEFAULT_PARALLEL
    monkeypatch.setenv("PARALLEL_REQUESTS", "3")
    assert cli.main(["info"]) == 0 and seen[-1] == 3
    assert cli.main(["--parallel", "9", "info"]) == 0 and seen[-1] == 9
    capsys.readouterr()
    with pytest.raises(SystemExit) as caught:
        cli.main(["--parallel", "0", "info"])
    assert caught.value.code == cli.EXIT_USAGE and "PARALLEL_REQUESTS must be" in capsys.readouterr().err
    assert cli.main(["--verbose", "--parallel", "5", "info"]) == 0
    assert "up to 5 requests at once" in capsys.readouterr().err


def test_extending_reports_what_the_snapshot_now_holds_when_tracing():
    from unifi_sentinel.snapshot import describe_snapshot
    client = other_client(1)
    snap = collect_snapshot(client, "default", Needs(offline=True))
    lines = []
    client.trace = lines.append
    extend_snapshot(client, snap, Needs(reservations=True))
    assert lines[-1] == describe_snapshot(snap) and "networks" in lines[-1]
