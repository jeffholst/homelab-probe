"""Names come from devices on the network, so they are untrusted when printed or exported."""

import argparse
import csv
import json

import pytest

from homelab_probe import cli
from homelab_probe.diagnose import Finding, format_findings, format_ignored
from homelab_probe.export import run_export
from homelab_probe.query import format_table
from homelab_probe.settings import IgnoreRule
from homelab_probe.snapshot import Needs, collect_snapshot
from homelab_probe.util import clean_data, csv_safe, printable, safe_output

# An escape sequence, a bell, a line break that forges a finding, and a bidi override.
HOSTILE = "\x1b[31m\x07\n[CRITICAL] forged: all clear\u202e"
NAME_KEYS = {"name", "hostname", "essid", "gw_name", "isp_name"}


def poison(value):
    """Append HOSTILE to every name-like text value, everywhere in a fixture."""
    if isinstance(value, dict):
        return {k: (v + HOSTILE if k in NAME_KEYS and isinstance(v, str) and v else poison(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [poison(v) for v in value]
    return value


def run(fake_client, monkeypatch, argv):
    monkeypatch.setenv("UNIFI_URL", "https://controller")
    monkeypatch.setenv("UNIFI_API_KEY", "key")
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, c: fake_client))
    return cli.main(argv)


# -- printable / safe_output / csv_safe / clean_data ---------------------------

def test_printable_flattens_whitespace_and_removes_controls():
    assert printable("a\tb\r\nc\x1b[2Jd\x07e\x00f\x7fg\x9bh") == "a b c[2Jdefgh"
    assert printable("line\u2028break\u0085next") == "line break next"
    assert printable("  padded  ") == "padded"


def test_printable_removes_bidi_overrides_but_keeps_real_text():
    assert printable("evil\u202etxt.exe\u2066x\u2069") == "eviltxt.exex"
    assert printable("Café ☕ 日本語 שלום مرحبا") == "Café ☕ 日本語 שלום مرحبا"
    assert printable("👨\u200d👩\u200d👧 home") == "👨\u200d👩\u200d👧 home"      # emoji joiner kept


def test_printable_keeps_weak_direction_marks_and_the_non_joiner():
    assert printable("שלום\u200f (1)") == "שלום\u200f (1)"                       # RLM
    assert printable("a\u200eb\u061cc") == "a\u200eb\u061cc"                    # LRM, ALM
    assert printable("می\u200cخواهم") == "می\u200cخواهم"                         # Persian non-joiner


@pytest.mark.parametrize("hidden", ["\u200b", "\u2060", "\u2063", "\ufeff"])
def test_printable_removes_invisible_characters_so_lookalike_names_match(hidden):
    assert printable(f"print{hidden}er") == "printer" == printable("printer")
    assert safe_output(f"print{hidden}er") == "printer"


def test_printable_handles_non_text_and_limit():
    assert printable(None) == "" and printable(0) == "0" and printable(["x"]) == "['x']"
    assert printable("x" * 10, limit=5) == "xxxx…"
    assert printable("short", limit=5) == "short"


def test_printable_and_safe_output_agree_on_what_is_dangerous():
    text = "a\x1b[0mb\u202ec\u200bd"
    assert safe_output(text) == printable(text) == "a[0mbcd"


def test_safe_output_keeps_lines_and_drops_escapes():
    assert safe_output("one\ntwo\x1b[0m\r\nthree\tcol\u202e") == "one\ntwo[0m\nthree col"


@pytest.mark.parametrize("text", ["=1+1", "+1", "-1", "@SUM(A1)", "\tx", "\rx"])
def test_csv_safe_prefixes_formula_starts(text):
    assert csv_safe(text) == "'" + text


@pytest.mark.parametrize("value", ["desktop", "", "a=b", " =x", "'quoted", 5, -5, 1.5, None, True])
def test_csv_safe_leaves_everything_else_alone(value):
    assert csv_safe(value) == value


def test_csv_safe_treats_numeric_looking_text_as_text():
    # a column of numbers must hold numbers, or negative values gain an apostrophe
    assert csv_safe("-67") == "'-67" and csv_safe(-67) == -67


def test_clean_data_cleans_every_text_leaf_and_copies():
    original = {"a": "x\ny", "b": ["\x1b[1mz", ("t\r", 3)], "c": {"d": None, "e": 2.5}}
    cleaned = clean_data(original)
    assert cleaned == {"a": "x y", "b": ["[1mz", ("t", 3)], "c": {"d": None, "e": 2.5}}
    assert original["a"] == "x\ny"                       # the input is not modified


# -- tables and findings --------------------------------------------------------

def test_format_table_cells_cannot_break_rows_or_carry_controls():
    text = format_table([{"Name": "a" + HOSTILE, "Other": "ok"}], ["Name", "Other"])
    assert len(text.splitlines()) == 3 and all("\x1b" not in line for line in text.splitlines())
    assert "\u202e" not in text and "\x07" not in text
    assert not any(line.startswith("[CRITICAL]") for line in text.splitlines())


def test_format_findings_cannot_forge_a_finding_line():
    text = format_findings([Finding("WARNING", "host" + HOSTILE, "msg" + HOSTILE)], emoji=False)
    assert not any(line.startswith("[CRITICAL]") for line in text.splitlines())
    assert "\x1b" not in text and "\u202e" not in text
    ignored = format_ignored([(Finding("INFO", "host" + HOSTILE, "m"), IgnoreRule(reason="r" + HOSTILE))])
    assert "\x1b" not in ignored and "\n[CRITICAL]" not in ignored


# -- every command's text output, with hostile names ----------------------------

COMMANDS = [
    ["info"], ["doctor"],
    ["query", "devices"], ["query", "clients", "--include-offline"], ["query", "ports"],
    ["query", "reservations"], ["query", "networks"], ["query", "wlans"],
    ["query", "networks", "--csv"], ["query", "wlans", "--csv"],
    ["query", "clients", "--include-offline", "--csv"], ["query", "devices", "--csv"], ["query", "ports", "--csv"],
    ["query", "reservations", "--csv"],
    ["new-clients"],
    ["events"], ["events", "--summary"],
    ["wifi", "--all"],
    ["wan"],
    ["firewall", "--all", "--zones", "--no-emoji"], ["firewall", "--zones"],
    ["audit", "--no-emoji"], ["audit"],
    ["topology", "--clients", "--no-emoji"], ["topology", "--clients"],
    ["client", "desktop", "--no-emoji"], ["client", "desktop"],
    ["diagnose", "--no-emoji", "--show-ignored"], ["diagnose"],
    ["diagnose", "--notify", "--notify-dry-run"],
]


def test_every_subcommand_has_a_hostile_name_case():
    """A new subcommand must be added to COMMANDS (or to the list below with the reason)."""
    parser = cli.build_parser()
    subcommands = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    covered = {argv[0] for argv in COMMANDS} | {"export", "snapshot", "diff", "completion", "web-user", "serve", "init"}   # their own tests; completion prints no controller data, web-user only usernames that the accounts module has checked, serve only fixed text
    assert set(subcommands.choices) <= covered, set(subcommands.choices) - covered


def assert_clean(text, argv):
    for bad in ("\x1b", "\x07", "\u202e", "\x9b"):
        assert bad not in text, f"{bad!r} reached the output of {argv}"
    for line in text.splitlines():
        assert not line.lstrip().startswith("[CRITICAL] forged"), f"forged line in {argv}: {line!r}"


@pytest.mark.parametrize("argv", COMMANDS, ids=lambda a: " ".join(a))
def test_text_output_is_clean_with_hostile_names(fake_client, monkeypatch, capsys, argv):
    fake_client.session.fx = poison(fake_client.session.fx)
    fake_client.session.events = poison(fake_client.session.events)
    run(fake_client, monkeypatch, argv)
    captured = capsys.readouterr()
    assert_clean(captured.out + captured.err, argv)
    assert captured.out.strip(), f"{argv} printed nothing"
    shown = {"query devices": "Office Switch", "query clients": "desktop", "query ports": "Office", "query networks": "Main", "query wlans": "HomeNet",
             "new-clients": "old-printer", "topology": "Office Switch", "client": "desktop",
             "wifi": "Office AP", "events": "phone", "firewall": "Open Inbound", "audit": "Lobby"}.get(" ".join(argv[:2]) if argv[0] == "query" else argv[0])
    if shown:
        assert shown in captured.out, f"{shown!r} missing from {argv}"


def test_names_are_still_shown_without_the_dangerous_parts(fake_client, monkeypatch, capsys):
    fake_client.session.fx = poison(fake_client.session.fx)
    run(fake_client, monkeypatch, ["query", "clients"])
    out = capsys.readouterr().out
    assert "desktop[31m [CRITICAL] forged: all clear" in out


def test_events_json_stays_raw_but_escaped(fake_client, monkeypatch, capsys):
    fake_client.session.events = poison(fake_client.session.events)
    run(fake_client, monkeypatch, ["events", "--json"])
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\n[CRITICAL]" not in out        # json escapes both
    assert "\\u001b" in out and any("\x1b" in e["Message"] for e in json.loads(out))   # raw after decoding


@pytest.mark.parametrize("argv", [["wifi", "--json"], ["topology", "--json"], ["wan", "--json"],
                                  ["query", "clients", "--json"], ["client", "desktop", "--json"]],
                         ids=lambda a: " ".join(a))
def test_json_output_is_valid_and_has_no_raw_control_characters(fake_client, monkeypatch, capsys, argv):
    fake_client.session.fx = poison(fake_client.session.fx)
    run(fake_client, monkeypatch, argv)
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\u202e" not in out
    json.loads(out)


def test_the_json_export_is_valid_escaped_and_clean_on_the_terminal(fake_client, monkeypatch, capsys, tmp_path):
    fake_client.session.fx = poison(fake_client.session.fx)
    run(fake_client, monkeypatch, ["export", "--format", "json", "-o", str(tmp_path)])
    captured = capsys.readouterr()
    assert_clean(captured.out + captured.err, ["export", "--format", "json"])
    text = (tmp_path / "unifi_inventory.json").read_text(encoding="utf-8")
    assert "\x1b" not in text and "\u202e" not in text and "\\u001b" in text      # JSON escapes them itself
    assert "\x1b" in json.loads(text)["clients"][0]["Name"]                         # raw once decoded, like every --json


def test_unmatched_client_query_is_not_echoed_raw(fake_client, monkeypatch, capsys):
    assert run(fake_client, monkeypatch, ["client", "\x1b[2Jnobody\nx"]) == cli.EXIT_NO_MATCH
    assert_clean(capsys.readouterr().err, "client")


def test_diff_and_snapshot_output_is_clean(fake_client, monkeypatch, capsys, tmp_path):
    folder = str(tmp_path / "snaps")
    assert run(fake_client, monkeypatch, ["snapshot", "--dir", folder]) == 0
    fake_client.session.fx = poison(fake_client.session.fx)
    assert run(fake_client, monkeypatch, ["diff", "--dir", folder, "--all"]) == 0
    captured = capsys.readouterr()
    assert_clean(captured.out + captured.err, "diff")
    assert "Clients" in captured.out or "Devices" in captured.out


def test_warnings_and_errors_are_clean(fake_client, monkeypatch, capsys):
    from homelab_probe.snapshot import warn
    warn("bad" + HOSTILE)
    assert_clean(capsys.readouterr().err, "warn")


# -- CSV export -------------------------------------------------------------------

FORMULAS = ['=HYPERLINK("http://example.invalid","x")', "+1+1", "-2+3", "@SUM(A1)", "\t=1+1"]


def read_rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_export_neutralises_formulas_in_every_text_column(fake_client, tmp_path):
    fx = fake_client.session.fx
    for client, formula in zip(fx["clients"], FORMULAS, strict=False):
        client["name"] = formula
    fx["devices"][1]["name"] = FORMULAS[2]
    for sta in fx["legacy"]["sta"]:
        sta["name"] = FORMULAS[3]
    run_export(collect_snapshot(fake_client, "default", Needs(offline=True)), tmp_path)

    files = sorted(tmp_path.glob("*.csv"))
    assert len(files) >= 2
    for path in files:
        for row in read_rows(path):
            for column, cell in row.items():
                assert not cell.startswith(("=", "+", "-", "@", "\t", "\r")), (path.name, column, cell)
    names = {r["Name"] for r in read_rows(tmp_path / "unifi_clients.csv")}
    assert "'" + FORMULAS[0] in names and "'" + FORMULAS[1] in names


def test_export_leaves_ordinary_cells_unchanged(fake_client, tmp_path):
    run_export(collect_snapshot(fake_client, "default", Needs(offline=True)), tmp_path)
    rows = {r["Name"]: r for r in read_rows(tmp_path / "unifi_clients.csv")}
    assert rows["desktop"]["Switch"] == "Office Switch" and rows["desktop"]["Port"] == "3"
    assert not any(cell.startswith("'") for r in rows.values() for cell in r.values())


def test_export_prints_a_clean_site_name(fake_client, tmp_path, capsys):
    fake_client.session.fx["sites"][0]["name"] = "Home" + HOSTILE
    run_export(collect_snapshot(fake_client, "default", Needs(offline=True)), tmp_path)
    assert_clean(capsys.readouterr().out, "export")
