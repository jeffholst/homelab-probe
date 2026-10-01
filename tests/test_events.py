import inspect
import json
import re
from pathlib import Path

import pytest

from unifi_sentinel import cli, client as client_module, events as events_module
from unifi_sentinel.client import SYSTEM_LOG_PATH, UniFiAPIError, UniFiClient
from unifi_sentinel.events import (event_json, event_row, fetch_events, make_filter, parse_duration,
                                   render_events, render_message, summarize)

SYSTEM_LOG = "/proxy/network/v2/api/site/default/system-log/all"
DAY = 86400


def names(events):
    return [e["id"] for e in events]


# -- the approved POST is the only POST ------------------------------------

def test_client_has_no_general_purpose_write_methods():
    public = {n for n in dir(UniFiClient) if not n.startswith("_")}
    assert not public & {"post", "put", "patch", "delete", "request", "send"}
    assert "system_log" in public
    # the guarded helper takes no path, so it cannot be pointed anywhere else
    assert list(inspect.signature(UniFiClient._post_system_log).parameters) == ["self", "site_ref", "query"]


def test_only_client_py_sends_a_post_and_only_once():
    root = Path(client_module.__file__).parent
    posts = {f.name: len(re.findall(r"\.post\(", f.read_text())) for f in root.glob("*.py")}
    assert {n: c for n, c in posts.items() if c} == {"client.py": 1}
    other = [f.name for f in root.glob("*.py")
             if re.search(r"\.(put|patch|delete)\(|requests\.(post|put|patch|delete)\(", f.read_text())]
    assert other == []


def test_system_log_posts_one_query_to_the_fixed_path(fake_client):
    page = fake_client.system_log("default", {"timestampFrom": 0, "pageSize": 3, "categories": ["AUDIT"]})
    assert fake_client.session.posts == [
        (SYSTEM_LOG, {"timestampFrom": 0, "pageSize": 3, "categories": ["AUDIT"]})]
    assert SYSTEM_LOG_PATH.format(site="default") == SYSTEM_LOG
    assert {e["category"] for e in page["data"]} == {"AUDIT"} and page["total_element_count"] == 2
    assert fake_client.session.calls == []          # no GET was involved


def test_a_system_log_query_may_only_use_known_keys_and_sends_nothing_otherwise(fake_client):
    for bad in ({"enabled": False}, {"timestampFrom": 0, "action": "delete"}, {"path": "/x"}):
        with pytest.raises(ValueError, match="unsupported system-log query key"):
            fake_client.system_log("default", bad)
    assert fake_client.session.posts == []


def test_get_requests_are_unchanged_and_the_log_path_is_post_only(fake_client):
    assert fake_client.info()["applicationVersion"] == "10.0.0"       # normal GET path
    assert fake_client.session.posts == []
    with pytest.raises(UniFiAPIError, match="HTTP 405"):
        fake_client._get("/proxy/network/v2/api/site/default/system-log/all")


def test_system_log_errors_are_reported_cleanly(fake_client):
    fake_client.session.status = 500
    with pytest.raises(UniFiAPIError, match="HTTP 500"):
        fake_client.system_log("default", {})
    fake_client.session.status = 401
    with pytest.raises(UniFiAPIError, match="401 Unauthorized"):
        fake_client.system_log("default", {})
    fake_client.session.status = None
    fake_client.session.post = lambda *a, **k: type("R", (), {
        "status_code": 200, "ok": True, "text": "", "json": lambda self: {"unexpected": True}})()
    with pytest.raises(UniFiAPIError, match="no 'data' list"):
        fake_client.system_log("default", {})


# -- durations and messages ------------------------------------------------

@pytest.mark.parametrize("text, seconds", [("90m", 5400), ("24h", DAY), ("7d", 7 * DAY),
                                           ("2w", 14 * DAY), (" 3 H ", 3 * 3600)])
def test_parse_duration(text, seconds):
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("bad", ["", "0h", "5", "h", "-1d", "1.5h", "5x", "1 day"])
def test_parse_duration_rejects_bad_values(bad):
    with pytest.raises(ValueError, match="invalid duration"):
        parse_duration(bad)


def test_message_placeholders_are_filled_and_missing_ones_stay_readable():
    e = {"message_raw": "{CLIENT} roamed from {A} to {B} ({N}) {GONE}",
         "parameters": {"CLIENT": {"name": "phone"}, "A": {"name": "AP1"}, "B": "AP2",
                        "N": {"name": ""}, "X": {"name": "unused"}}}
    assert render_message(e) == "phone roamed from AP1 to AP2 (<n>) <gone>"
    assert render_message({"title_raw": "Title only"}) == "Title only"
    assert render_message({}) == ""
    assert render_message({"message_raw": "{A}", "parameters": {"A": {"name": {"x": 1}}}}) == "<a>"


