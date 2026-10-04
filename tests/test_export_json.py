"""`export --format json`: the CSV export's data as one JSON file (issue #139)."""

import copy
import csv
import json
from pathlib import Path

import pytest
from docs_support import ROOT

from homelab_probe import cli
from homelab_probe.export import EXPORT_FORMATS, JSON_FILENAME, JSON_VERSION

HOSTILE = "\x1b[31m\x07\n[CRITICAL] forged: all clear\u202e"


def run(fake_client, monkeypatch, capsys, *argv):
    monkeypatch.setenv("UNIFI_URL", "https://controller.example")
    monkeypatch.setenv("UNIFI_API_KEY", "key")
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, c: fake_client))
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def export(fake_client, monkeypatch, capsys, tmp_path, *extra, fmt="json"):
    code, out, err = run(fake_client, monkeypatch, capsys, "export", "--format", fmt, "-o", str(tmp_path), *extra)
    assert code == 0, err
    return out


def document(tmp_path):
    return json.loads((tmp_path / JSON_FILENAME).read_text(encoding="utf-8"))


def csv_rows(path):
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# -- the document ----------------------------------------------------------------------------------------------

def test_the_document_has_a_version_first_then_devices_clients_and_switches(fake_client, monkeypatch, capsys, tmp_path):
    export(fake_client, monkeypatch, capsys, tmp_path)
    doc = document(tmp_path)
    assert list(doc) == ["version", "devices", "clients", "switches"] and doc["version"] == JSON_VERSION == 1
    assert [d["Name"] for d in doc["devices"]] == ["Gateway", "Office Switch", "Office AP", "Garage AP"]
    assert all(d["Type"].startswith("Device") for d in doc["devices"])
    assert [c["Name"] for c in doc["clients"]] == ["desktop", "phone"] and all(c["Type"] == "Client" for c in doc["clients"])
    (switch,) = doc["switches"]
    assert (switch["name"], switch["mac"], len(switch["ports"])) == ("Office Switch", "AA:00:00:00:00:02", 4)
    assert list(switch) == ["name", "mac", "ports"]


def test_it_is_the_data_of_the_csv_files(fake_client, monkeypatch, capsys, tmp_path):
    """Every cell of every row is the cell of the CSV file (a number is the same text); nothing else is added."""
    export(fake_client, monkeypatch, capsys, tmp_path / "csv", fmt="csv")
    export(fake_client, monkeypatch, capsys, tmp_path / "json")
    doc = document(tmp_path / "json")
    inventory = csv_rows(tmp_path / "csv" / "unifi_clients.csv")
    rows = doc["devices"] + doc["clients"]
    assert len(rows) == len(inventory)
    as_text = lambda row: {key: str(value) for key, value in row.items()}           # noqa: E731
    assert sorted(map(as_text, rows), key=lambda r: r["MAC Address"]) == \
        sorted(inventory, key=lambda r: r["MAC Address"])
    (switch,) = doc["switches"]
    assert [as_text(p) for p in switch["ports"]] == csv_rows(tmp_path / "csv" / "switch_Office Switch.csv")


def test_include_offline_adds_the_offline_clients_last_and_matches_the_csv(fake_client, monkeypatch, capsys, tmp_path):
    export(fake_client, monkeypatch, capsys, tmp_path / "csv", "--include-offline", fmt="csv")
    export(fake_client, monkeypatch, capsys, tmp_path / "json", "--include-offline")
    doc = document(tmp_path / "json")
    inventory = csv_rows(tmp_path / "csv" / "unifi_clients.csv")
    assert len(doc["devices"]) + len(doc["clients"]) == len(inventory) > 6
    assert [c["Status"] for c in doc["clients"]][-1] == "Offline" and "old-printer" in {c["Name"] for c in doc["clients"]}
    plain = tmp_path / "plain"
    export(fake_client, monkeypatch, capsys, plain)
    assert len(document(plain)["clients"]) == 2                                      # not without the option


