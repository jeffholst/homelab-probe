"""The named record shapes match what the code builds, so a TypedDict cannot drift from its data."""

from typing import get_type_hints

import pytest

from unifi_sentinel.client_view import DeviceIndex
from unifi_sentinel.history import Change, ChangedRecord, Diff, DiffPart, capture, diff_snapshots
from unifi_sentinel.snapshot import Needs, collect_snapshot
from unifi_sentinel.wifi import Radio, radios


def keys(typed):
    return set(get_type_hints(typed))


def test_every_radio_row_has_exactly_the_keys_of_radio(fake_client):
    snap = collect_snapshot(fake_client, "default")
    rows = radios(snap, DeviceIndex(snap))
    assert rows and all(set(row) == keys(Radio) for row in rows)
    assert any(row["band"] == "" for row in rows)             # the offline AP's placeholder row has them too


def test_a_diff_has_exactly_the_keys_of_diff_and_its_parts(fake_client):
    snap = collect_snapshot(fake_client, "default", Needs(reservations=True, groups=True))
    old = capture(snap, "1.0")
    snap.devices[0] = {**snap.devices[0], "name": "Renamed"}
    snap.clients = snap.clients[:1]
    result = diff_snapshots(old, capture(snap, "2.0"))
    assert set(result) == keys(Diff)
    for part in ("devices", "clients", "reservations"):
        assert set(result[part]) == keys(DiffPart)
    assert result["controller"] and set(result["controller"][0]) == keys(Change)
    changed = [c for part in ("devices", "clients") for c in result[part]["changed"]]
    assert changed and all(set(c) == keys(ChangedRecord) for c in changed)
    assert all(set(c) == keys(Change) for entry in changed for c in entry["changes"])


def test_the_total_counts_every_difference(fake_client):
    snap = collect_snapshot(fake_client, "default", Needs(reservations=True, groups=True))
    old = capture(snap, "1.0")
    snap.clients = snap.clients[:1]
    result = diff_snapshots(old, capture(snap, "2.0"))
    counted = len(result["controller"]) + sum(
        len(p["added"]) + len(p["removed"]) + sum(len(c["changes"]) for c in p["changed"])
        for p in (result["devices"], result["clients"], result["reservations"]))
    assert result["total"] == counted and counted > 0


def test_the_ci_type_check_blocks_and_mypy_is_strict_about_untyped_functions():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    workflow = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    types_job = workflow.split("  types:", 1)[1]
    assert "continue-on-error" not in types_job and "uv run mypy" in types_job
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    for option in ("check_untyped_defs = true", "warn_unused_ignores = true", "warn_unreachable = true"):
        assert option in pyproject


@pytest.mark.parametrize("name", ["Radio", "Diff", "DiffPart", "Change", "ChangedRecord"])
def test_the_records_are_typed_dicts(name):
    import unifi_sentinel.history as history
    import unifi_sentinel.wifi as wifi
    cls = getattr(history, name, None) or getattr(wifi, name)
    assert hasattr(cls, "__required_keys__") and cls.__required_keys__ == frozenset(get_type_hints(cls))
