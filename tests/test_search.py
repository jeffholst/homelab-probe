"""Global search (``search.py``): what matches, in which order, and what it says when a source could not be read."""

import json
import random

import pytest
from conftest import FakeResponse
from test_needs import BASE, reads

from homelab_probe.diagnose import CRITICAL, INFO, WARNING, Finding, diagnose, needs_for
from homelab_probe.documents import diagnose_document
from homelab_probe.search import (
    DEFAULT_LIMIT,
    KINDS,
    MAX_QUERY,
    SEARCH_NEEDS,
    Query,
    build_search,
    parse_query,
)
from homelab_probe.snapshot import Needs, Snapshot, collect_snapshot
from homelab_probe.triage import finding_id

SECRET = "pa55-never-print-this-passphrase"


def snapshot(fake_client, needs=SEARCH_NEEDS):
    return collect_snapshot(fake_client, "default", needs)


def search(snap, text, limit=DEFAULT_LIMIT, findings=(), subjects=()):
    return build_search(snap, list(findings), parse_query(text), limit, subjects)


def keys(result, kind):
    return [hit["key"] for hit in result["items"] if hit["kind"] == kind]


def fail(fake_client, *suffixes):
    get = fake_client.session.get

    def request(url, *args, **kwargs):
        if any(url.endswith(suffix) for suffix in suffixes):
            return FakeResponse(503, {})
        return get(url, *args, **kwargs)

    fake_client.session.get = request


def subject(name, ref="device:AA:BB:CC:00:00:09", at=1_900_000_000.0, count=1):
    return {"subject": ref, "kind": ref.partition(":")[0], "note_count": count, "last_note_at": at,
            "last_known": None if name is None else {"name": name, "recorded_at": at}}


# -- what is read ---------------------------------------------------------------------------------------------------

def test_search_reads_what_the_findings_read_plus_the_history_the_wifi_and_the_networks(fake_client):
    snapshot(fake_client)
    searched = reads(fake_client)
    assert fake_client.session.posts, "the findings need the event log"
    other = type(fake_client)("https://controller", "key")
    other.session = type(fake_client.session)()
    diagnose_document(other, "default", echo=False)
    assert searched == reads(other) | {"alluser", "wlans", "networkconf"}
    assert searched >= BASE and not searched & {"groups", "firewall", "neighbors"}


def test_the_needs_are_the_union_and_nothing_more():
    assert SEARCH_NEEDS == Needs(**{**vars(needs_for(None, 86400)), "offline": True, "wlans": True, "networks": True})
    assert not (SEARCH_NEEDS.groups or SEARCH_NEEDS.firewall or SEARCH_NEEDS.neighbors or SEARCH_NEEDS.users_required)


# -- the query --------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["", "a", " a ", "   ", "x" * (MAX_QUERY + 1), " " + "x" * 70])
def test_a_search_text_must_have_two_to_sixty_four_characters_once_trimmed(text):
    with pytest.raises(ValueError, match="2 to 64"):
        parse_query(text)


def test_a_search_text_is_trimmed_and_the_bounds_are_inclusive():
    assert parse_query("  ab  ").text == "ab" and parse_query("x" * MAX_QUERY).text == "x" * MAX_QUERY


def test_a_mac_fragment_needs_four_hex_digits_and_an_address_that_is_not_one_is_text():
    assert parse_query("aa:bb").digits == "aabb" and parse_query("aabb.ccdd").digits == "aabbccdd"
    assert parse_query("AA-BB-CC").digits == "aabbcc" and parse_query("aabbccddeeff").digits == "aabbccddeeff"
    assert parse_query("aa:b").digits == "" and parse_query("aa").digits == ""
    assert parse_query("10.0.0.5").digits == ""            # an address is not a MAC fragment, whatever its digits say
    assert parse_query("gateway").digits == "" and parse_query("12:34:zz").digits == ""
    assert parse_query("10.0.0.5").ip == "10.0.0.5" and parse_query("FE80:0:0:0:0:0:0:1").ip == "fe80::1"
    assert parse_query("10.0.0").ip == "" and isinstance(parse_query("ab"), Query)


# -- MAC and IP spellings ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["AA:00:00:00:00:03", "aa-00-00-00-00-03", "AA00.0000.0003", "aa0000000003",
                                  "  Aa:00:00:00:00:03  ", "00:00:00:03", "0000.0003", "00-00-00-00-03"])
