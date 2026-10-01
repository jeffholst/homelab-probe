import json

import pytest

from unifi_sentinel import cli
from unifi_sentinel.client import UniFiAPIError
from unifi_sentinel.snapshot import Snapshot, collect_snapshot
from unifi_sentinel.wifi import (DEFAULT_MIN_SIGNAL, NAMES_PER_CHANNEL, build_wifi, overlaps, own_bssids,
                                 parse_band, radios, render_text, span_mhz, to_json, unique_neighbors)
from unifi_sentinel.client_view import DeviceIndex


# -- spectrum -------------------------------------------------------------------

def test_2_4_ghz_spans_overlap_when_channels_are_closer_than_five_apart():
    span = lambda ch: span_mhz("ng", ch, 20)
    assert span(1) == (2401, 2423) and span(6) == (2426, 2448)
    assert not overlaps(span(1), span(6)) and not overlaps(span(6), span(11)) and not overlaps(span(1), span(11))
    for adjacent in (2, 3, 4, 5):
        assert overlaps(span(1), span(adjacent)), adjacent                      # 2-5 all overlap channel 1
    assert overlaps(span(1), span(4)) and overlaps(span(6), span(4)) and overlaps(span(6), span(8))
    assert overlaps(span(6), span(6))                                           # co-channel overlaps itself


def test_5_ghz_widths_occupy_whole_blocks():
    assert span_mhz("na", 36, 20) == (5170, 5190)
    assert span_mhz("na", 40, 40) == span_mhz("na", 36, 40) == (5170, 5210)     # channels 36 and 40 together
    assert span_mhz("na", 36, 80) == span_mhz("na", 48, 80) == (5170, 5250)     # the 36-48 block
    assert span_mhz("na", 161, 80) == (5735, 5815)
    assert span_mhz("na", 100, 160) == (5490, 5650)
    assert overlaps(span_mhz("na", 36, 80), span_mhz("na", 44, 20))             # inside our block
    assert not overlaps(span_mhz("na", 36, 80), span_mhz("na", 52, 20))         # the next block
    assert span_mhz("na", 165, 80) == (5785, 5865)                              # not in a table: centred fallback


def test_6_ghz_blocks_and_unknowns():
    assert span_mhz("6e", 1, 20) == (5945, 5965)
    lo, hi = span_mhz("6e", 37, 160)
    assert span_mhz("6e", 33, 160) == span_mhz("6e", 61, 160) == (lo, hi)         # one 160 MHz block: channels 33-61
    assert hi - lo == 160 and lo == 5950 + 5 * 33 - 10 and hi == 5950 + 5 * 61 + 10
    assert span_mhz("6e", 1, 160)[0] == 5945 and span_mhz("6e", 65, 160)[0] == lo + 160
    assert span_mhz("ng", None, 20) is None and span_mhz("xx", 1, 20) is None and span_mhz("ng", "x", 20) is None
    assert not overlaps(None, (1, 2)) and not overlaps((1, 2), None)
    assert not overlaps((1, 2), (2, 3))                                         # touching is not overlapping


def test_parse_band():
    assert [parse_band(t) for t in ("2.4", "2.4 GHz", "24", "5", "5ghz", "6", "ng", "NA", "6e")] == [
        "ng", "ng", "ng", "na", "na", "6e", "ng", "na", "6e"]
    with pytest.raises(ValueError, match="invalid band"):
        parse_band("7")


# -- neighbors --------------------------------------------------------------------

def row(bssid="02:aa:00:00:00:01", ap="aa:03", band="ng", ch=6, sig=-60, name="Net", **kw):
    return {"bssid": bssid, "ap_mac": ap, "band": band, "channel": ch, "signal": sig, "essid": name,
            "security": "WPA2", "bw": 20, "center_freq": None if ch is None else 2407 + 5 * ch, **kw}


def snap_with(rows, devices=(), clients=()):
    return Snapshot(site={}, devices=[], clients=[], neighbors=list(rows), legacy_devices=list(devices))