def test_numbers_stay_numbers_in_the_ports(fake_client, monkeypatch, capsys, tmp_path):
    export(fake_client, monkeypatch, capsys, tmp_path)
    port = document(tmp_path)["switches"][0]["ports"][0]
    assert isinstance(port["Port Index"], int) and isinstance(port["RX Bytes"], int) and port["Status"] == "Up"


def test_a_switch_with_no_port_table_is_left_out_and_two_with_one_name_are_two_entries(fake_client, monkeypatch,
                                                                                       capsys, tmp_path):
    devices = fake_client.session.fx["legacy"]["device"]
    twin = copy.deepcopy(devices[1])
    twin["mac"] = "aa:00:00:00:00:77"
    devices.append(twin)
    bare = copy.deepcopy(devices[1])
    bare.update(mac="aa:00:00:00:00:78", name="Empty Switch", port_table=[])
    devices.append(bare)
    export(fake_client, monkeypatch, capsys, tmp_path)
    switches = document(tmp_path)["switches"]
    assert [(s["name"], s["mac"]) for s in switches] == [("Office Switch", "AA:00:00:00:00:02"),
                                                          ("Office Switch", "AA:00:00:00:00:77")]


def test_the_message_says_what_was_written_where(fake_client, monkeypatch, capsys, tmp_path):
    out = export(fake_client, monkeypatch, capsys, tmp_path)
    assert "Site: Default (site-1)" in out and "Found 4 device(s), 2 connected client(s)" in out
    assert f"Wrote 4 device(s), 2 client(s) and 1 switch(es) -> {tmp_path / JSON_FILENAME}" in out


# -- the files -----------------------------------------------------------------------------------------------

def test_json_writes_only_its_file_and_leaves_the_csv_files_alone(fake_client, monkeypatch, capsys, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "unifi_clients.csv").write_text("old,content\n", encoding="utf-8")
    export(fake_client, monkeypatch, capsys, out)
    assert sorted(p.name for p in out.iterdir()) == ["unifi_clients.csv", JSON_FILENAME]
    assert (out / "unifi_clients.csv").read_text(encoding="utf-8") == "old,content\n"
    export(fake_client, monkeypatch, capsys, tmp_path / "csv-only", fmt="csv")
    assert not (tmp_path / "csv-only" / JSON_FILENAME).exists()


def test_the_default_is_csv_and_its_files_are_byte_for_byte_those_of_an_explicit_csv(fake_client, monkeypatch,
                                                                                 capsys, tmp_path):
    code, _, _ = run(fake_client, monkeypatch, capsys, "export", "-o", str(tmp_path / "default"))
    assert code == 0
    export(fake_client, monkeypatch, capsys, tmp_path / "explicit", fmt="csv")
    names = sorted(p.name for p in (tmp_path / "default").iterdir())
    assert names == sorted(p.name for p in (tmp_path / "explicit").iterdir()) == ["switch_Office Switch.csv",
                                                                                   "unifi_clients.csv"]
    for name in names:
        assert (tmp_path / "default" / name).read_bytes() == (tmp_path / "explicit" / name).read_bytes()


def test_the_output_directory_is_created_and_the_file_ends_with_a_newline(fake_client, monkeypatch, capsys, tmp_path):
    target = tmp_path / "a" / "b"
    export(fake_client, monkeypatch, capsys, target)
    assert (target / JSON_FILENAME).read_text(encoding="utf-8").endswith("}\n")


def test_a_second_export_replaces_the_file(fake_client, monkeypatch, capsys, tmp_path):
    (tmp_path / JSON_FILENAME).write_text("not json", encoding="utf-8")
    export(fake_client, monkeypatch, capsys, tmp_path)
    assert document(tmp_path)["version"] == 1


