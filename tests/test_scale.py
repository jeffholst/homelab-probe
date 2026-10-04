"""The analysis grows linearly with the size of the site: it used to be quadratic (per-client rebuilds of
lookups), which made 8,000 clients take seconds per command."""

import time

import pytest
from scale_support import big_site

from homelab_probe.client_view import AddressingIndex, addressing, known_clients
from homelab_probe.diagnose import diagnose
from homelab_probe.export import LocationIndex, client_location
from homelab_probe.history import capture
from homelab_probe.new_clients import report
from homelab_probe.topology import build_topology


def best_of(fn, repeats=3):
    best = None
    for _ in range(repeats):
        start = time.perf_counter()
        fn()
        took = time.perf_counter() - start
        best = took if best is None else min(best, took)
    return best


ANALYSES = {
    "diagnose": lambda snap: diagnose(snap),
    "capture": lambda snap: capture(snap, "1.0"),
    "topology": lambda snap: build_topology(snap, None, True),
    "new-clients": lambda snap: report(snap),
}


@pytest.mark.parametrize("name", sorted(ANALYSES))
def test_four_times_the_clients_costs_about_four_times_the_time(name):
    """Quadratic code costs ~16x for 4x the clients (it was ~15x); linear costs ~4x. The bound is generous."""
    small, large = big_site(2000), big_site(8000)
    t_small = max(best_of(lambda: ANALYSES[name](small)), 0.004)       # a floor, so timer noise cannot flip the ratio
    t_large = best_of(lambda: ANALYSES[name](large))
    assert t_large / t_small < 9, f"{name}: 8000 clients took {t_large:.2f}s, 2000 took {t_small:.3f}s"


@pytest.mark.parametrize("name", sorted(ANALYSES))
def test_a_large_site_is_fast_in_absolute_terms(name):
    assert best_of(lambda: ANALYSES[name](big_site(8000)), repeats=2) < 2.0


def test_the_indexed_forms_give_the_same_answers_as_the_one_off_forms():
    snap = big_site(300)
    locate = LocationIndex(snap).of
    assert all(locate(c) == client_location(snap, c) for c in snap.clients)
    index = AddressingIndex(snap)
    records = known_clients(snap)
    assert len(records) > 300
    for rec in records[::7]:
        assert addressing(snap, rec, index) == addressing(snap, rec)


def test_the_generated_site_looks_like_a_site():
    snap = big_site(60)
    assert len(snap.clients) == 60 and len(snap.legacy_clients) == 60 and len(snap.all_users) == 72
    assert sum(c["type"] == "WIRED" for c in snap.clients) == 20
    assert len({c["macAddress"] for c in snap.clients}) == 60 and len({c["ipAddress"] for c in snap.clients}) == 60
