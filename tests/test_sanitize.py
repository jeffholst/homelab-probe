"""The recording sanitiser (tools/sanitize.py): consistent, deterministic, class-preserving, and it checks itself."""

import ipaddress

import pytest
from sanitize import LeakError, Sanitizer, check_no_leaks

MAC_A, MAC_B = "A4:BB:CC:00:00:01", "02:11:22:33:44:55"


def sanitise(data, device_macs=(), presets=None):
    sanitizer = Sanitizer(device_macs, presets)
    return sanitizer(data), sanitizer


def test_one_real_value_is_one_synthetic_value_everywhere():
    data = {"sta": [{"mac": MAC_A.lower(), "ip": "192.168.1.10"}],
            "log": [{"message": f"{MAC_A.replace(':', '-')} got 192.168.1.10 from {MAC_A}"}]}
    clean, _ = sanitise(data)
    mac = clean["sta"][0]["mac"]
    assert mac != MAC_A.lower()
    assert clean["log"][0]["message"] == f"{mac} got {clean['sta'][0]['ip']} from {mac}"
    assert clean["sta"][0]["ip"] != "192.168.1.10"


def test_the_output_depends_only_on_the_input_not_its_order():
    data = {"a": [{"mac": MAC_A, "name": "Alpha Switch", "ip": "10.1.1.5"},
                  {"mac": MAC_B, "name": "Beta Phone", "ip": "10.1.2.7"}]}
    reordered = {"a": list(reversed(data["a"]))}
    first, _ = sanitise(data)
    second, _ = sanitise(data)
    third, _ = sanitise(reordered)
    assert first == second
    assert third["a"] == list(reversed(first["a"]))


def test_a_device_keeps_being_a_device_and_a_randomized_mac_stays_randomized():
    clean, _ = sanitise([MAC_A, MAC_B, "00:11:22:33:44:56"], device_macs=[MAC_A])
    device, randomized, plain = clean
    assert device.startswith("a0:") and randomized.startswith("b2:") and plain.startswith("b0:")
    assert len({device, randomized, plain}) == 3


def test_private_addresses_keep_their_network_and_last_octet():
    data = ["192.168.1.10", "192.168.1.20", "172.16.5.20", "10.9.9.1"]
    clean, _ = sanitise(data)
    a, b, c, d = (ipaddress.ip_address(x) for x in clean)
    assert a.packed[:3] == b.packed[:3] and (a.packed[3], b.packed[3]) == (10, 20)
    assert len({a.packed[:3], c.packed[:3], d.packed[:3]}) == 3 and c.packed[3] == 20
    assert all(ip in ipaddress.ip_network("10.0.0.0/8") for ip in (a, b, c, d))