# -- fetching --------------------------------------------------------------

def test_window_server_filters_and_paging(fake_client, monkeypatch):
    got, more = fetch_events(fake_client, "default", DAY, limit=0)
    assert names(got) == [f"ev{i}" for i in range(1, 11)] and not more        # newest first; ev11 is 10 days old
    assert "ev11" in names(fetch_events(fake_client, "default", 14 * DAY, limit=0)[0])

    monkeypatch.setattr(events_module, "PAGE_SIZE", 3)
    fake_client.session.posts.clear()
    paged, _ = fetch_events(fake_client, "default", DAY, limit=0)
    assert names(paged) == names(got)
    assert [b["pageNumber"] for _, b in fake_client.session.posts] == [0, 1, 2, 3]

    fake_client.session.posts.clear()
    audit, _ = fetch_events(fake_client, "default", DAY, categories=["audit"], limit=0)
    assert names(audit) == ["ev9", "ev10"]
    assert fake_client.session.posts[0][1]["categories"] == ["AUDIT"]          # upper-cased, sent to the server
    assert names(fetch_events(fake_client, "default", DAY, severities=["high"], limit=0)[0]) == ["ev7", "ev8"]
    assert names(fetch_events(fake_client, "default", DAY, search="roamed", limit=0)[0]) == ["ev4"]


def test_limit_marks_that_more_exist_and_stops_early(fake_client, monkeypatch):
    monkeypatch.setattr(events_module, "PAGE_SIZE", 2)
    fake_client.session.posts.clear()
    got, more = fetch_events(fake_client, "default", DAY, limit=3)
    assert names(got) == ["ev1", "ev2", "ev3"] and more
    assert len(fake_client.session.posts) == 2                                  # did not read every page
    got, more = fetch_events(fake_client, "default", DAY, limit=10)
    assert len(got) == 10 and more                                              # stopped exactly at the limit
    got, more = fetch_events(fake_client, "default", DAY, limit=11)
    assert len(got) == 10 and not more


def test_predicate_is_applied_across_pages_before_the_limit(fake_client, monkeypatch):
    monkeypatch.setattr(events_module, "PAGE_SIZE", 2)
    got, more = fetch_events(fake_client, "default", DAY, predicate=make_filter(client="phone"), limit=2)
    assert names(got) == ["ev1", "ev2"] and more
    got, more = fetch_events(fake_client, "default", DAY, predicate=make_filter(client="phone"), limit=0)
    assert names(got) == ["ev1", "ev2", "ev3", "ev4", "ev5"] and not more


def test_never_reads_more_than_max_events(fake_client, monkeypatch):
    monkeypatch.setattr(events_module, "PAGE_SIZE", 2)
    monkeypatch.setattr(events_module, "MAX_EVENTS", 4)
    fake_client.session.posts.clear()
    got, more = fetch_events(fake_client, "default", DAY, limit=0)
    assert len(got) == 4 and not more and len(fake_client.session.posts) == 2


# -- filters the server cannot apply ---------------------------------------

def test_client_device_and_event_filters(fake_client):
    events = fake_client.session.events

    def pick(**kw):
        return [e["id"] for e in events if make_filter(**kw)(e)]

    assert pick(client="phone") == ["ev1", "ev2", "ev3", "ev4", "ev5"]
    assert pick(client="PHONE") == pick(client="phone")                       # case-insensitive
    assert pick(client="10.0.0.10") == ["ev6", "ev11"]                         # by IP (the desktop)
    assert pick(client="bb:00:00:00:00:02") == pick(client="phone")            # by MAC
    assert pick(client="BB-00-00-00-00-01") == ["ev6", "ev11"]
    assert pick(client="nobody") == []
    assert pick(device="garage ap") == ["ev4", "ev8", "ev11"]                  # incl. DEVICE_FROM
    assert pick(device="Office Switch") == ["ev6"]
    assert pick(device="10.0.0.3") == ["ev2", "ev4", "ev11"]
    assert pick(event="roam") == ["ev4", "ev11"]
    assert pick(event="ip conflict") == pick(event="ip-conflict") == pick(event="IP_CONFLICT") == ["ev7"]
    assert pick(client="phone", event="disconnected") == ["ev1", "ev3", "ev5"]  # filters combine with AND
    assert make_filter() is None


# -- presenting ------------------------------------------------------------

