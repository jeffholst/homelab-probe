"""`query --csv`: the columns and rows of --json as CSV that a spreadsheet cannot turn into a formula."""

import csv
import io
import json
import time

import pytest

from unifi_sentinel import cli
from unifi_sentinel.query import PORT_COLUMNS, csv_cell, data_columns, port_rows, render_csv
from unifi_sentinel.snapshot import Needs, collect_snapshot

KINDS = [
    ["all"], ["devices"], ["clients"], ["clients", "--include-offline"], ["reservations"],
    ["ports"], ["ports", "--down"], ["ports", "--errors"],
    ["ports", "--switch", "office"], ["clients", "--search", "phone"], ["devices", "-s", "switch"],
]


def run(fake_client, monkeypatch, capsys, argv):
    monkeypatch.setenv("CONTROLLER_URL", "https://controller.example")
    monkeypatch.setenv("API_KEY", "key")
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, c: fake_client))
    code = cli.main(["query", *argv])
    return code, capsys.readouterr().out


def parse(text):
    reader = csv.DictReader(io.StringIO(text, newline=""))
    return reader.fieldnames, list(reader)


@pytest.mark.parametrize("argv", KINDS, ids=lambda a: " ".join(a))
def test_the_csv_has_the_columns_and_rows_of_the_json(fake_client, monkeypatch, capsys, argv):
    _, as_json = run(fake_client, monkeypatch, capsys, [*argv, "--json"])
    code, as_csv = run(fake_client, monkeypatch, capsys, [*argv, "--csv"])
    expected = json.loads(as_json)
    fields, rows = parse(as_csv)
    assert code == 0 and expected                                    # the fixture has rows for every case
    assert fields == list(expected[0])
    assert len(rows) == len(expected)
    for got, want in zip(rows, expected, strict=True):
        assert got == {key: str(value) for key, value in want.items()}


def test_offline_reservations_round_trip_with_their_extra_column(fake_client, monkeypatch, capsys):
    fake_client.session.fx["legacy"]["alluser"][1]["last_seen"] = int(time.time()) - 40 * 86400    # offline for weeks
    _, as_json = run(fake_client, monkeypatch, capsys, ["reservations", "--offline", "--json"])
    _, as_csv = run(fake_client, monkeypatch, capsys, ["reservations", "--offline", "--csv"])
    expected, (fields, rows) = json.loads(as_json), parse(as_csv)
    assert expected and fields == list(expected[0]) and fields[-1] == "Offline For"
    assert rows == [{key: str(value) for key, value in want.items()} for want in expected]


def test_the_columns_are_the_documented_ones(fake_client, monkeypatch, capsys):
    assert parse(run(fake_client, monkeypatch, capsys, ["clients", "--csv"])[1])[0][-1] == "Private MAC"
    assert parse(run(fake_client, monkeypatch, capsys, ["reservations", "--offline", "--csv"])[1])[0][-1] == \
        "Offline For"
    assert parse(run(fake_client, monkeypatch, capsys, ["devices", "--csv"])[1])[0][-4:] == [
        "Firmware", "Update Available", "Uptime", "Uptime (s)"]


def test_a_result_with_no_rows_is_just_the_header(fake_client, monkeypatch, capsys):
    for argv, columns in ((["ports", "--switch", "nothing-matches"], PORT_COLUMNS),
                          (["clients", "--search", "nothing-matches"], data_columns("clients"))):
        code, out = run(fake_client, monkeypatch, capsys, [*argv, "--csv"])
        assert code == 0 and parse(out) == (columns, [])
        assert json.loads(run(fake_client, monkeypatch, capsys, [*argv, "--json"])[1]) == []


def test_port_columns_are_exactly_the_keys_of_a_port_row(fake_client):
    snap = collect_snapshot(fake_client, "default", Needs())
    assert port_rows(snap) and all(list(row) == PORT_COLUMNS for row in port_rows(snap))


def test_there_is_no_footer_and_lines_end_with_a_newline_only(fake_client, monkeypatch, capsys):
    _, out = run(fake_client, monkeypatch, capsys, ["all", "--csv"])
    assert "row(s)" not in out and "\r" not in out and out.endswith("\n") and not out.endswith("\n\n")


def test_json_and_csv_together_are_a_usage_error_before_any_request(fake_client, monkeypatch, capsys):
    with pytest.raises(SystemExit) as stop:
        run(fake_client, monkeypatch, capsys, ["clients", "--json", "--csv"])
    assert stop.value.code == cli.EXIT_USAGE
    assert "--json and --csv cannot be combined" in capsys.readouterr().err and fake_client.session.calls == []