def test_every_spelling_of_a_mac_finds_the_same_device(fake_client, text):
    result = search(snapshot(fake_client), text)
    assert keys(result, "device") == ["AA:00:00:00:00:03"]
    assert result["items"][0]["matched"] == ["mac"]


@pytest.mark.parametrize("text", ["BB:00:00:00:00:02", "bb-00-00-00-00-02", "BB00.0000.0002", "bb0000000002"])
def test_every_spelling_of_a_mac_finds_the_same_client(fake_client, text):
    assert keys(search(snapshot(fake_client), text), "client") == ["BB:00:00:00:00:02"]


def test_a_mac_fragment_of_less_than_four_digits_is_text_only(fake_client):
    snap = snapshot(fake_client)
    assert keys(search(snap, "00:03"), "device") == ["AA:00:00:00:00:03"]         # four digits across the colon
    assert keys(search(snap, "03"), "device") == ["AA:00:00:00:00:03"]            # as text, inside the address
    assert keys(search(snap, "0:0"), "device") == [
        f"AA:00:00:00:00:0{n}" for n in (4, 1, 3, 2)]                                   # by name: Garage, Gateway...
    assert "BB:00:00:00:00:01" in keys(search(snap, "0:0"), "client")             # text matches the colon form only


def test_an_ip_matches_as_text_and_a_whole_address_in_any_spelling(fake_client):
    fx = fake_client.session.fx
    fx["clients"][0]["ipAddress"] = "FE80:0:0:0:0:0:0:ABCD"
    fx["devices"][1]["ipAddress"] = "2001:DB8::1"
    snap = snapshot(fake_client)
    for text in ("fe80::abcd", "FE80::ABCD", "fe80:0000:0000:0000:0000:0000:0000:abcd", "Fe80:0:0:0:0:0:0:abCD"):
        result = search(snap, text)
        assert keys(result, "client") == ["BB:00:00:00:00:01"], text
        assert result["items"][0]["matched"] == ["ip"] and result["items"][0]["ip"] == "FE80:0:0:0:0:0:0:ABCD"
    assert keys(search(snap, "2001:db8:0:0:0:0:0:1"), "device") == ["AA:00:00:00:00:02"]
    assert keys(search(snap, "10.0.0.1"), "device") == ["AA:00:00:00:00:01"]
    assert keys(search(snap, "10.0.0.11"), "client") == ["BB:00:00:00:00:02"]
    assert keys(search(snap, "10.0.0.1"), "client") == ["BB:00:00:00:00:02"]       # a fragment is a substring too


def test_a_client_is_found_by_any_name_it_has_and_by_its_last_known_address(fake_client):
    fx = fake_client.session.fx
    fx["legacy"]["sta"] = [{"mac": "BB-00-00-00-00-02", "name": "Pocket", "hostname": "iphone-of-someone",
                            "ip": "10.0.0.77"}]
    result = search(snapshot(fake_client), "someone")
    assert keys(result, "client") == ["BB:00:00:00:00:02"] and result["items"][0]["matched"] == ["name"]
    assert keys(search(snapshot(fake_client), "10.0.0.77"), "client") == ["BB:00:00:00:00:02"]
    offline = search(snapshot(fake_client), "old-tablet")                          # in the history only
    assert offline["items"][0] == {"kind": "client", "key": "BB:00:00:00:00:04", "label": "old-tablet",
                                   "matched": ["name"], "mac": "BB:00:00:00:00:04", "ip": "10.0.0.51",
                                   "status": "offline"}
    assert search(snapshot(fake_client), "10.0.0.51")["items"][0]["key"] == "BB:00:00:00:00:04"


def test_a_device_is_found_by_name_model_and_the_legacy_name_and_says_whether_it_is_online(fake_client):
    fake_client.session.fx["legacy"]["device"][0]["name"] = "Rack Gateway"
    snap = snapshot(fake_client)
    gateway = search(snap, "rack gate")["items"][0]
    assert gateway["key"] == "AA:00:00:00:00:01" and gateway["matched"] == ["name"] and gateway["status"] == "online"
    assert gateway["label"] == "Gateway" and gateway["model"] == "UCG Max" and gateway["ip"] == "10.0.0.1"
    assert keys(search(snap, "u6 pro"), "device") == ["AA:00:00:00:00:04"]
    assert search(snap, "u6 pro")["items"][0]["status"] == "offline"


