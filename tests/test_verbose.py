"""--verbose / --debug: one line per request on stderr, a connection line and a summary, no secrets."""

import re

import pytest
import requests

from unifi_sentinel import cli
from unifi_sentinel.client import UniFiAPIError, UniFiClient
from unifi_sentinel.config import Config
from unifi_sentinel.snapshot import EventQuery, Needs, collect_event_snapshot, collect_snapshot, describe_snapshot

KEY = "sekret-key-0123456789"
MS = r"\(\d+ ms\)"


class Resp:
    def __init__(self, status=200, body=None, text=""):
        self.status_code, self.ok, self._body, self.text = status, 200 <= status < 300, body or {"data": []}, text

    def json(self):
        return self._body


class Scripted:
    def __init__(self, *outcomes):
        self.outcomes, self.headers = list(outcomes), {"X-API-KEY": KEY}

    def _next(self):
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def get(self, url, **kwargs):
        return self._next()

    def post(self, url, **kwargs):
        return self._next()


def traced(*outcomes, **kwargs):
    client = UniFiClient("https://controller.example", KEY, **kwargs)
    client.session = Scripted(*outcomes)
    client._sleep = lambda seconds: None
    lines = []
    client.trace = lines.append
    return client, lines


# -- the client's trace ------------------------------------------------------------------------

def test_a_get_is_traced_with_path_status_and_time():
    client, lines = traced(Resp(200, {"applicationVersion": "10"}))
    client.info()
    assert len(lines) == 1
    assert re.fullmatch(rf"GET /proxy/network/integration/v1/info -> 200 {MS}", lines[0])


def test_pagination_parameters_are_part_of_the_line(fake_client):
    lines = []
    fake_client.trace = lines.append
    fake_client.devices("site-1")
    assert any(re.fullmatch(rf"GET .*/sites/site-1/devices\?offset=0&limit=200 -> 200 {MS}", line) for line in lines)


def test_the_event_log_post_shows_the_query_keys_but_never_the_values():
    client, lines = traced(Resp(200, {"data": []}))
    client.system_log("default", {"pageNumber": 0, "searchText": "very-private-search", "pageSize": 5})
    (line,) = lines
    assert line.startswith("POST /proxy/network/v2/api/site/default/system-log/all (query keys: "
                           "pageNumber, pageSize, searchText) -> 200")
    assert "very-private-search" not in line


def test_retries_are_traced_with_the_wait_and_the_attempt():
    client, lines = traced(Resp(503, text="busy"), Resp(502, text="bad"), Resp(200, {"data": []}))
    client.info()
    assert [re.sub(r" \(\d+ ms\)", "", line) for line in lines] == [
        "GET /proxy/network/integration/v1/info -> 503",
        "GET /proxy/network/integration/v1/info -> retrying in 0 s (attempt 2 of 3)",
        "GET /proxy/network/integration/v1/info -> 502",
        "GET /proxy/network/integration/v1/info -> retrying in 0 s (attempt 3 of 3)",
        "GET /proxy/network/integration/v1/info -> 200"]


@pytest.mark.parametrize("failure, outcome", [
    (requests.exceptions.SSLError(f"bad cert {KEY}"), "TLS certificate verification failed"),
    (requests.exceptions.ReadTimeout(f"slow {KEY}"), "timed out"),
    (requests.exceptions.ConnectionError(f"reset {KEY}"), "connection error"),
    (requests.exceptions.InvalidURL(f"bad {KEY}"), "request error"),
    (OSError(f"cannot read {KEY}"), "could not send"),
])
def test_failures_are_traced_without_the_exception_text(failure, outcome):
    client, lines = traced(failure, retries=0)
    with pytest.raises(UniFiAPIError):
        client.info()
    assert len(lines) == 1 and f"-> {outcome} " in lines[0] and KEY not in lines[0]


def test_non_retried_statuses_are_traced_once():
    client, lines = traced(Resp(403, text="no"))
    with pytest.raises(UniFiAPIError):
        client.info()
    assert len(lines) == 1 and "-> 403" in lines[0]


def test_nothing_is_traced_or_required_without_a_trace_function():
    client, _ = traced(Resp(200, {"data": []}))
    client.trace = None
    assert client.info() == {"data": []}


def test_counters_and_the_summary():
    client, _ = traced(Resp(503, text="busy"), Resp(200, {"data": []}))
    client.info()
    client.info()
    assert (client.attempts_made, client.attempts_retried) == (3, 1)
    assert re.fullmatch(r"3 request\(s\), 1 retried, \d+\.\d s in requests \(added up over all of them, so more than the wall time when they overlap\)", client.summary())
    fresh, _ = traced(Resp(200))
    fresh.info()
    assert "retried" not in fresh.summary()


# -- what was read -------------------------------------------------------------------------------

def test_describe_snapshot_lists_only_what_was_read(fake_client):
    snap = collect_snapshot(fake_client, "default", Needs(reservations=True, health=True))
    text = describe_snapshot(snap)
    assert text.startswith("read 4 devices, 2 connected clients, 4 legacy devices")
    assert "networks" in text and "health subsystems" in text and "neighbor rows" not in text and "events" not in text
    assert describe_snapshot(collect_snapshot(fake_client, "default")).startswith("read 4 devices")


