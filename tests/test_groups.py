"""Findings that share a cause: a device offline behind an offline uplink (issue #230)."""

import json

from homelab_probe.documents import diagnose_document
from homelab_probe.groups import KIND, LIMITATIONS, group_findings
from homelab_probe.snapshot import Snapshot, collect_snapshot
from homelab_probe.topology import device_links
from homelab_probe.triage import finding_id

GW, SW, AP, CAM = "AA:00:00:00:00:01", "AA:00:00:00:00:02", "AA:00:00:00:00:03", "AA:00:00:00:00:04"


def offline(mac, name):
    return {"severity": "critical", "code": "device.offline", "subject": name, "message": "device is offline", "mac": mac}


def link(mac, name, parent="", online=False, port=None):
    return {"name": name, "mac": mac, "online": online, "parent": parent, "parent_port": port, "port": None}


def links(*items):
    return {item["mac"]: item for item in items}


def ident(mac, name):
    return finding_id("device.offline", name, mac)


# -- the claim and its evidence -----------------------------------------------------------------------------------

def test_a_device_whose_uplink_parent_is_offline_is_grouped_under_it_with_the_chain_as_evidence():
    found = [offline(SW, "Switch"), offline(AP, "AP")]
    groups = group_findings(found, links(link(GW, "Gateway", online=True), link(SW, "Switch", GW, port=2),
                                         link(AP, "AP", SW, port=5)))
    assert set(groups) == {ident(AP, "AP")}
    group = groups[ident(AP, "AP")]
    assert group["kind"] == KIND == "offline_behind_offline_uplink" and group["cause"] == ident(SW, "Switch")
    assert group["cause_code"] == "device.offline" and "Switch" in group["summary"] and "probably" in group["summary"].lower()
    assert group["evidence"]["source"] and group["limitations"] == LIMITATIONS
    assert group["evidence"]["chain"] == [
        {"name": "AP", "mac": AP, "offline": True, "finding": ident(AP, "AP"), "uplink_port": 5},
        {"name": "Switch", "mac": SW, "offline": True, "finding": ident(SW, "Switch"), "uplink_port": None}]


def test_the_limitation_says_that_an_offline_device_keeps_its_last_known_uplink():
    assert any("keeps its last known uplink" in text for text in LIMITATIONS)
    assert any("probable cause, not a confirmed one" in text for text in LIMITATIONS)


def test_the_cause_is_the_root_most_offline_ancestor_and_every_device_below_it_points_at_it():
    found = [offline(GW, "Gateway"), offline(SW, "Switch"), offline(AP, "AP")]
    groups = group_findings(found, links(link(GW, "Gateway"), link(SW, "Switch", GW, port=1),
                                         link(AP, "AP", SW, port=3)))
    assert set(groups) == {ident(SW, "Switch"), ident(AP, "AP")}
    assert {g["cause"] for g in groups.values()} == {ident(GW, "Gateway")}      # never the nearer switch
    assert [h["name"] for h in groups[ident(AP, "AP")]["evidence"]["chain"]] == ["AP", "Switch", "Gateway"]
    assert "through it" in groups[ident(AP, "AP")]["summary"]


def test_a_device_with_no_known_uplink_is_not_grouped():
    assert group_findings([offline(AP, "AP"), offline(SW, "Switch")], links(link(AP, "AP"), link(SW, "Switch"))) == {}


def test_a_device_whose_parent_is_online_is_not_grouped():
    found = [offline(AP, "AP")]
    assert group_findings(found, links(link(SW, "Switch", online=True), link(AP, "AP", SW))) == {}


def test_a_parent_whose_state_is_not_known_is_not_taken_for_offline():
    unknown = {"name": "Switch", "mac": SW, "parent": GW}                     # no "online" key at all
    assert group_findings([offline(AP, "AP"), offline(SW, "Switch")], links(unknown, link(AP, "AP", SW))) == {}


def test_an_offline_parent_without_a_finding_is_not_a_cause():
    # an ignore rule hid the switch's finding: there is nothing to point at
    assert group_findings([offline(AP, "AP")], links(link(SW, "Switch"), link(AP, "AP", SW))) == {}


def test_a_gap_of_online_devices_stops_the_walk_so_a_farther_offline_device_is_not_the_cause():
    found = [offline(GW, "Gateway"), offline(AP, "AP")]
    assert group_findings(found, links(link(GW, "Gateway"), link(SW, "Switch", GW, online=True),
                                       link(AP, "AP", SW))) == {}


def test_an_offline_device_between_without_a_finding_is_shown_in_the_chain_without_an_id():
    found = [offline(GW, "Gateway"), offline(AP, "AP")]
    group = group_findings(found, links(link(GW, "Gateway"), link(SW, "Switch", GW), link(AP, "AP", SW)))[ident(AP, "AP")]
    assert group["cause"] == ident(GW, "Gateway")
    assert [(h["name"], h["finding"]) for h in group["evidence"]["chain"]] == [
        ("AP", ident(AP, "AP")), ("Switch", None), ("Gateway", ident(GW, "Gateway"))]