# -- networks and Wi-Fi networks ----------------------------------------------------------------------------------------

def test_wifi_networks_and_networks_are_found_by_name_and_networks_by_subnet_and_vlan(fake_client):
    snap = snapshot(fake_client)
    result = search(snap, "homenet")
    assert result["items"] == [{"kind": "ssid", "key": "wlan-1", "label": "HomeNet", "matched": ["name"],
                                "security": "WPA2/WPA3", "enabled": True, "network": "Main"}]
    assert search(snap, "retired")["items"][0]["enabled"] is False
    iot = search(snap, "10.0.20")["items"]
    assert [(h["kind"], h["label"], h["matched"]) for h in iot] == [("network", "IoT", ["subnet"])]
    assert iot[0]["subnet"] == "10.0.20.0/24" and iot[0]["vlan"] == 20 and iot[0]["purpose"] == "corporate"
    assert keys(search(snap, "20"), "network") == ["net-2"]                          # the VLAN id, as text
    assert [h["label"] for h in search(snap, "remote")["items"]] == ["Remote Access"]


def test_a_record_without_an_id_is_keyed_by_its_name(fake_client):
    fx = fake_client.session.fx
    for record in fx["legacy_rest"]["wlanconf"] + fx["legacy_rest"]["networkconf"]:
        record.pop("_id", None)
    snap = snapshot(fake_client)
    assert keys(search(snap, "homenet"), "ssid") == ["HomeNet"] and keys(search(snap, "iot"), "network") == ["IoT"]


def test_the_passphrase_is_never_read(fake_client):
    class Guarded(dict):
        def _check(self, key):
            if "pass" in str(key) or "psk" in str(key) or "key" in str(key):
                raise AssertionError(f"{key!r} was read")

        def get(self, key, default=None):
            self._check(key)
            return super().get(key, default)

        def __getitem__(self, key):
            self._check(key)
            return super().__getitem__(key)

        def items(self):
            raise AssertionError("the whole record was walked")

        values = items

        def __iter__(self):
            raise AssertionError("the whole record was walked")

    snap = snapshot(fake_client)
    snap.wlans = [Guarded(w, x_passphrase=SECRET, x_iapp_key=SECRET) for w in snap.wlans]
    assert keys(search(snap, "homenet"), "ssid") == ["wlan-1"]
    assert search(snap, SECRET)["items"] == [] and search(snap, "x_passphrase")["items"] == []


# -- findings ---------------------------------------------------------------------------------------------------------

def test_findings_are_found_by_code_subject_message_mac_and_id_with_the_id_the_findings_list_uses(fake_client):
    snap = snapshot(fake_client)
    findings = diagnose(snap)
    offline = next(f for f in findings if f.code == "device.offline")
    ident = finding_id(offline.code, offline.subject, offline.target_mac)
    for text, field in (("device.offline", "code"), ("garage", "subject"), ("device is off", "message"),
                        (offline.target_mac.replace(":", "-"), "mac"), (ident, "id")):
        result = search(snap, text, findings=findings)
        hit = next(h for h in result["items"] if h["kind"] == "finding" and h["key"] == ident)
        assert field in hit["matched"], text
        assert (hit["severity"], hit["code"], hit["message"], hit["label"]) == (
            offline.severity, "device.offline", offline.message, offline.subject)
    assert keys(search(snap, ident, findings=findings), "finding") == [ident]
    assert keys(search(snap, ident[:8], findings=findings), "finding") == []       # an id is whole or nothing


def test_findings_are_ordered_by_how_well_they_match_then_severity_then_subject():
    findings = [Finding(INFO, "alpha router", "restarted 5m ago", code="device.recent_reboot"),
                Finding(WARNING, "beta", "the router is slow", code="wan.slow"),
                Finding(CRITICAL, "gamma", "router overheating", code="device.overheating"),
                Finding(WARNING, "router", "x", code="device.offline"),
                Finding(WARNING, "Router 2", "x", code="device.offline")]
    result = build_search(Snapshot(site={}, devices=[], clients=[]), findings, parse_query("router"))
    assert [h["label"] for h in result["items"]] == ["router", "gamma", "Router 2", "beta", "alpha router"]