def test_one_network_heard_by_several_aps_counts_once_with_its_strongest_reading():
    got = unique_neighbors(snap_with([row(ap="aa:03", sig=-70), row(ap="aa:04", sig=-45), row(ap="AA:05", sig=-60)]))
    assert len(got) == 1
    assert got[0]["signal"] == -45 and got[0]["seen_by"] == ["aa:03", "aa:04", "aa:05"]
    assert got[0]["name"] == "Net" and got[0]["span"] == (2426, 2448)


def test_our_own_networks_invalid_rows_and_odd_data_are_skipped():
    ours = {"mac": "AA:03", "type": "uap", "vap_table": [{"bssid": "02:00:5E:00:00:01"}, None, {"x": 1}]}
    rows = [row(bssid="02:00:5e:00:00:01", sig=-30),                            # one of our VAPs (any case)
            row(bssid="aa:03", sig=-30),                                        # one of our device MACs
            row(bssid="02:aa:00:00:00:02", sig=None), {"ap_mac": "x"}, {"bssid": "02:aa:00:00:00:03"},
            row(bssid="02:aa:00:00:00:04", sig="strong"),
            row(bssid="02:aa:00:00:00:05", sig=-80)]
    s = snap_with(rows, [ours])
    assert own_bssids(s) == {"aa:03", "02:00:5e:00:00:01"}
    assert [n["bssid"] for n in unique_neighbors(s)] == ["02:aa:00:00:00:05"]


def test_names_are_cleaned_hidden_names_stay_empty_and_widths_default():
    got = {n["bssid"]: n for n in unique_neighbors(snap_with([
        row(bssid="b1", name="Line\nBreak\x07 "), row(bssid="b2", name=None), row(bssid="b3", name="x" * 60),
        row(bssid="b4", name="Café ☕", bw=None, center_freq=None, security="Open", oui="Acme"),
        row(bssid="b5", ch=None)]))}
    assert got["b1"]["name"] == "LineBreak" and got["b2"]["name"] == ""
    assert len(got["b3"]["name"]) == 40 and got["b3"]["name"].endswith("…")
    assert got["b4"]["name"] == "Café ☕" and got["b4"]["open"] and got["b4"]["vendor"] == "Acme"
    assert got["b4"]["span"] == (2426, 2448)                                      # computed when center_freq is missing
    assert got["b5"]["channel"] is None and got["b5"]["span"] is None             # unknown channel: no overlap claims


# -- radios -----------------------------------------------------------------------

def ap(mac, name, stats=None, **kw):
    return {"mac": mac, "type": "uap", "name": name, "radio_table_stats": stats, **kw}


def test_radio_rows_unknown_satisfaction_offline_aps_and_odd_values():
    stats = [{"radio": "na", "channel": 36, "bw": 80, "tx_power": 26, "num_sta": 3, "cu_total": 10,
              "tx_retries_pct": 2.5, "satisfaction": -1},
             {"radio": "ng", "channel": 6.0, "bw": "wide", "tx_power": None, "satisfaction": 98},
             {"radio": "6e", "channel": None}, "junk"]
    s = Snapshot(site={}, devices=[{"id": "x", "macAddress": "aa:09", "name": "Off AP", "state": "OFFLINE"}],
                 clients=[], legacy_devices=[ap("AA:01", "B AP", stats), ap("AA:09", "Off AP", []),
                                             {"mac": "AA:02", "type": "usw", "name": "Switch"}])
    rows = radios(s, DeviceIndex(s))
    assert [(r["ap"], r["band"]) for r in rows] == [("B AP", "ng"), ("B AP", "na"), ("B AP", "6e"), ("Off AP", "")]
    ng, na, six, off = rows
    assert ng["channel"] == 6 and ng["width"] is None and ng["tx_power"] is None and ng["satisfaction"] == 98
    assert na["satisfaction"] is None and na["span"] == (5170, 5250)             # -1 means unknown
    assert six["channel"] is None and six["span"] is None
    assert off["online"] is False and off["channel"] is None                      # placeholder, not hidden


# -- the report -------------------------------------------------------------------

@pytest.fixture
def report(fake_client):
    return build_wifi(collect_snapshot(fake_client, "default", include_neighbors=True))


