"""The small helpers shared by the analysis and rendering modules, tested directly."""

from datetime import datetime

import pytest

from unifi_sentinel import export, query, topology, wan, wifi
from unifi_sentinel.util import (
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
    from unifi_sentinel.util import record_for
    table = {"sw1": {"name": "Office Switch"}, "empty": {}}
    assert record_for(table, "sw1") == {"name": "Office Switch"}
    assert record_for(table, "unknown") == {} and record_for(table, None) == {} and record_for(table, 5) == {}
    assert record_for(table, "empty") == {}
    assert record_for({}, "sw1") == {}
