"""The JSON Schemas in docs/schemas: valid, complete, versioned, and true to what the commands print.

Each schema is checked against real output from the synthetic fixture over many variants (every client, filters,
mutated controllers). The check uses a strict copy of the schema (``additionalProperties: false`` wherever the schema
does not say otherwise), so a key the code adds without declaring it fails here: a schema can only be incomplete if a
variant is missing, never silently.
"""

import copy
import json
import re
import time

import pytest
from conftest import FakeSession
from docs_support import ROOT
from golden_support import run_command
from jsonschema import Draft202012Validator, ValidationError

from unifi_sentinel import cli, events, history, topology, wan, wifi
from unifi_sentinel import client_view as client_view_module
from unifi_sentinel import firewall as firewall_module
from unifi_sentinel.client import UniFiClient
from unifi_sentinel.diagnose import JSON_VERSION as FINDINGS_VERSION
from unifi_sentinel.diagnose import areas as diagnose_areas
from unifi_sentinel.history import Change as ChangeRecord
from unifi_sentinel.history import (
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
)
from unifi_sentinel.notify import Event, render_payload
from unifi_sentinel.snapshot import Needs, collect_snapshot
from unifi_sentinel.topology import ClientCounts, Node, NodeFinding, Summary, Topology

SCHEMAS = ROOT / "docs" / "schemas"
BASE = "https://raw.githubusercontent.com/jeffholst/unifi-sentinel/main/docs/schemas/"


def load(name):
    return json.loads((SCHEMAS / f"{name}.v1.schema.json").read_text(encoding="utf-8"))


def strict(node):
    """A copy that rejects any property the schema does not declare."""
    if isinstance(node, dict):
        out = {key: strict(value) for key, value in node.items()}
        if "properties" in out and "additionalProperties" not in out and isinstance(out["properties"], dict):
            out["additionalProperties"] = False
        return out
    if isinstance(node, list):
        return [strict(item) for item in node]
    return node


def controller(change=None):
    client = UniFiClient("https://controller", "key")
    client.session = FakeSession()
    if change:
        change(client.session.fx)
    return client


def output(argv, change=None):
    code, out, _ = run_command(controller(change), argv)
    return json.loads(out)


def detach(fx):
    fx["legacy"]["device"][2]["uplink"]["uplink_mac"] = "ee:ee:ee:ee:ee:ee"


def private_wan(fx):
    next(h for h in fx["legacy"]["health"] if h["subsystem"] == "wan")["wan_ip"] = "192.168.1.5"


def no_speedtests(fx):
    fx["legacy_v2"]["speedtest"]["data"] = []


def no_firewall(fx):
    for key in ("firewall-policies", "firewall/zone", "firewall/zone-matrix"):
        fx["legacy_v2"][key] = []


def very_offline(fx):
    fx["legacy"]["alluser"][1]["last_seen"] = int(time.time()) - 40 * 86400


def inventory_snapshot(change=None):
    client = controller(change)
    return capture(collect_snapshot(client, "default", Needs(reservations=True, groups=True)), "1.0")


def churned():
    def change(fx):
        fx["devices"][1]["name"] = "Renamed"
        fx["devices"] = fx["devices"][:3]
        fx["clients"] = fx["clients"][:1]
        fx["legacy"]["alluser"][0]["fixed_ip"] = "10.0.0.88"
    return inventory_snapshot(change)


def cli_variants(*variants):
    return [lambda argv=argv, change=change: output(argv, change) for argv, change in variants]