def test_a_file_that_cannot_be_written_is_an_error_not_a_traceback(fake_client, monkeypatch, capsys, tmp_path):
    (tmp_path / JSON_FILENAME).mkdir()                                  # a directory is in the way
    code, _, err = run(fake_client, monkeypatch, capsys, "export", "--format", "json", "-o", str(tmp_path))
    assert code == 3 and "ERROR:" in err and str(tmp_path / JSON_FILENAME) in err and "Traceback" not in err


def test_the_file_is_git_ignored_like_the_csv_files_because_it_holds_real_addresses():
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert JSON_FILENAME in ignored and "*.csv" in ignored


# -- untrusted text --------------------------------------------------------------------------------------------

def hostile(fake_client):
    fake_client.session.fx["clients"][0]["name"] = "=HYPERLINK(\"http://x\")" + HOSTILE
    fake_client.session.fx["legacy"]["sta"][0]["name"] = "=HYPERLINK(\"http://x\")" + HOSTILE
    fake_client.session.fx["devices"][1]["name"] = "+Switch é"
    fake_client.session.fx["legacy"]["device"][1]["name"] = "+Switch é"


def test_values_are_raw_in_json_and_guarded_in_csv(fake_client, monkeypatch, capsys, tmp_path):
    hostile(fake_client)
    export(fake_client, monkeypatch, capsys, tmp_path / "json")
    export(fake_client, monkeypatch, capsys, tmp_path / "csv", fmt="csv")
    doc = document(tmp_path / "json")
    names = [c["Name"] for c in doc["clients"]]
    assert names[0].startswith('=HYPERLINK("http://x")') and doc["devices"][1]["Name"] == "+Switch é"   # not changed
    assert doc["switches"][0]["name"] == "+Switch é"
    csv_names = [r["Name"] for r in csv_rows(tmp_path / "csv" / "unifi_clients.csv")]
    assert all(not n.startswith(("=", "+")) for n in csv_names) and any(n.startswith("'") for n in csv_names)


def test_control_characters_are_escaped_by_json_and_survive_decoding(fake_client, monkeypatch, capsys, tmp_path):
    hostile(fake_client)
    export(fake_client, monkeypatch, capsys, tmp_path)
    text = (tmp_path / JSON_FILENAME).read_text(encoding="utf-8")
    for raw in ("\x1b", "\x07", "\u202e"):
        assert raw not in text, repr(raw)
    assert "\\u001b" in text and "\\n[CRITICAL]" in text
    assert "\x1b" in document(tmp_path)["clients"][0]["Name"]               # raw after decoding, as in every --json


def test_the_names_printed_to_the_terminal_are_still_cleaned(fake_client, monkeypatch, capsys, tmp_path):
    fake_client.session.fx["info"]["applicationVersion"] = "10.0.0"
    fake_client.session.fx["sites"][0]["name"] = "Home" + HOSTILE
    out = export(fake_client, monkeypatch, capsys, tmp_path)
    assert "\x1b" not in out and "\u202e" not in out and "Site: Home" in out


# -- the command line ------------------------------------------------------------------------------------------

def test_the_formats_are_csv_and_json_and_csv_is_the_default():
    assert EXPORT_FORMATS == ("csv", "json")
    args = cli.build_parser().parse_args(["export"])
    assert args.format == "csv"


def test_an_unknown_format_is_a_usage_error(monkeypatch, capsys):
    monkeypatch.setenv("UNIFI_URL", "https://controller.example")
    monkeypatch.setenv("UNIFI_API_KEY", "key")
    with pytest.raises(SystemExit) as stopped:
        cli.main(["export", "--format", "xml"])
    assert stopped.value.code == 64 and "invalid choice" in capsys.readouterr().err


def test_the_help_names_both_formats_and_the_file(capsys):
    with pytest.raises(SystemExit):
        cli.main(["export", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "--format {csv,json}" in text and JSON_FILENAME in text
    assert Path(JSON_FILENAME).suffix == ".json"
