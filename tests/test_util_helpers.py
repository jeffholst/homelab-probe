"""The small helpers shared by the analysis and rendering modules, tested directly."""

from datetime import datetime

import pytest

from homelab_probe import export, query, topology, wan, wifi
from homelab_probe.util import (
    epoch_text,
    format_time,
    hex_digits,
    known_percent,
    normalize_mac,
    number,
    number_or_zero,
    plural,
    search_rows,
)


@pytest.mark.parametrize("value, expected", [
    (5, 5.0), (5.5, 5.5), (0, 0.0), (-3, -3.0), (True, None), (False, None), (None, None), ("5", None), ("", None),
    ([], None), ({}, None), (float("inf"), float("inf")),
])
def test_number_accepts_real_numbers_only(value, expected):
    assert number(value) == expected
    assert isinstance(number(7), float)


@pytest.mark.parametrize("value, expected", [(5, 5.0), (None, 0.0), ("x", 0.0), (True, 0.0), (-1, -1.0)])
def test_number_or_zero(value, expected):
    assert number_or_zero(value) == expected


@pytest.mark.parametrize("value, expected", [(0, 0.0), (55, 55.0), (99.5, 99.5), (-1, None), (-0.1, None),
                                             (None, None), ("50", None), (True, None)])
def test_known_percent_treats_negative_as_unknown(value, expected):
    assert known_percent(value) == expected


@pytest.mark.parametrize("n, word, expected", [(0, "time", "0 times"), (1, "time", "1 time"), (2, "time", "2 times"),
                                               (1, "neighbor", "1 neighbor"), (7, "neighbor", "7 neighbors")])
def test_plural(n, word, expected):
    assert plural(n, word) == expected


@pytest.mark.parametrize("value, expected", [("aa:bb:cc:dd:ee:ff", "AA:BB:CC:DD:EE:FF"), ("AA:BB", "AA:BB"),
                                             (None, ""), ("", "")])
def test_normalize_mac_uppercases_and_tolerates_missing_values(value, expected):
    assert normalize_mac(value) == expected


@pytest.mark.parametrize("value, expected", [("AA:bb-CC.dd ee:ff", "aabbccddeeff"), ("aabbccddeeff", "aabbccddeeff"),
                                             ("", ""), (None, "none"), (123, "123"), ("a b\tc", "abc")])
def test_hex_digits_strips_every_separator_and_lowercases(value, expected):
    assert hex_digits(value) == expected


def test_epoch_text_formats_seconds_locally_and_blanks_missing_values():
    moment = datetime(2026, 3, 4, 5, 6, 7)
    assert epoch_text(moment.timestamp()) == "2026-03-04 05:06:07"
    assert epoch_text(0) == "" and epoch_text(None) == "" and epoch_text("") == ""


def test_format_time_converts_iso_to_local_and_passes_other_text_through():
    local = datetime.fromisoformat("2026-01-01T09:00:00+00:00").astimezone().strftime("%Y-%m-%d %H:%M:%S")
    assert format_time("2026-01-01T09:00:00Z") == local and format_time("2026-01-01T09:00:00+00:00") == local
    assert format_time("yesterday") == "yesterday" and format_time(None) == "" and format_time("") == ""


def test_search_rows_matches_any_field_case_insensitively():
    rows = [{"Name": "Kitchen Echo", "IP": "10.0.0.5"}, {"Name": "desk", "IP": "10.0.0.6", "Port": 3}]
    assert search_rows(rows, "") is rows
    assert [r["Name"] for r in search_rows(rows, "ECHO")] == ["Kitchen Echo"]
    assert [r["Name"] for r in search_rows(rows, "10.0.0.")] == ["Kitchen Echo", "desk"]
    assert [r["Name"] for r in search_rows(rows, "3")] == ["desk"]                      # numbers are searched as text
    assert search_rows(rows, "nothing") == []