# name -> functions that each produce one real document
DOCUMENTS = {
    "query-all": cli_variants((["query", "all", "--json"], None), (["query", "--json", "-s", "e"], None)),
    "query-devices": cli_variants((["query", "devices", "--json"], None), (["query", "devices", "-s", "switch", "--json"], None)),
    "query-clients": cli_variants((["query", "clients", "--json"], None), (["query", "clients", "--include-offline", "--json"], None)),
    "query-reservations": cli_variants((["query", "reservations", "--json"], None),
                                       (["query", "reservations", "--offline", "--json"], very_offline)),
    "query-ports": cli_variants((["query", "ports", "--json"], None), (["query", "ports", "--down", "--json"], None)),
    "new-clients": cli_variants((["new-clients", "--json"], None), (["new-clients", "-s", "printer", "--json"], None)),
    "events": cli_variants((["events", "--json"], None), (["events", "--client", "phone", "--json"], None),
                           (["events", "--since", "7d", "--severity", "high", "--json"], None)),
    "events-summary": cli_variants((["events", "--summary", "--json"], None), (["events", "--summary", "--since", "7d", "--json"], None)),
    "topology": cli_variants((["topology", "--json"], None), (["topology", "--clients", "--json"], None),
                             (["topology", "--clients", "--json"], detach)),
    "wifi": cli_variants((["wifi", "--json"], None), (["wifi", "--all", "--json"], None), (["wifi", "--band", "2.4", "--json"], None),
                         (["wifi", "--ap", "Office", "--json"], None), (["wifi", "--ap", "nothing-matches", "--json"], None)),
    "wan": cli_variants((["wan", "--json"], None), (["wan", "--days", "90", "--json"], None), (["wan", "--json"], private_wan),
                        (["wan", "--json"], no_speedtests)),
    "firewall": cli_variants((["firewall", "--json"], None), (["firewall", "--all", "--zones", "--json"], None),
                             (["firewall", "--search", "game", "--json"], None), (["firewall", "--json"], no_firewall)),
    "client": cli_variants(*[(["client", who, "--json", *extra], None) for who in ("desktop", "phone", "old-printer", "old-tablet",
                                                                                   "bb:00:00:00:00:02")
                             for extra in ([], ["--no-events"])]),
    "audit": cli_variants((["audit", "--json"], None), (["audit", "--show-ignored", "--json"], None)),
    "diagnose": cli_variants((["diagnose", "--json"], None), (["diagnose", "--show-ignored", "--json"], None),
                             (["diagnose", "--only", "ports", "--json"], None), (["diagnose", "--no-events", "--json"], None)),
    "snapshot": [lambda: inventory_snapshot()],
    "diff": [lambda: json.loads(history.diff_json(diff_snapshots(inventory_snapshot(), inventory_snapshot()))),
             lambda: json.loads(history.diff_json(diff_snapshots(inventory_snapshot(), churned()))),
             lambda: json.loads(history.diff_json(diff_snapshots(churned(), inventory_snapshot())))],
    "webhook-payload": [lambda: render_payload([Event("new", "warning", "device.offline", "Garage AP", "device is offline")], False),
                        lambda: render_payload([Event("recovered", "warning", "port.errors", "Switch port 2"),
                                                Event("worsened", "critical", "wan.availability", "wan", "down")], True)],
}
CASES = [(name, index) for name, makers in DOCUMENTS.items() for index in range(len(makers))]


# -- the files --------------------------------------------------------------------------------------------------

def test_the_schema_files_are_exactly_the_documented_set():
    on_disk = sorted(p.name for p in SCHEMAS.glob("*.json"))
    assert on_disk == sorted(f"{name}.v1.schema.json" for name in DOCUMENTS)


@pytest.mark.parametrize("name", sorted(DOCUMENTS))
def test_each_schema_is_a_valid_draft_2020_12_schema_with_an_id_a_title_and_a_description(name):
    schema = load(name)
    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == f"{BASE}{name}.v1.schema.json"
    assert schema["title"] and len(schema["description"]) > 20


def test_ids_are_unique_and_nothing_refers_to_a_remote_document():
    ids = [load(name)["$id"] for name in DOCUMENTS]
    assert len(set(ids)) == len(ids)
    for name in DOCUMENTS:
        refs = re.findall(r'"\$ref":\s*"([^"]+)"', json.dumps(load(name)))
        assert all(ref.startswith("#/$defs/") for ref in refs), (name, refs)


# -- the schemas against real output -----------------------------------------------------------------------------

@pytest.mark.parametrize("name, index", CASES, ids=[f"{n}-{i}" for n, i in CASES])
def test_real_output_validates_and_declares_every_key_it_has(name, index):
    document = DOCUMENTS[name][index]()
    schema = load(name)
    Draft202012Validator(schema).validate(document)
    try:
        Draft202012Validator(strict(schema)).validate(document)
    except ValidationError as e:
        pytest.fail(f"{name}: the output has something the schema does not declare: {e.message} at "
                    f"{'/'.join(str(p) for p in e.absolute_path)}")


@pytest.mark.parametrize("name", sorted(DOCUMENTS))
def test_the_variants_really_exercise_the_schema(name):
    """A schema nothing is validated against proves nothing: every one has at least one non-empty document."""
    documents = [make() for make in DOCUMENTS[name]]
    assert any(document for document in documents)


def test_an_undeclared_key_is_caught_by_the_strict_copy():
    document = output(["audit", "--json"])
    document["findings"][0]["surprise"] = 1
    with pytest.raises(ValidationError, match="surprise"):
        Draft202012Validator(strict(load("audit"))).validate(document)
    Draft202012Validator(load("audit")).validate(document)            # the published schema stays open to new fields


@pytest.mark.parametrize("name", sorted(DOCUMENTS))
def test_a_document_that_lost_a_required_key_or_has_the_wrong_type_is_rejected(name):
    document = next(d for d in (make() for make in DOCUMENTS[name]) if d)
    schema = load(name)
    validator = Draft202012Validator(schema)
    target = document[0] if isinstance(document, list) else document
    key = next(iter(schema.get("required") or schema["items"].get("required", [])))
    broken = copy.deepcopy(document)
    (broken[0] if isinstance(broken, list) else broken).pop(key)
    assert not validator.is_valid(broken), f"{name}: dropping '{key}' was accepted"
    assert not validator.is_valid("not a document") and not validator.is_valid(None)
    assert isinstance(target, dict)