def test_a_finding_with_no_mac_is_keyed_by_the_subject_and_one_with_a_mac_by_the_mac():
    named = Finding(WARNING, "Old Name", "x", code="device.offline")
    with_mac = Finding(WARNING, "Old Name", "x", "aa-bb-cc-dd-ee-ff", "device.offline")
    result = build_search(Snapshot(site={}, devices=[], clients=[]), [named, with_mac], parse_query("old name"))
    assert set(keys(result, "finding")) == {finding_id("device.offline", "Old Name"),
                                            finding_id("device.offline", "A different name", "AA:BB:CC:DD:EE:FF")}


# -- notes ------------------------------------------------------------------------------------------------------------

def test_a_subject_with_notes_is_found_by_its_last_known_name_even_when_nothing_is_there_now(fake_client):
    snap = snapshot(fake_client)
    result = search(snap, "retired cabinet", subjects=[subject("Retired Cabinet Switch"), subject("Other", "client:"
                                                                                                  "AA:BB:CC:00:00:08")])
    assert result["items"] == [{"kind": "note", "key": "device:AA:BB:CC:00:00:09", "label": "Retired Cabinet Switch",
                                "matched": ["name"], "subject_kind": "device", "note_count": 1,
                                "last_note_at": "2030-03-17T17:46:40Z"}]
    assert result["kinds"]["note"] == {"total": 1, "shown": 1} and result["complete"] is True


@pytest.mark.parametrize("text", ["AA:BB:CC:00:00:09", "aa-bb-cc-00-00-09", "AABB.CC00.0009", "aabbcc000009",
                                  "bb:cc:00:00", "device:aa:bb"])
def test_a_subject_is_found_by_its_mac_in_any_spelling(fake_client, text):
    result = search(snapshot(fake_client), text, subjects=[subject(None), subject("x", "client:AA:BB:CC:00:00:08")])
    assert [h["key"] for h in result["items"] if h["kind"] == "note"][:1] == ["device:AA:BB:CC:00:00:09"], text
    assert result["items"][-1]["label"] in ("device:AA:BB:CC:00:00:09", "x")          # no name: the reference labels


def test_a_finding_subject_is_found_by_its_id_and_a_client_subject_is_not_matched_as_a_device(fake_client):
    ident = "0123456789abcdef"
    result = search(snapshot(fake_client), ident, subjects=[subject("cleared", f"finding:{ident}"),
                                                            subject("c", "client:AA:BB:CC:00:00:09")])
    assert keys(result, "note") == [f"finding:{ident}"] and result["items"][0]["subject_kind"] == "finding"
    assert keys(search(snapshot(fake_client), "0123", subjects=[subject("c", f"finding:{ident}")]), "note") == [
        f"finding:{ident}"]                                                              # as text, inside the reference


def test_unreadable_notes_are_reported_as_not_searched_and_not_as_no_hit(fake_client):
    result = search(snapshot(fake_client), "retired", subjects=None)
    assert result["unavailable"] == ["note"] and result["complete"] is False
    assert result["limitations"] == ["The notes could not be read, so none were searched."]
    assert result["kinds"]["note"] == {"total": 0, "shown": 0}
    assert search(snapshot(fake_client), "retired", subjects=[])["unavailable"] == []


# -- partial reads ----------------------------------------------------------------------------------------------------

def test_a_complete_read_says_so(fake_client):
    result = search(snapshot(fake_client), "gateway")
    assert result["complete"] is True and result["unavailable"] == [] and result["limitations"] == []


def test_unreadable_wifi_networks_are_named_and_the_rest_is_still_searched(fake_client):
    fail(fake_client, "/rest/wlanconf")
    result = search(snapshot(fake_client), "net")
    assert result["unavailable"] == ["ssid"] and result["complete"] is False
    assert keys(result, "ssid") == [] and keys(result, "network") == ["net-wan"]
    assert "The Wi-Fi networks could not be read, so none were searched." in result["limitations"]
    assert any("see the warnings" in line for line in result["limitations"])
    assert keys(search(snapshot(fake_client), "gateway"), "device") == ["AA:00:00:00:00:01"]


def test_unreadable_networks_are_named(fake_client):
    fail(fake_client, "/rest/networkconf")
    result = search(snapshot(fake_client), "main")
    assert result["unavailable"] == ["network"] and result["complete"] is False and keys(result, "network") == []
    assert keys(result, "ssid") == [] and "The networks could not be read, so none were searched." in result[
        "limitations"]


