"""The named record shapes match what the code builds, so a TypedDict cannot drift from its data."""

from typing import get_type_hints

import pytest

from homelab_probe.client_view import DeviceIndex
from homelab_probe.history import (
    Change,
    ChangedRecord,
    ClientRecord,
    ControllerRecord,
    DeviceRecord,
    Diff,
    DiffPart,
    ReservationRecord,
    SiteRecord,
    SnapshotRecord,
    capture,
    diff_snapshots,
    load_snapshot,
    save_snapshot,
)
from homelab_probe.snapshot import Needs, collect_snapshot
from homelab_probe.topology import ClientCounts, Node, NodeFinding, Summary, Topology, WiredClient, build_topology
from homelab_probe.wifi import Neighbor, Radio, radios, unique_neighbors


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


def required(typed):
    return set(typed.__required_keys__)


def optional(typed):
    return set(typed.__optional_keys__)


# -- the saved snapshot ---------------------------------------------------------------------------------------

def test_a_capture_has_exactly_the_keys_of_the_snapshot_record_and_its_parts(fake_client):
    snap = collect_snapshot(fake_client, "default", Needs(reservations=True, groups=True))
    record = capture(snap, "1.0")
    assert set(record) == keys(SnapshotRecord) == required(SnapshotRecord)
    assert set(record["site"]) == keys(SiteRecord) and set(record["controller"]) == keys(ControllerRecord)
    for name, typed in (("devices", DeviceRecord), ("clients", ClientRecord), ("reservations", ReservationRecord)):
        assert record[name] and all(set(item) == keys(typed) for item in record[name]), name


def test_the_client_record_values_have_the_declared_kinds(fake_client):
    record = capture(collect_snapshot(fake_client, "default", Needs(reservations=True, groups=True)), "1.0")
    assert all(isinstance(c["groups"], list) and isinstance(c["vlan"], (int, str)) for c in record["clients"])
    assert any(isinstance(c["vlan"], int) for c in record["clients"]) and any(c["vlan"] == "" for c in record["clients"])
    assert all(isinstance(value, str) for item in record["devices"] for value in item.values())


def test_a_saved_and_loaded_snapshot_keeps_exactly_those_keys(fake_client, tmp_path):
    record = capture(collect_snapshot(fake_client, "default", Needs(reservations=True, groups=True)), "1.0")
    loaded = load_snapshot(save_snapshot(record, tmp_path / "s.json"))
    assert loaded == record and set(loaded) == keys(SnapshotRecord)


def test_the_added_and_removed_records_of_a_diff_have_the_keys_of_their_kind(fake_client):
    snap = collect_snapshot(fake_client, "default", Needs(reservations=True, groups=True))
    full, trimmed = capture(snap, "1.0"), capture(snap, "1.0")
    trimmed["devices"], trimmed["clients"], trimmed["reservations"] = [], [], []
    result = diff_snapshots(trimmed, full)
    for name, typed in (("devices", DeviceRecord), ("clients", ClientRecord), ("reservations", ReservationRecord)):
        assert result[name]["added"] and all(set(item) == keys(typed) for item in result[name]["added"])
        assert diff_snapshots(full, trimmed)[name]["removed"]


# -- the topology ------------------------------------------------------------------------------------------------

def every_node(topology):
    def walk(nodes):
        for node in nodes:
            yield node
            yield from walk(node["children"])
    return list(walk(topology["roots"])) + topology["unattached"]


def test_a_topology_has_exactly_the_keys_of_topology_summary_and_nodes(fake_client):
    tree = build_topology(collect_snapshot(fake_client, "default"))
    assert set(tree) == keys(Topology) and set(tree["summary"]) == keys(Summary)
    nodes = every_node(tree)
    assert len(nodes) == 4
    for node in nodes:
        assert required(Node) <= set(node) and set(node) <= keys(Node)
        assert "wired_clients" not in node                              # only with --clients
        assert all(set(f) == keys(NodeFinding) for f in node["findings"])
        assert node["clients"] is None or set(node["clients"]) == keys(ClientCounts)
    assert any(node["findings"] for node in nodes) and any(node["clients"] for node in nodes)


def test_with_clients_a_node_also_has_the_wired_client_list(fake_client):
    tree = build_topology(collect_snapshot(fake_client, "default"), with_clients=True)
    nodes = every_node(tree)
    assert all(set(node) == required(Node) | {"wired_clients"} for node in nodes)
    listed = [c for node in nodes for c in node["wired_clients"]]
    assert listed and all(set(c) == keys(WiredClient) for c in listed)


def test_an_unattached_device_also_has_a_reason(fake_client):
    fx = fake_client.session.fx
    fx["legacy"]["device"][2]["uplink"]["uplink_mac"] = "ee:ee:ee:ee:ee:ee"      # an uplink to nothing we know
    tree = build_topology(collect_snapshot(fake_client, "default"))
    assert tree["unattached"] and all(set(node) == required(Node) | {"reason"} for node in tree["unattached"])
    assert all("reason" not in node for node in tree["roots"])


def test_node_keys_are_optional_only_for_the_two_that_exist_sometimes():
    assert optional(Node) == {"wired_clients", "reason"} and "name" in required(Node) and "children" in required(Node)


# -- the wifi neighbors ----------------------------------------------------------------------------------------------

def test_every_neighbor_has_exactly_the_keys_of_neighbor(fake_client):
    snap = collect_snapshot(fake_client, "default", Needs(neighbors=True))
    found = unique_neighbors(snap)
    assert found and all(set(n) == keys(Neighbor) for n in found)
    assert any(n["open"] for n in found) and any(not n["open"] for n in found)


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
    import homelab_probe.history as history
    import homelab_probe.wifi as wifi
    cls = getattr(history, name, None) or getattr(wifi, name)
    assert hasattr(cls, "__required_keys__") and cls.__required_keys__ == frozenset(get_type_hints(cls))