HOSTILE = {
    "=HYPERLINK(\"http://example.invalid\",\"x\")": "'=HYPERLINK(\"http://example.invalid\",\"x\")",
    "+1+1": "'+1+1", "-2+3": "'-2+3", "@SUM(A1:A2)": "'@SUM(A1:A2)",
    "\t=1+1": "'=1+1", "\r=2+2": "'=2+2", "  =spaces": "'=spaces",
    "\x1b=1+1": "'=1+1",                    # the escape must not hide the formula from the check
    "\x1b[31m+cmd": "[31m+cmd", "\u202e=evil": "'=evil", "\u200b@zero": "'@zero",
    "plain, with comma": "plain, with comma", 'with "quotes"': 'with "quotes"',
    "two\nlines": "two lines", "tab\there": "tab here", "bell\x07": "bell",
}


def hostile_names(fake_client):
    names = list(HOSTILE)
    fx = fake_client.session.fx
    fx["clients"][0]["name"], fx["clients"][1]["name"] = names[0], names[1]
    for i, name in enumerate(names[2:], start=2):
        fx["clients"].append({"id": f"c{i}", "type": "WIRED", "macAddress": f"cc:00:00:00:00:{i:02x}",
                              "ipAddress": f"10.0.9.{i}", "name": name, "connectedAt": "2026-01-01T00:00:00Z"})
    return names


def test_names_that_a_spreadsheet_would_run_are_neutralised_and_the_rest_survive(fake_client, monkeypatch, capsys):
    names = hostile_names(fake_client)
    _, out = run(fake_client, monkeypatch, capsys, ["clients", "--csv"])
    for bad in ("\x1b", "\x07", "\u202e", "\u200b", "\r", "\x00"):
        assert bad not in out, repr(bad)
    _, rows = parse(out)
    shown = {r["MAC Address"]: r["Name"] for r in rows}
    assert len(rows) == len(names)
    for name in names:
        assert HOSTILE[name] in shown.values(), name
    for cell in (value for row in rows for value in row.values()):
        assert not cell.startswith(("=", "+", "-", "@", "\t", "\r")), cell


def test_a_name_with_a_comma_or_quotes_round_trips_through_the_csv_quoting(fake_client, monkeypatch, capsys):
    hostile_names(fake_client)
    _, out = run(fake_client, monkeypatch, capsys, ["clients", "--csv"])
    assert '"plain, with comma"' in out and '"with ""quotes"""' in out
    assert [r["Name"] for r in parse(out)[1]].count("plain, with comma") == 1


def test_hostile_names_in_every_kind_are_neutralised(fake_client, monkeypatch, capsys):
    fx = fake_client.session.fx
    fx["devices"][1]["name"] = "\x1b=1+1"
    fx["legacy"]["device"][1]["name"] = "\x1b=1+1"
    fx["legacy"]["alluser"][1]["hostname"] = "=cmd|' /C calc'!A0"
    fx["legacy"]["alluser"][0]["name"] = "@evil"
    for argv in (["devices"], ["reservations"], ["ports"], ["all"], ["clients", "--include-offline"]):
        _, out = run(fake_client, monkeypatch, capsys, [*argv, "--csv"])
        for row in parse(out)[1]:
            assert not any(cell.startswith(("=", "+", "-", "@")) for cell in row.values()), (argv, row)
        assert "\x1b" not in out


def test_csv_cell_cleans_then_checks_and_leaves_numbers_alone():
    assert csv_cell("\x1b=1") == "'=1" and csv_cell("=1") == "'=1" and csv_cell("a\nb") == "a b"
    assert csv_cell("-67") == "'-67"                         # a string that looks numeric is still text
    assert csv_cell(-67) == -67 and csv_cell(0.5) == 0.5 and csv_cell(None) is None and csv_cell(True) is True
    assert csv_cell("") == "" and csv_cell("Office AP") == "Office AP"


def test_render_csv_fills_missing_cells_and_ignores_extra_keys():
    text = render_csv([{"Name": "a", "extra": "ignored"}, {}], "clients")
    fields, rows = parse(text)
    assert fields == data_columns("clients") and rows[0]["Name"] == "a" and rows[1]["Name"] == ""
    assert "ignored" not in text


def test_negative_numbers_in_a_numeric_column_are_not_prefixed(fake_client, monkeypatch, capsys):
    fake_client.session.fx["legacy"]["device"][1]["port_table"][0]["rx_errors"] = -5
    _, out = run(fake_client, monkeypatch, capsys, ["ports", "--csv"])
    assert "-5" in [r["RX Errors"] for r in parse(out)[1]] and "'-5" not in out