def channel(report, band, ch):
    return next(c for b in report["plan"] if b["band"] == band for c in b["channels"] if c["channel"] == ch)


def test_fixture_counts_dedupe_and_strong_neighbors(report):
    assert report["neighbors"] == {"total": 9, "strong": 8, "open": 1, "hidden": 1}      # 13 rows, 9 networks
    assert (channel(report, "ng", 6)["neighbors"], channel(report, "ng", 6)["strong"]) == (3, 3)
    assert (channel(report, "ng", 1)["neighbors"], channel(report, "ng", 1)["strong"]) == (1, 0)   # the weak one
    assert channel(report, "ng", 6)["our_radios"] == ["Office AP"] and channel(report, "ng", 11)["our_radios"] == []
    names = [n["name"] for n in channel(report, "ng", 6)["strong_networks"]]
    assert names == ["Neighbor One", "", "Café Guest ☕"]                                 # strongest first
    assert [r["ap"] for r in report["radios"]] == ["Garage AP", "Office AP", "Office AP"]   # sorted by name
    assert report["ap_matched"] is True


def test_the_min_signal_cutoff_changes_what_is_strong(fake_client):
    snap = collect_snapshot(fake_client, "default", include_neighbors=True)
    loose = build_wifi(snap, min_signal=-95)
    assert loose["neighbors"]["strong"] == 9 and len(channel(loose, "ng", 1)["strong_networks"]) == 1
    tight = build_wifi(snap, min_signal=-55)
    assert tight["neighbors"]["strong"] == 3 and tight["neighbors"]["total"] == 9      # still counted
    assert DEFAULT_MIN_SIGNAL == -80
    edge = build_wifi(snap, min_signal=-60)                                            # a -60 neighbor is "at" the cutoff
    assert "Adjacent Net" in [n["name"] for n in channel(edge, "ng", 4)["strong_networks"]]


def test_observations_use_overlap_not_just_the_channel_number(report):
    obs = report["observations"]
    assert "Office AP 2.4 GHz (channel 6): 3 neighbors stronger than -80 dBm on the same channel, " \
           "1 overlapping it" in obs                                                   # channel 4 overlaps 6
    assert "Office AP 5 GHz (channel 36): 0 neighbors stronger than -80 dBm on the same channel, " \
           "1 overlapping it" in obs                                                   # channel 44 is in the 80 MHz block
    assert any("Of the usual 2.4 GHz channels, channel 1 overlaps the fewest neighbors "
               "(channel 1: 1, channel 6: 4, channel 11: 2)" in o for o in obs)


def snap_two_aps(neighbors=()):
    stats = lambda ch, band="ng", bw=20: [{"radio": band, "channel": ch, "bw": bw}]
    devices = [ap("AA:01", "Upper", stats(6)), ap("AA:02", "Lower", stats(6)),
               ap("AA:03", "Hall", stats(4)), ap("AA:04", "Quiet", stats(11))]
    return Snapshot(site={}, devices=[], clients=[], legacy_devices=devices, neighbors=list(neighbors))


def test_our_own_radios_competing_with_each_other_are_called_out():
    obs = build_wifi(snap_two_aps())["observations"]
    assert "Lower and Upper both use 2.4 GHz channel 6, so they compete with each other" in obs
    assert "Hall and Upper use overlapping 2.4 GHz channels 4 and 6, so they compete with each other" in obs
    assert not any("Quiet" in o and "compete" in o for o in obs)                       # channel 11 is clear of 6
    five = Snapshot(site={}, devices=[], clients=[], legacy_devices=[
        ap("AA:01", "A", [{"radio": "na", "channel": 36, "bw": 80}]), ap("AA:02", "B", [{"radio": "na", "channel": 100, "bw": 80}]),
        ap("AA:03", "C", [{"radio": "na", "channel": 44, "bw": 80}])])
    assert [o for o in build_wifi(five)["observations"] if "compete" in o] == [
        "A and C use overlapping 5 GHz channels 36 and 44, so they compete with each other"]
    same_ap = Snapshot(site={}, devices=[], clients=[], legacy_devices=[ap("AA:01", "Solo", [
        {"radio": "ng", "channel": 6, "bw": 20}, {"radio": "ng", "channel": 6, "bw": 20}])])
    assert not any("compete" in o for o in build_wifi(same_ap)["observations"])        # one AP never competes with itself