# -- versions -----------------------------------------------------------------------------------------------------

VERSIONS = {
    "diagnose": FINDINGS_VERSION, "audit": FINDINGS_VERSION, "firewall": firewall_module.JSON_VERSION,
    "topology": topology.JSON_VERSION, "wifi": wifi.JSON_VERSION, "wan": wan.JSON_VERSION,
    "client": client_view_module.JSON_VERSION, "events-summary": events.JSON_VERSION, "diff": history.JSON_VERSION,
}


@pytest.mark.parametrize("name, version", sorted(VERSIONS.items()))
def test_a_document_carries_the_version_its_schema_and_its_file_name_say(name, version):
    document = DOCUMENTS[name][0]()
    assert list(document)[0] == "version" and document["version"] == version
    assert load(name)["properties"]["version"]["const"] == version and "version" in load(name)["required"]
    assert (SCHEMAS / f"{name}.v{version}.schema.json").exists()


def test_the_snapshot_file_and_the_webhook_payload_are_versioned_too():
    assert load("snapshot")["properties"]["schema_version"]["const"] == history.SCHEMA_VERSION
    assert load("webhook-payload")["properties"]["version"]["const"] == DOCUMENTS["webhook-payload"][0]()["version"]


def test_the_plain_lists_have_no_version_field_and_say_so_in_their_schemas():
    for name in ("query-all", "query-devices", "query-clients", "query-reservations", "query-ports", "new-clients", "events"):
        assert load(name)["type"] == "array" and "version" not in load(name).get("properties", {})


# -- every --json command has a schema ---------------------------------------------------------------------------

COMMAND_OF = {"query": "query-all", "new-clients": "new-clients", "events": "events", "topology": "topology", "wifi": "wifi",
              "wan": "wan", "firewall": "firewall", "client": "client", "audit": "audit", "diagnose": "diagnose",
              "diff": "diff"}


def test_every_command_with_a_json_option_has_a_schema():
    subparsers = next(a for a in cli.build_parser()._actions if a.__class__.__name__ == "_SubParsersAction").choices
    with_json = {name for name, sub in subparsers.items() if any("--json" in a.option_strings for a in sub._actions)}
    assert with_json <= set(COMMAND_OF), f"add a schema for {sorted(with_json - set(COMMAND_OF))}"
    assert all(name in DOCUMENTS for name in COMMAND_OF.values())


# -- the schemas against the typed records -------------------------------------------------------------------------

def keys(typed):
    return set(typed.__required_keys__) | set(typed.__optional_keys__)


def test_the_snapshot_and_diff_schemas_name_exactly_the_keys_of_the_typed_records():
    snapshot = load("snapshot")
    assert set(snapshot["properties"]) == keys(SnapshotRecord)
    assert set(snapshot["properties"]["site"]["properties"]) == keys(SiteRecord)
    assert set(snapshot["properties"]["controller"]["properties"]) == keys(ControllerRecord)
    for name, typed in (("device", DeviceRecord), ("client", ClientRecord), ("reservation", ReservationRecord)):
        assert set(snapshot["$defs"][name]["properties"]) == keys(typed), name
    diff = load("diff")
    assert set(diff["properties"]) - {"version"} == keys(Diff)
    assert set(diff["$defs"]["part"]["properties"]) == keys(DiffPart)
    assert set(diff["$defs"]["change"]["properties"]) == keys(ChangeRecord)
    changed = diff["$defs"]["part"]["properties"]["changed"]["items"]["properties"]
    assert set(changed) == keys(ChangedRecord)


def test_the_topology_schema_names_exactly_the_keys_of_the_typed_tree():
    schema = load("topology")
    assert set(schema["properties"]) - {"version"} == keys(Topology)
    assert set(schema["properties"]["summary"]["properties"]) == keys(Summary)
    node = schema["$defs"]["node"]
    assert set(node["properties"]) == keys(Node) and set(node["required"]) == set(Node.__required_keys__)
    assert set(node["properties"]["clients"]["anyOf"][0]["properties"]) == keys(ClientCounts)
    assert set(node["properties"]["findings"]["items"]["properties"]) == keys(NodeFinding)


def test_the_findings_schemas_cover_the_areas_the_code_has():
    assert load("diagnose")["properties"]["areas"]["items"]["enum"] == list(diagnose_areas.AREA_NAMES)


# -- the documentation ---------------------------------------------------------------------------------------------

def test_the_schemas_page_lists_every_schema_and_how_they_are_versioned():
    page = (ROOT / "docs" / "schemas.md").read_text(encoding="utf-8")
    for name in DOCUMENTS:
        assert f"{name}.v1.schema.json" in page, name
    for phrase in ("never renamed", "`version`", "additional", "bare array"):
        assert phrase in page or phrase.title() in page, phrase
