"""The server's response cache: single-flight, stale on error, error coalescing and a cap (issue #183, PR 2)."""

import threading
import time

import pytest

from homelab_probe import logs
from homelab_probe.client import UniFiAPIError
from homelab_probe.server.cache import ResponseCache, track_ages


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def cache(clock):
    return ResponseCache(ttl=30, stale_ttl=600, error_ttl=5, max_in_flight=3, clock=clock, wall=lambda: 1_800_000_000)


class Source:
    """A controller stand-in: counts reads, can fail."""

    def __init__(self, values=None):
        self.reads = 0
        self.fail = None
        self.values = values or [{"n": 1}, {"n": 2}, {"n": 3}]

    def __call__(self):
        self.reads += 1
        if self.fail:
            raise self.fail
        return self.values[min(self.reads, len(self.values)) - 1]


def error(kind="connection"):
    return UniFiAPIError("could not reach it", kind=kind)


# -- freshness -------------------------------------------------------------------------------------------------

def test_an_answer_is_read_once_while_it_is_fresh_and_again_after_the_ttl(cache, clock):
    source = Source()
    assert cache.fetch("k", source) == {"n": 1} and cache.fetch("k", source) == {"n": 1} and source.reads == 1
    clock.now += 29.9
    assert cache.fetch("k", source) == {"n": 1} and source.reads == 1
    clock.now += 0.2
    assert cache.fetch("k", source) == {"n": 2} and source.reads == 2


def test_keys_are_independent(cache):
    a, b = Source([1]), Source([2])
    assert (cache.fetch("a", a), cache.fetch("b", b), cache.fetch("a", a)) == (1, 2, 1) and (a.reads, b.reads) == (1, 1)


def test_clearing_forces_a_new_read_for_every_key(cache):
    source = Source()
    cache.fetch("k", source)
    cache.clear()
    assert cache.fetch("k", source) == {"n": 2} and source.reads == 2


def test_a_read_started_before_clear_cannot_repopulate_the_cache(cache):
    started, release, results = threading.Event(), threading.Event(), []

    def slow():
        started.set()
        release.wait(5)
        return {"n": 1}

    thread = threading.Thread(target=lambda: results.append(cache.fetch("k", slow)))
    thread.start()
    assert started.wait(5)
    cache.clear()
    release.set()
    thread.join(5)
    assert not thread.is_alive() and results == [{"n": 1}]
    assert cache.fetch("k", lambda: {"n": 2}) == {"n": 2}


def test_cache_entries_and_errors_are_bounded_and_expired_entries_are_pruned(clock):
    bounded = ResponseCache(ttl=30, stale_ttl=10, error_ttl=5, clock=clock, max_entries=2)
    for key in ("a", "b", "c"):
        bounded.fetch(key, lambda value=key: value)
    assert len(bounded._entries) == 2 and list(bounded._entries) == ["b", "c"]
    for key in ("x", "y", "z"):
        with pytest.raises(UniFiAPIError):
            bounded.fetch(key, lambda: (_ for _ in ()).throw(error()))
    assert len(bounded._errors) == 2 and list(bounded._errors) == ["y", "z"]
    clock.now += 11
    bounded.fetch("new", lambda: "new")
    assert list(bounded._entries) == ["new"] and bounded._errors == {}


def test_nobody_can_change_what_another_request_will_see(cache):
    first = cache.fetch("k", lambda: {"list": [1, 2]})
    first["list"].append(99)                                 # the caller who read it
    second = cache.fetch("k", lambda: pytest.fail("fresh"))
    second["list"].append(100)                               # a caller who got a hit
    assert cache.fetch("k", lambda: pytest.fail("fresh")) == {"list": [1, 2]}


# -- single flight and the cap ---------------------------------------------------------------------------------

def test_many_requests_at_once_cause_one_read(cache):
    gate, started, results = threading.Event(), threading.Event(), []
    reads = []

    def slow():
        reads.append(1)
        started.set()
        gate.wait(5)
        return {"slow": True}

    def ask():
        results.append(cache.fetch("k", slow))

    threads = [threading.Thread(target=ask) for _ in range(20)]
    for t in threads:
        t.start()
    started.wait(5)
    time.sleep(0.05)                                         # let the others reach the lock
    gate.set()
    for t in threads:
        t.join(5)
    assert len(reads) == 1 and results == [{"slow": True}] * 20 and cache._key_locks == {}