def test_other_address_classes_stay_in_their_class():
    clean, _ = sanitise(["100.70.1.2", "169.254.7.8", "8.8.4.4", "93.184.216.34", "fe80::1234", "fd12:3456::9",
                         "2606:4700::1111"])
    cgnat, link, public1, public2, link6, ula, global6 = clean
    assert ipaddress.ip_address(cgnat) in ipaddress.ip_network("100.64.0.0/10")
    assert ipaddress.ip_address(link) in ipaddress.ip_network("169.254.0.0/16")
    documentation = [ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")]
    assert public1 != public2 and all(any(ipaddress.ip_address(p) in n for n in documentation) for p in (public1, public2))
    assert ipaddress.ip_address(link6).is_link_local
    assert ipaddress.ip_address(ula) in ipaddress.ip_network("fc00::/7")
    assert ipaddress.ip_address(global6) in ipaddress.ip_network("2001:db8::/32")


def test_addresses_inside_text_and_cidr_are_replaced_and_lookalikes_are_not():
    clean, _ = sanitise({"ip_subnet": "192.168.1.1/24", "message": "roamed to 10.0.0.5. Version 10.6.106, 999.1.1.1",
                         "time": "10:13:05"})
    assert clean["ip_subnet"].endswith(".1/24") and not clean["ip_subnet"].startswith("192.168")
    assert "10.0.0.5" not in clean["message"] and "Version 10.6.106, 999.1.1.1" in clean["message"]
    assert clean["time"] == "10:13:05"


def test_addresses_that_identify_nothing_are_kept():
    data = ["0.0.0.0", "255.255.255.255", "255.255.255.0", "127.0.0.1", "::1", "ff:ff:ff:ff:ff:ff", "224.0.0.251"]
    clean, sanitizer = sanitise(data)
    assert clean == data
    check_no_leaks(clean, sanitizer)


def test_ids_are_mapped_and_a_chosen_one_is_pinned():
    site, other = "5f3c1a2b-0000-4000-8000-123456789abc", "64b0f3e2a1c9d8e7f6a5b4c3"
    clean, _ = sanitise({"sites": [{"id": site}], "net": {"_id": other}, "url": f"/sites/{site}/devices"},
                        presets={site: "site-1"})
    assert clean["sites"][0]["id"] == "site-1" and clean["url"] == "/sites/site-1/devices"
    assert clean["net"]["_id"] != other and len(clean["net"]["_id"]) == 24


def test_names_are_replaced_by_kind_whole_words_only_without_regard_to_case():
    data = {"devices": [{"name": "Garage Switch", "hostname": "garage-pc"}],
            "wlan": {"essid": "FamilyNet"}, "health": {"isp_name": "Example Telecom"},
            "message": "garage-pc moved from GARAGE SWITCH to FamilyNet; Garage Switches are fine"}
    clean, _ = sanitise(data)
    assert clean["devices"][0] == {"name": "Name 1", "hostname": "host-1"}
    assert clean["wlan"]["essid"] == "SSID 1" and clean["health"]["isp_name"] == "ISP 1"
    assert clean["message"] == "host-1 moved from Name 1 to SSID 1; Garage Switches are fine"


def test_one_text_with_several_kinds_gets_one_label():
    clean, _ = sanitise([{"name": "Studio", "hostname": "studio"}, {"essid": "Studio"}])
    assert clean[0]["name"] == clean[0]["hostname"] == clean[1]["essid"] == "SSID 1"


def test_generic_and_short_names_and_chosen_names_are_handled():
    data = {"wan1": {"name": "WAN"}, "ap": {"name": "AP"}, "site": {"name": "Our Home", "internalReference": "x1y2z3"}}
    clean, _ = sanitise(data, presets={"our home": "Default", "x1y2z3": "default"})
    assert clean["wan1"]["name"] == "WAN" and clean["ap"]["name"] == "AP"
    assert clean["site"] == {"name": "Default", "internalReference": "default"}


def test_monitor_targets_are_sanitized_consistently():
    data = {"monitors": [{"target": "1.1.1.1", "type": "ICMP"}, {"target": "router.example.net"}],
            "ip": "1.1.1.1", "firmwareVersion": "4.3.2.1", "name": "Hallway Cam",
            "parameters": {"DURATION": {"name": "25s"}, "CLIENT": {"name": "Hallway Cam"}}}
    clean, sanitizer = sanitise(data)
    check_no_leaks(clean, sanitizer)
    assert clean["monitors"][0]["target"] == clean["ip"] != "1.1.1.1"
    assert clean["monitors"][1]["target"] == "host-1"
    assert clean["monitors"][0]["type"] == "ICMP" and clean["firmwareVersion"] == "4.3.2.1"
    assert clean["parameters"]["DURATION"] == {"name": "25s"} and clean["parameters"]["CLIENT"]["name"] == "Name 1"


def test_a_clean_result_passes_the_leak_check():
    data = {"mac": MAC_A, "ip": "192.168.1.9", "name": "Hallway Camera", "note": f"{MAC_A} is Hallway Camera"}
    clean, sanitizer = sanitise(data)
    check_no_leaks(clean, sanitizer)
    sanitizer.check(clean)


def test_the_leak_check_rejects_an_address_nobody_issued():
    clean, sanitizer = sanitise({"ip": "192.168.1.9", "mac": MAC_A})
    for stray in ({**clean, "extra": "10.77.77.77"}, {**clean, "extra": "02:de:ad:be:ef:01"},
                  {**clean, "extra": "2001:db8:dead::99"}, {**clean, "extra": "0a1b2c3d-1111-4222-8333-444455556666"},
                  {**clean, "extra": "0123456789abcdef01234567"}):
        with pytest.raises(LeakError, match=r"(?s)1 leak\(s\).*extra"):
            check_no_leaks(stray, sanitizer)


def test_the_leak_check_rejects_a_real_name_that_survived():
    clean, sanitizer = sanitise({"name": "Hallway Camera", "ip": "192.168.1.9"})
    with pytest.raises(LeakError, match="a real name is still present") as caught:
        check_no_leaks({**clean, "text": "see the HALLWAY CAMERA, it is down"}, sanitizer)
    assert "Hallway" not in str(caught.value) and "192.168" not in str(caught.value)


def test_the_leak_check_names_the_place_never_the_value():
    _, sanitizer = sanitise({"devices": [{"mac": MAC_A}]})
    with pytest.raises(LeakError) as caught:
        check_no_leaks({"devices": [{"mac": MAC_A.lower()}]}, sanitizer)
    text = str(caught.value)
    assert "devices[].mac" in text and MAC_A.lower() not in text and "a4:bb" not in text.lower()


def test_a_name_that_is_an_address_is_mapped_as_an_address():
    clean, sanitizer = sanitise({"parameters": {"IP": {"name": "10.0.0.50"}, "CLIENT": {"name": MAC_A}}})
    address = clean["parameters"]["IP"]["name"]
    assert address.startswith("10.200.0.") and address.endswith(".50")
    assert clean["parameters"]["CLIENT"]["name"].startswith("b0:")
    check_no_leaks(clean, sanitizer)