def test_a_client_history_that_could_not_be_read_makes_the_answer_incomplete_without_hiding_the_live_clients(
        fake_client):
    fail(fake_client, "/stat/alluser")
    result = search(snapshot(fake_client), "old-tablet")
    assert result["complete"] is False and result["unavailable"] == [] and keys(result, "client") == []
    assert result["limitations"] == ["Some data could not be read (see the warnings), so something that exists may "
                                     "not be found."]
    assert keys(search(snapshot(fake_client), "desktop"), "client") == ["BB:00:00:00:00:01"]


def test_a_missing_optional_read_of_the_findings_makes_the_answer_incomplete(fake_client):
    fail(fake_client, "/stat/health")
    assert search(snapshot(fake_client), "gateway")["complete"] is False


# -- limits, truncation, order ----------------------------------------------------------------------------------------

def test_the_limit_is_per_kind_and_the_totals_say_what_was_cut(fake_client):
    snap = snapshot(fake_client)
    findings = diagnose(snap)
    result = search(snap, "aa", limit=2, findings=findings)
    assert result["limit"] == 2 and result["truncated"] is True
    assert result["kinds"]["device"] == {"total": 4, "shown": 2}
    assert result["kinds"]["finding"]["shown"] == 2 and result["kinds"]["finding"]["total"] > 2
    assert len(keys(result, "device")) == 2 and len(keys(result, "finding")) == 2
    assert [h["kind"] for h in result["items"]] == ["device", "device", "finding", "finding"]
    whole = search(snap, "aa", limit=50, findings=findings)
    assert whole["truncated"] is False and all(v["total"] == v["shown"] for v in whole["kinds"].values())
    assert set(whole["kinds"]) == set(KINDS)


def test_the_limit_applies_after_the_order_not_before(fake_client):
    fx = fake_client.session.fx
    fx["devices"][3]["name"] = "gate"                      # an exact match must win over "Gateway" (a prefix)
    snap = snapshot(fake_client)
    assert keys(search(snap, "gate", limit=1), "device") == ["AA:00:00:00:00:04"]
    assert keys(search(snap, "gate"), "device") == ["AA:00:00:00:00:04", "AA:00:00:00:00:01"]


def test_the_order_does_not_depend_on_the_order_the_controller_listed_things_in(fake_client):
    fx = fake_client.session.fx
    reference = search(snapshot(fake_client), "in", findings=diagnose(snapshot(fake_client)))
    shuffler = random.Random(7)
    for _ in range(5):
        for records in (fx["devices"], fx["clients"], fx["legacy_rest"]["wlanconf"], fx["legacy_rest"]["networkconf"],
                        fx["legacy"]["alluser"]):
            shuffler.shuffle(records)
        snap = snapshot(fake_client)
        assert search(snap, "in", findings=diagnose(snap)) == reference
    assert len(reference["items"]) > 5


def test_two_records_that_tie_keep_the_order_they_came_in(fake_client):
    fx = fake_client.session.fx
    for wlan in fx["legacy_rest"]["wlanconf"]:
        wlan["name"] = "Same"
    assert keys(search(snapshot(fake_client), "same"), "ssid") == [w["_id"] for w in fx["legacy_rest"]["wlanconf"]]


def test_the_kinds_are_listed_in_a_fixed_order():
    assert KINDS == ("client", "device", "ssid", "network", "finding", "note")


# -- hostile strings --------------------------------------------------------------------------------------------------

HOSTILE = ['<img src=x onerror=alert(1)>', "'; DROP TABLE clients; --", "\u202eevil\u202c", "\x1b[31mred\x1b[0m",
           "line one\nline two", "${jndi:ldap://x.example/a}", "{{7*7}}", "=cmd|' /C calc'!A0", "..\\..\\etc\\passwd",
           "zero\u200bwidth", "a" * 300, "\u0000nul"]


@pytest.mark.parametrize("name", HOSTILE)
def test_hostile_names_are_returned_verbatim_in_every_kind_and_never_interpreted(fake_client, name):
    fx = fake_client.session.fx
    fx["clients"][0]["name"] = name
    fx["devices"][0]["name"] = name
    fx["legacy_rest"]["wlanconf"][0]["name"] = name
    fx["legacy_rest"]["networkconf"][0]["name"] = name
    snap = snapshot(fake_client)
    needle = name.strip()[:60] if name.strip() else name
    finding = Finding(WARNING, name, name, code="device.offline")
    subjects = [subject(name)]
    result = search(snap, needle[:MAX_QUERY], findings=[finding], subjects=subjects)
    labels = {h["kind"]: h["label"] for h in result["items"]}
    assert labels == {"client": name, "device": name, "ssid": name, "network": name, "finding": name, "note": name}
    assert json.loads(json.dumps(result)) == result                                  # and it is plain JSON data