def test_usual_2_4_ghz_channel_comparison_handles_ties_and_no_neighbors():
    even = build_wifi(snap_two_aps())["observations"]
    assert any("channels 1, 6 and 11 are equally busy" in o for o in even)
    crowded = [row(bssid=f"n{i}", ch=6, sig=-50) for i in range(2)] + [row(bssid="n9", ch=1, sig=-50)]
    tied = build_wifi(snap_two_aps(crowded))["observations"]
    assert any("channel 11 overlaps the fewest neighbors (channel 1: 1, channel 6: 2, channel 11: 0)" in o for o in tied)
    both = [row(bssid="n1", ch=6, sig=-50)]
    two_quiet = [o for o in build_wifi(snap_two_aps(both))["observations"] if "fewest" in o][0]
    assert "channel 1 and channel 11 overlap the fewest neighbors" in two_quiet


def test_6_ghz_has_no_neighbor_data_and_is_labelled_as_such():
    s = Snapshot(site={}, devices=[], clients=[], legacy_devices=[ap("AA:01", "Tri", [
        {"radio": "6e", "channel": 37, "bw": 160}, {"radio": "ng", "channel": 1, "bw": 20}])])
    w = build_wifi(s)
    six = next(b for b in w["plan"] if b["band"] == "6e")
    assert six["scanned"] is False and next(b for b in w["plan"] if b["band"] == "ng")["scanned"] is True
    text = render_text(w)
    assert "(the controller's neighbor scan does not report 6 GHz networks)" in text
    assert any(line.split()[:3] == ["37", "n/a", "n/a"] for line in text.splitlines())   # the table says n/a
    assert not any("6 GHz" in o for o in w["observations"])                           # nothing claimed about 6 GHz


def test_filters_by_band_and_by_ap(fake_client):
    snap = collect_snapshot(fake_client, "default", include_neighbors=True)
    five = build_wifi(snap, band="na")
    assert [b["band"] for b in five["plan"]] == ["na"] and five["neighbors"]["total"] == 2
    assert {r["band"] for r in five["radios"]} <= {"na", ""}
    garage = build_wifi(snap, ap="garage")                                             # only what Garage AP hears
    assert garage["ap_matched"] is True and [r["ap"] for r in garage["radios"]] == ["Garage AP"]
    assert garage["neighbors"]["total"] == 3                                           # One, Eleven Net, Far Block
    none = build_wifi(snap, ap="nonexistent")
    assert none["ap_matched"] is False and none["radios"] == [] and none["neighbors"]["total"] == 0
    assert render_text(none, ap="nonexistent") == "No access point matches 'nonexistent'."
    assert build_wifi(snap, ap="OFFICE")["ap_matched"] is True                         # case-insensitive


# -- rendering --------------------------------------------------------------------

def test_text_shows_names_security_open_flag_and_how_many_aps_heard_it(report):
    text = render_text(report)
    assert "Neighboring networks: 9 seen by your APs (8 stronger than -80 dBm, 1 open, 1 with a hidden name)" in text
    assert "Neighbor One  (-45 dBm, WPA2-Personal (AES/CCMP), heard by 2 APs)" in text
    assert "(hidden, Acme Corp)  (-66 dBm" in text
    assert "Café Guest ☕  (-72 dBm, Open)  [OPEN]" in text
    assert "Garage AP (offline): no radio data" in text
    assert any(line.split()[:4] == ["Office", "AP", "2.4", "GHz"] for line in text.splitlines())
    assert "Weak Net" not in text                                                       # below the cutoff: counted, not named