def test_an_uplink_to_a_device_the_controller_does_not_list_is_not_grouped():
    assert group_findings([offline(AP, "AP")], links(link(AP, "AP", "AA:99:99:99:99:99"))) == {}


def test_a_loop_or_a_self_parent_is_never_a_cause_and_cannot_hang():
    found = [offline(SW, "Switch"), offline(AP, "AP"), offline(CAM, "Camera")]
    assert group_findings(found, links(link(SW, "Switch", AP), link(AP, "AP", SW))) == {}
    assert group_findings(found, links(link(CAM, "Camera", CAM))) == {}
    # a loop above a chain: the data contradict themselves, nothing is claimed
    assert group_findings(found, links(link(CAM, "Camera", SW), link(SW, "Switch", AP), link(AP, "AP", SW))) == {}


def test_only_offline_findings_with_a_mac_are_grouped():
    other = {"severity": "warning", "code": "device.cpu_high", "subject": "AP", "message": "CPU", "mac": AP}
    nameless = {"severity": "warning", "code": "device.offline", "subject": "AP", "message": "x", "mac": ""}
    assert group_findings([offline(SW, "Switch"), other, nameless],
                          links(link(SW, "Switch"), link(AP, "AP", SW))) == {}


def test_mac_spelling_does_not_matter_and_the_result_does_not_depend_on_the_input_order():
    found = [offline("aa-00-00-00-00-02", "Switch"), offline("aa:00:00:00:00:03", "AP")]
    topology = links(link(SW, "Switch", GW), link(AP, "AP", "aa-00-00-00-00-02"))
    assert group_findings(found, topology) == group_findings(list(reversed(found)), topology)
    assert list(group_findings(found, topology)) == [ident(AP, "AP")]


def test_a_port_that_is_not_a_number_is_left_out_of_the_evidence():
    topology = links(link(SW, "Switch"), link(AP, "AP", SW, port=True))
    chain = group_findings([offline(SW, "Switch"), offline(AP, "AP")], topology)[ident(AP, "AP")]["evidence"]["chain"]
    assert chain[0]["uplink_port"] is None


def test_a_device_without_a_name_is_shown_by_its_mac():
    topology = links({"mac": SW, "online": False, "parent": ""}, link(AP, "AP", SW))
    chain = group_findings([offline(SW, ""), offline(AP, "AP")], topology)[ident(AP, "AP")]["evidence"]["chain"]
    assert chain[1]["name"] == SW


# -- the data the links come from ---------------------------------------------------------------------------------

def test_device_links_say_where_each_device_of_the_fixture_is_plugged_in(fake_client):
    found = device_links(collect_snapshot(fake_client, "default"))
    switch = found["AA:00:00:00:00:02"]
    assert switch["parent"] == "AA:00:00:00:00:01" and switch["parent_port"] == 2 and switch["online"] is True
    assert found["AA:00:00:00:00:01"]["parent"] == ""                         # a gateway's uplink is its WAN
    assert found["AA:00:00:00:00:04"]["online"] is False and found["AA:00:00:00:00:04"]["parent"] == "AA:00:00:00:00:02"
    assert list(found) == sorted(found)                                       # deterministic order


def test_device_links_fall_back_on_the_integration_api_when_the_legacy_data_has_no_uplink():
    snap = Snapshot(site={}, devices=[{"id": "a", "macAddress": AP, "name": "AP", "state": "OFFLINE"},
                                      {"id": "b", "macAddress": SW, "name": "Switch", "state": "OFFLINE"}],
                    clients=[], device_details={"a": {"uplink": {"deviceId": "b"}}})
    found = device_links(snap)
    assert found[AP]["parent"] == SW and found[SW]["parent"] == "" and found[AP]["online"] is False


def test_the_fixture_has_no_group_because_the_only_offline_device_hangs_off_an_online_switch(fake_client):
    document = diagnose_document(fake_client, "default", echo=False)
    assert group_findings(document.data["findings"], document.meta["links"]) == {}
    json.dumps(document.meta["links"])                                        # plain data, ready for an API


def test_the_fixture_with_the_switch_offline_groups_the_garage_ap_under_it(fake_client):
    fake_client.session.fx["devices"][1]["state"] = "OFFLINE"
    document = diagnose_document(fake_client, "default", echo=False)
    groups = group_findings(document.data["findings"], document.meta["links"])
    by_subject = {f["subject"]: finding_id(f["code"], f["subject"], f["mac"]) for f in document.data["findings"]
                  if f["code"] == "device.offline"}
    assert groups and set(groups) == {by_subject["Garage AP"]}
    assert groups[by_subject["Garage AP"]]["cause"] == by_subject["Office Switch"]