def test_at_most_max_in_flight_reads_are_on_the_wire_at_once(cache):
    active, peak, lock, gate = 0, 0, threading.Lock(), threading.Event()

    def read():
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        gate.wait(2)
        with lock:
            active -= 1
        return 1

    threads = [threading.Thread(target=cache.fetch, args=(f"key{i}", read)) for i in range(10)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    seen = peak
    gate.set()
    for t in threads:
        t.join(5)
    assert seen == 3 and peak == 3                           # max_in_flight of the fixture


# -- failure ---------------------------------------------------------------------------------------------------

def test_a_failed_read_with_an_older_answer_serves_that_with_a_warning(cache, clock):
    source = Source()
    cache.fetch("k", source)
    clock.now += 100
    source.fail = error("timeout")
    with logs.collect_warnings(quiet=True) as warnings:
        assert cache.fetch("k", source) == {"n": 1}
    assert len(warnings) == 1 and "timeout" in warnings[0] and "served from the cache as of" in warnings[0]


def test_failures_that_serve_stale_data_are_coalesced(cache, clock):
    source = Source()
    cache.fetch("k", source)
    clock.now += 100
    source.fail = error("timeout")
    for _ in range(3):
        with logs.collect_warnings(quiet=True) as warnings:
            assert cache.fetch("k", source) == {"n": 1}
        assert len(warnings) == 1 and "timeout" in warnings[0]
    assert source.reads == 2


def test_an_answer_older_than_the_stale_limit_is_not_served(cache, clock):
    source = Source()
    cache.fetch("k", source)
    clock.now += 601
    source.fail = error()
    with pytest.raises(UniFiAPIError):
        cache.fetch("k", source)


def test_a_failure_without_any_answer_is_raised(cache):
    source = Source()
    source.fail = error("tls")
    with pytest.raises(UniFiAPIError) as caught:
        cache.fetch("k", source)
    assert caught.value.kind == "tls"


def test_a_failure_is_remembered_briefly_so_a_burst_does_not_hammer_the_controller(cache, clock):
    source = Source()
    source.fail = error()
    for _ in range(5):
        with pytest.raises(UniFiAPIError):
            cache.fetch("k", source)
    assert source.reads == 1                                 # the other four were refused from memory
    clock.now += 5.1
    source.fail = None
    assert cache.fetch("k", source) == {"n": 2} and source.reads == 2
    clock.now += 31
    source.fail = error()
    with logs.collect_warnings(quiet=True):
        assert cache.fetch("k", source) == {"n": 2}          # and now the stale answer is there


def test_only_the_api_errors_of_the_client_are_swallowed(cache):
    def broken():
        raise ValueError("a bug")

    with pytest.raises(ValueError):
        cache.fetch("k", broken)


def test_a_miss_returns_a_copy_without_changing_reader_owned_data(cache):
    original = {"list": [1, 2]}
    result = cache.fetch("k", lambda: original)
    result["list"].append(3)
    assert original == {"list": [1, 2]}
    assert cache.fetch("k", lambda: pytest.fail("fresh")) == {"list": [1, 2]}


def test_a_success_forgets_the_remembered_failure(cache, clock):
    source = Source()
    source.fail = error()
    with pytest.raises(UniFiAPIError):
        cache.fetch("k", source)
    clock.now += 6
    source.fail = None
    cache.fetch("k", source)
    clock.now += 31
    source.fail = error()
    with logs.collect_warnings(quiet=True):
        cache.fetch("k", source)                             # stale, not the old failure


# -- when the data was read ------------------------------------------------------------------------------------

def test_the_times_of_the_answers_used_are_collected_per_block(cache):
    with track_ages() as ages:
        cache.fetch("a", lambda: 1)
        cache.fetch("a", lambda: 1)
    assert ages == [1_800_000_000, 1_800_000_000]
    cache.fetch("a", lambda: 1)                              # outside a block nothing is collected, nothing breaks


def test_the_cache_logs_each_read_at_debug_without_values(cache):
    import io
    import json

    stream = io.StringIO()
    logs.configure("json", "DEBUG", stream=stream)
    cache.fetch("k", lambda: {"secret": "value"}, "GET /x")
    cache.fetch("k", lambda: 1, "GET /x")
    records = [json.loads(line) for line in stream.getvalue().splitlines() if "server.cache" in line]
    assert [r["outcome"] for r in records] == ["miss", "hit"] and all(r["label"] == "GET /x" for r in records)
    assert "value" not in stream.getvalue()


def test_a_failure_that_arrives_after_a_refresh_is_raised_but_not_remembered(cache):
    """A manual refresh (`clear`) while a read is in flight makes that read's outcome obsolete: its failure must not
    be remembered, or the next request would be refused for 5 s on account of a read the refresh had replaced."""
    reads = []

    def failing_during_a_refresh():
        reads.append(1)
        cache.clear()
        raise error("timeout")

    with pytest.raises(UniFiAPIError):
        cache.fetch("k", failing_during_a_refresh)
    assert cache.fetch("k", lambda: {"fresh": True}) == {"fresh": True}        # not refused: nothing was remembered
    assert reads == [1]