def test_the_private_copies_are_gone():
    """Each helper lives in util.py; a module that needs one imports it instead of defining another."""
    for module, names in ((export, ("_mac", "_fmt_time")), (query, ("_search",)), (topology, ("_num",)),
                          (wan, ("_num",)), (wifi, ("_num", "_clean", "_plural"))):
        for name in names:
            assert not hasattr(module, name), f"{module.__name__}.{name} is a private copy of a util helper"


def test_record_for_looks_up_by_an_id_that_may_be_missing():
    from homelab_probe.util import record_for
    table = {"sw1": {"name": "Office Switch"}, "empty": {}}
    assert record_for(table, "sw1") == {"name": "Office Switch"}
    assert record_for(table, "unknown") == {} and record_for(table, None) == {} and record_for(table, 5) == {}
    assert record_for(table, "empty") == {}
    assert record_for({}, "sw1") == {}


# -- the bind, allowed-host and proxy helpers of `serve` (issue #184) -------------------------------------------

@pytest.mark.parametrize("host, expected", [("0.0.0.0", True), ("::", True), ("[::]", True), ("", True), (" 0.0.0.0 ", True),
                                            ("0", True), ("127.0.0.1", False), ("hlp.lan", False), ("::1", False)])
def test_which_hosts_mean_every_address(host, expected):
    from homelab_probe.util import is_wildcard_bind

    assert is_wildcard_bind(host) is expected


@pytest.mark.parametrize("host, expected", [("127.0.0.1", True), ("127.9.9.9", True), ("::1", True), ("[::1]", True),
                                            ("localhost", True), ("LocalHost", True), ("0.0.0.0", False),
                                            ("192.168.1.5", False), ("example.com", False), ("", False)])
def test_which_hosts_are_loopback(host, expected):
    from homelab_probe.util import is_loopback

    assert is_loopback(host) is expected


@pytest.mark.parametrize("text, expected", [("HLP.lan", "hlp.lan"), ("hlp.lan:8787", "hlp.lan"), ("192.168.1.5", "192.168.1.5"),
                                            ("192.168.1.5:80", "192.168.1.5"), ("[FD00::5]", "[fd00::5]"),
                                            ("[fd00:0:0::5]:9", "[fd00::5]"), (" a-b.c ", "a-b.c"), ("x", "x")])
def test_allowed_hosts_are_normalized_to_a_bare_lower_case_host(text, expected):
    from homelab_probe.util import parse_allowed_host

    assert parse_allowed_host(text) == expected


@pytest.mark.parametrize("text", ["*", "*.lan", "", "a b", "http://x", "x/y", "x?y", "x@y", "a..b", ".a", "a.", "-a", "a-",
                                  "1.2.3.4.5:80x", "[::1", "[]", "[::1]x", "x:y", "a_b", "x" * 254])
def test_things_that_are_not_hosts_are_refused(text):
    from homelab_probe.util import parse_allowed_host

    with pytest.raises(ValueError, match="not a host name or address"):
        parse_allowed_host(text)


def test_forwarded_ips_are_a_list_of_addresses_or_networks_and_never_everyone():
    from homelab_probe.util import parse_forwarded_ips

    assert parse_forwarded_ips("127.0.0.1, 10.0.0.0/8 ,::1,fd00::/8") == "127.0.0.1,10.0.0.0/8,::1,fd00::/8"
    for bad in ("*", "0.0.0.0/0x", "", " , ", "proxy", "10.0.0.256"):
        with pytest.raises(ValueError):
            parse_forwarded_ips(bad)


def test_binding_every_address_needs_an_allowed_host_and_nothing_else_does():
    from homelab_probe.util import check_bind

    assert check_bind("127.0.0.1") == "127.0.0.1" and check_bind("192.168.1.5") == "192.168.1.5"
    assert check_bind("hlp.lan") == "hlp.lan" and check_bind("0.0.0.0", ["hlp.lan"]) == "0.0.0.0"
    for host in ("0.0.0.0", "::", ""):
        with pytest.raises(ValueError, match="--allowed-host"):
            check_bind(host)