def test_rows_json_and_summary(fake_client):
    events = fake_client.session.events[:5]
    row = event_row(events[0])
    assert row["Severity"] == "Low" and row["Category"] == "CLIENT_DEVICES"
    assert row["Event"] == "CLIENT_DISCONNECTED_WIRELESS"
    assert row["Message"] == "phone disconnected from Home. Time Connected: 25s."
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", row["Time"])

    j = event_json(events[0])
    assert j["client"] == {"name": "phone", "ip": "10.0.0.11", "id": "bb:00:00:00:00:02"}
    assert j["device"] is None and j["key"] == "CLIENT_DISCONNECTED_WIRELESS_2"
    assert event_json(events[1])["device"]["name"] == "Office AP"

    s = summarize(fake_client.session.events[:10])
    assert s["total"] == 10
    assert dict(s["by_event"])["CLIENT_DISCONNECTED_WIRELESS"] == 3
    assert s["noisiest"][0] == {"Event": "CLIENT_DISCONNECTED_WIRELESS", "Subject": "phone", "Count": 3}
    assert dict(s["by_severity"]) == {"Low": 6, "High": 2, "Medium": 2}


def test_render_modes(fake_client):
    events = fake_client.session.events[:3]
    table = render_events(events, more=False)
    assert "Message" in table and "3 event(s)" in table and "showing the newest" not in table
    assert "showing the newest events only" in render_events(events, more=True)
    assert [r["Event"] for r in json.loads(render_events(events, False, as_json=True))][0] == \
        "CLIENT_DISCONNECTED_WIRELESS"
    assert render_events([], False) == "No events match."
    text = render_events(fake_client.session.events[:10], False, summary=True)
    assert "By severity:" in text and "Noisiest" in text and "phone" in text
    parsed = json.loads(render_events(fake_client.session.events[:10], True, as_json=True, summary=True))
    assert parsed["total"] == 10 and parsed["truncated"] is True and parsed["noisiest"][0]["Count"] == 3
    assert render_events([], False, summary=True) == "No events in this window."


def test_unresolved_placeholders_render_readably(fake_client):
    text = render_events([fake_client.session.events[9]], False)
    assert "made a change to <setting name> in Office AP <section> settings" in text


# -- command line ----------------------------------------------------------

def _run(fake_client, monkeypatch, argv):
    monkeypatch.setenv("CONTROLLER_URL", "https://controller")
    monkeypatch.setenv("API_KEY", "key")
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, c: fake_client))
    return cli.main(argv)


def test_cli_events_default_filters_summary_and_json(fake_client, monkeypatch, capsys):
    assert _run(fake_client, monkeypatch, ["events"]) == 0
    out = capsys.readouterr().out
    assert "10 event(s)" in out and "ev1" not in out                           # a table, not raw ids
    assert "Multiple devices are using the same IP 10.0.0.50 address." in out
    assert "roamed from Garage AP to Office AP" in out

    assert _run(fake_client, monkeypatch, ["events", "--client", "phone", "--event", "disconnected",
                                           "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 3

    assert _run(fake_client, monkeypatch, ["events", "--severity", "high", "--severity", "low",
                                           "--category", "UNIFI_DEVICES"]) == 0
    assert "Garage AP is unreachable." in capsys.readouterr().out
    assert _run(fake_client, monkeypatch, ["events", "--summary"]) == 0
    assert "Noisiest" in capsys.readouterr().out
    assert _run(fake_client, monkeypatch, ["events", "--summary", "--limit", "2", "--json"]) == 0
    summary = json.loads(capsys.readouterr().out)                    # --limit does not cap a summary
    assert summary["total"] == 10 and summary["truncated"] is False
    assert _run(fake_client, monkeypatch, ["events", "--since", "14d", "--limit", "0"]) == 0
    assert "11 event(s)" in capsys.readouterr().out
    assert _run(fake_client, monkeypatch, ["events", "--limit", "2"]) == 0
    assert "showing the newest events only" in capsys.readouterr().out
    assert _run(fake_client, monkeypatch, ["events", "-s", "nothing-matches-this"]) == 0
    assert "No events match." in capsys.readouterr().out
    assert all(path == SYSTEM_LOG for path, _ in fake_client.session.posts)   # the only POST target


def test_cli_events_usage_and_errors(fake_client, monkeypatch, capsys):
    for bad in (["events", "--since", "5x"], ["events", "--limit", "-1"],
                ["events", "--severity", "urgent"], ["events", "--limit", "many"]):
        with pytest.raises(SystemExit) as exc:
            _run(fake_client, monkeypatch, bad)
        assert exc.value.code == cli.EXIT_USAGE
        capsys.readouterr()
    assert fake_client.session.posts == []                                     # rejected before any request
    fake_client.session.status = 500
    assert _run(fake_client, monkeypatch, ["events"]) == cli.EXIT_ERROR
    assert "HTTP 500" in capsys.readouterr().err