@pytest.mark.parametrize("text", [".*", ".+", "^$", "(a", "[a-z]", "\\d+", "a|b", "%%", "__", "{}"])
def test_the_search_text_is_never_a_pattern_or_a_wildcard(fake_client, text):
    snap = snapshot(fake_client)
    result = search(snap, text, findings=diagnose(snap))
    assert result["items"] == [], text


def test_a_pattern_does_not_match_everything():
    findings = [Finding(WARNING, "one", "plain", code="device.offline")]
    snap = Snapshot(site={}, devices=[{"macAddress": "aa:bb:cc:dd:ee:01", "name": "plain"}], clients=[])
    assert build_search(snap, findings, parse_query(".*"))["items"] == []
    assert build_search(snap, findings, parse_query("p.ain"))["items"] == []
    assert len(build_search(snap, findings, parse_query("plain"))["items"]) == 2


def test_case_folding_finds_non_ascii_names_whatever_their_case():
    snap = Snapshot(site={}, devices=[{"macAddress": "aa:bb:cc:dd:ee:01", "name": "Wohnzimmer GROẞE Lampe"}],
                    clients=[])
    for text in ("große", "GROSSE", "Groẞe"):
        assert [h["label"] for h in build_search(snap, [], parse_query(text))["items"]] == [
            "Wohnzimmer GROẞE Lampe"], text


# -- odd records ------------------------------------------------------------------------------------------------------

def test_records_with_missing_or_odd_fields_are_skipped_or_tolerated_and_never_raise():
    snap = Snapshot(
        site={},
        devices=[{"macAddress": "aa:bb:cc:dd:ee:01", "name": None, "ipAddress": 5, "model": 7},
                 {"name": "no mac"},
                 {"macAddress": "AA-BB-CC-DD-EE-01", "name": "duplicate"},
                 {"macAddress": "aa:bb:cc:dd:ee:02", "name": "kept", "state": None}],
        clients=[{"macAddress": "bb:bb:cc:dd:ee:01", "name": 12, "ipAddress": 5},
                 {"macAddress": "bb:bb:cc:dd:ee:02", "name": "okay", "ipAddress": None}],
        legacy_clients=[{"mac": "bb:bb:cc:dd:ee:02", "hostname": 5, "name": ""}],
        networks=[{"name": 55, "purpose": None}], wlans=[{"name": None, "security": 3}])
    result = build_search(snap, [], parse_query("ee:0"))
    assert keys(result, "device") == ["AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"]
    assert {h["label"] for h in result["items"] if h["kind"] == "device"} == {"AA:BB:CC:DD:EE:01", "kept"}
    assert keys(result, "client") == ["BB:BB:CC:DD:EE:01", "BB:BB:CC:DD:EE:02"]
    assert [h["label"] for h in build_search(snap, [], parse_query("55"))["items"]] == ["55"]
    assert json.dumps(result) and build_search(snap, [], parse_query("no such"))["items"] == []


def test_a_mac_matches_best_as_the_whole_address_then_as_the_start_then_anywhere():
    snap = Snapshot(site={}, devices=[{"macAddress": "11:aa:bb:cc:dd:ee", "name": "a contains"},
                                      {"macAddress": "aa:bb:cc:dd:ee:01", "name": "z prefix"},
                                      {"macAddress": "aa:bb:cc:dd:00:00", "name": "m other"}], clients=[])
    ranked = [h["label"] for h in build_search(snap, [], parse_query("AABB.CCDD"))["items"]]
    assert ranked == ["m other", "z prefix", "a contains"]            # the two prefixes by name, then the one inside
    assert [h["label"] for h in build_search(snap, [], parse_query("11aabbccddee"))["items"]] == ["a contains"]
    assert [h["label"] for h in build_search(snap, [], parse_query("aabbccdd0000"))["items"]] == ["m other"]


def test_nothing_found_is_an_empty_list_with_the_totals_at_zero(fake_client):
    result = search(snapshot(fake_client), "no such thing anywhere")
    assert result["items"] == [] and result["truncated"] is False
    assert all(v == {"total": 0, "shown": 0} for v in result["kinds"].values())