def test_names_are_capped_per_channel_unless_all():
    rows = [row(bssid=f"n{i:02d}", ch=11, sig=-40 - i, name=f"Net{i:02d}") for i in range(NAMES_PER_CHANNEL + 3)]
    w = build_wifi(snap_two_aps(rows))
    capped = render_text(w)
    assert f"... and 3 more (use --all)" in capped
    assert f"Net{NAMES_PER_CHANNEL - 1:02d}" in capped and f"Net{NAMES_PER_CHANNEL:02d}" not in capped
    full = render_text(w, show_all=True)
    assert "more (use --all)" not in full and f"Net{NAMES_PER_CHANNEL + 2:02d}" in full


def test_missing_data_still_renders_and_json_is_complete(report):
    empty = build_wifi(Snapshot(site={}, devices=[], clients=[]))
    assert empty["plan"] == [] and empty["observations"] == []
    text = render_text(empty)
    assert "no radio data" in text and "Neighboring networks: 0 seen by your APs" in text
    parsed = json.loads(to_json(report))
    assert set(parsed) == {"ap_matched", "min_signal", "radios", "neighbors", "plan", "observations"}
    assert parsed["plan"][0]["channels"][0]["strong_networks"] is not None and "span" not in parsed["radios"][0]


# -- command line -----------------------------------------------------------------

def _run(fake_client, monkeypatch, argv):
    monkeypatch.setenv("CONTROLLER_URL", "https://controller")
    monkeypatch.setenv("API_KEY", "key")
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, c: fake_client))
    return cli.main(argv)


def test_cli_wifi_options(fake_client, monkeypatch, capsys):
    assert _run(fake_client, monkeypatch, ["wifi"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Access points") and "Neighbor One" in out and "Observations" in out
    assert fake_client.session.posts == []                                              # wifi only reads (GET)

    assert _run(fake_client, monkeypatch, ["wifi", "--band", "5"]) == 0
    out = capsys.readouterr().out
    assert "5 GHz" in out and "\n2.4 GHz" not in out and "Five GHz Neighbor" in out

    assert _run(fake_client, monkeypatch, ["wifi", "--band", "2.4 GHz", "--ap", "garage", "--min-signal", "-55"]) == 0
    out = capsys.readouterr().out
    assert "Eleven Net" in out and "Neighbor One" in out and "Far Block" not in out

    assert _run(fake_client, monkeypatch, ["wifi", "--ap", "zzz"]) == 0
    assert capsys.readouterr().out.strip() == "No access point matches 'zzz'."

    assert _run(fake_client, monkeypatch, ["wifi", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["neighbors"]["total"] == 9

    for bad in (["wifi", "--band", "7"], ["wifi", "--min-signal", "5"], ["wifi", "--min-signal", "weak"]):
        with pytest.raises(SystemExit) as exc:
            _run(fake_client, monkeypatch, bad)
        assert exc.value.code == cli.EXIT_USAGE
        capsys.readouterr()


def test_cli_wifi_all_lists_every_strong_neighbor(fake_client, monkeypatch, capsys):
    fake_client.session.fx["legacy"]["rogueap"] += [row(bssid=f"02:bb:00:00:00:{i:02x}", ch=6, sig=-50 - i, name=f"Extra{i}")
                                                    for i in range(8)]
    _run(fake_client, monkeypatch, ["wifi"])
    assert "more (use --all)" in capsys.readouterr().out
    _run(fake_client, monkeypatch, ["wifi", "--all"])
    out = capsys.readouterr().out
    assert "more (use --all)" not in out and "Extra7" in out


def test_cli_wifi_degrades_when_the_neighbor_scan_is_unavailable(fake_client, monkeypatch, capsys):
    real = fake_client.legacy_stat

    def flaky(site_ref, resource):
        if resource == "rogueap":
            raise UniFiAPIError("scan down")
        return real(site_ref, resource)

    monkeypatch.setattr(fake_client, "legacy_stat", flaky)
    assert _run(fake_client, monkeypatch, ["wifi"]) == 0
    captured = capsys.readouterr()
    assert "neighboring networks unavailable, the channel plan was skipped: scan down" in captured.err
    assert any(line.split()[:4] == ["Office", "AP", "2.4", "GHz"] for line in captured.out.splitlines())
    assert "Neighboring networks: 0 seen" in captured.out