def test_collecting_a_snapshot_reports_it_when_tracing(fake_client):
    lines = []
    fake_client.trace = lines.append
    collect_snapshot(fake_client, "default")
    assert lines[-1].startswith("read 4 devices") and any("GET " in line for line in lines[:-1])
    quiet = []
    fake_client.trace = None
    collect_snapshot(fake_client, "default")
    assert quiet == []


def test_collecting_event_snapshot_reports_it_when_tracing(fake_client):
    lines = []
    fake_client.trace = lines.append
    snap = collect_event_snapshot(fake_client, "default", EventQuery(3600))
    assert lines[-1] == describe_snapshot(snap)
    assert lines[-1].startswith("read ") and " events" in lines[-1]


# -- the command line ------------------------------------------------------------------------------

def run(fake_client, monkeypatch, argv, config_env=None):
    monkeypatch.setenv("CONTROLLER_URL", "https://controller.example")
    monkeypatch.setenv("API_KEY", KEY)
    for name, value in (config_env or {}).items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, c: fake_client))
    return cli.main(argv)


def verbose_lines(err):
    return [line for line in err.splitlines() if line.startswith("[verbose] ")]


@pytest.mark.parametrize("flag", ["--verbose", "--debug"])
def test_verbose_writes_to_stderr_and_leaves_stdout_alone(fake_client, monkeypatch, capsys, flag):
    assert run(fake_client, monkeypatch, ["query", "devices"]) == 0
    plain = capsys.readouterr()
    assert verbose_lines(plain.err) == []
    assert run(fake_client, monkeypatch, [flag, "query", "devices"]) == 0
    loud = capsys.readouterr()
    assert loud.out == plain.out
    lines = verbose_lines(loud.err)
    assert any(re.search(r"GET /proxy/network/integration/v1/sites\?offset=0&limit=200 -> 200 \(\d+ ms\)", line)
               for line in lines)
    assert re.search(r"\[verbose\] \d+ request\(s\), [\d.]+ s in requests \(added up .*\)$", lines[-1])
    assert KEY not in loud.err and KEY not in loud.out


def test_the_first_line_names_the_settings_in_use_without_the_key(fake_client, monkeypatch, capsys):
    run(fake_client, monkeypatch, ["--verbose", "--timeout", "30", "info"])
    first = verbose_lines(capsys.readouterr().err)[0]
    assert first.startswith("[verbose] unifi-sentinel ") and "environment variables only" in first
    assert "controller https://controller.example, site default, timeout 30 s, TLS verification on" in first
    assert KEY not in first


def test_the_line_names_the_env_file_and_a_ca_bundle(fake_client, monkeypatch, capsys, tmp_path):
    bundle = tmp_path / "lab-ca.pem"
    bundle.write_text("x")
    env = tmp_path / "lab.env"
    env.write_text(f"CONTROLLER_URL=https://controller.example\nAPI_KEY={KEY}\nVERIFY_SSL={bundle}\nSITE_ID=lab\n")
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, c: fake_client))
    assert cli.main(["--verbose", "--env-file", str(env), "info"]) == 0
    first = verbose_lines(capsys.readouterr().err)[0]
    assert f"settings from {env}" in first and f"TLS verification CA bundle {bundle}" in first and "site lab" in first
    env.write_text(f"CONTROLLER_URL=https://controller.example\nAPI_KEY={KEY}\nVERIFY_SSL=false\n")
    monkeypatch.delenv("VERIFY_SSL")                       # load_dotenv never overrides what the first run set
    monkeypatch.delenv("SITE_ID", raising=False)
    assert cli.main(["--debug", "--env-file", str(env), "info"]) == 0
    assert "TLS verification off" in verbose_lines(capsys.readouterr().err)[0]


def test_a_command_that_reads_a_snapshot_says_what_it_read(fake_client, monkeypatch, capsys):
    run(fake_client, monkeypatch, ["--verbose", "diagnose", "--json"])
    lines = verbose_lines(capsys.readouterr().err)
    assert any(re.search(r"\[verbose\] read 4 devices, 2 connected clients", line) for line in lines)
    assert any("POST /proxy/network/v2/api/site/default/system-log/all (query keys: " in line for line in lines)
    assert not any(KEY in line for line in lines)


def test_the_summary_is_printed_when_a_request_fails(fake_client, monkeypatch, capsys):
    fake_client.session.status = 500
    assert run(fake_client, monkeypatch, ["--verbose", "info"]) == cli.EXIT_ERROR
    err = capsys.readouterr().err
    assert "ERROR: HTTP 500" in err and "-> 500" in err and verbose_lines(err)[-1].endswith("when they overlap)")


def test_a_config_error_prints_no_summary(fake_client, monkeypatch, capsys):
    monkeypatch.setenv("TIMEOUT", "soon")
    assert run(fake_client, monkeypatch, ["--verbose", "info"]) == cli.EXIT_ERROR
    err = capsys.readouterr().err
    assert "TIMEOUT must be" in err and verbose_lines(err) == []


def test_the_option_goes_before_the_command(fake_client, monkeypatch):
    with pytest.raises(SystemExit) as caught:
        run(fake_client, monkeypatch, ["info", "--verbose"])
    assert caught.value.code == cli.EXIT_USAGE


def test_a_config_object_without_an_env_file_is_comparable():
    assert Config("https://c", "k") == Config("https://c", "k", env_file=None)
