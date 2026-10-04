"""The logger tree (``logs.py``): formats, redaction, one line per record, ids, personal data and the event list."""

import ast
import contextlib
import io
import json
import logging
import re
from pathlib import Path
from urllib.parse import quote, quote_plus

import pytest
from conftest import FakeSession
from test_notify import NTFY, TOKEN, FakePost, events_for, finding
from test_watch import Script
from test_watch import run as run_watch

from homelab_probe import cli, logs
from homelab_probe.client import UniFiAPIError, UniFiClient
from homelab_probe.config import Config, ConfigError, SmtpSettings, load_config, parse_log_format, parse_log_level
from homelab_probe.diagnose import WARNING
from homelab_probe.notify import Destination, send
from homelab_probe.snapshot import EventQuery, Needs, collect_snapshot

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "homelab_probe"
log = logging.getLogger("homelab_probe.test")

UNIFI_API_KEY = "ak-9f8e7d6c5b4a3210"
PASSWORD = "pw-Hunter2-Secret!"
SESSION = "sess-0123456789abcdef"
SETUP = "setup-7777aaaa8888"
URL = "https://ntfy.example/topic-3f9c1a2b"
REGISTERED = (UNIFI_API_KEY, PASSWORD, SESSION, SETUP, URL, TOKEN)


def capture(fmt, level="DEBUG"):
    stream = io.StringIO()
    logs.configure(fmt, level, stream=stream)
    return stream


def records(stream):
    return [json.loads(line) for line in stream.getvalue().splitlines()]


# -- the command-line format: what the tool always printed ---------------------------------------------------

def test_without_any_setup_a_warning_prints_as_it_always_did(capsys):
    logs.reset()
    logs.warn("legacy stat/sta unavailable; port mapping will be incomplete: HTTP 500")
    logs.verbose("not shown without --verbose")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Warning: legacy stat/sta unavailable; port mapping will be incomplete: HTTP 500\n"


def test_verbose_lines_show_with_debug_and_a_prefix(capsys):
    logs.configure("cli", "DEBUG")
    logs.verbose("GET /x -> 200 (3 ms)", "http.request")
    assert capsys.readouterr().err == "[verbose] GET /x -> 200 (3 ms)\n"


def test_in_cli_format_a_failed_delivery_is_not_a_warning_line_unless_verbose(capsys):
    logs.configure("cli", "WARNING")
    logs.log_event(log, logging.WARNING, "notify.delivery", "notify ntfy -> HTTP 503 (4 ms)")
    logs.log_event(log, logging.INFO, "notify.delivery", "notify ntfy -> HTTP 200 (4 ms)")
    assert capsys.readouterr().err == ""
    logs.configure("cli", "INFO")
    logs.log_event(log, logging.INFO, "notify.delivery", "notify ntfy -> HTTP 200 (4 ms)")
    assert capsys.readouterr().err == "[verbose] notify ntfy -> HTTP 200 (4 ms)\n"


def test_an_error_record_has_the_error_prefix(capsys):
    logs.configure("cli", "WARNING")
    log.error("boom")
    assert capsys.readouterr().err == "ERROR: boom\n"


def test_the_stream_is_looked_up_for_each_record(capsys):
    logs.configure("cli", "WARNING")
    first = io.StringIO()
    with contextlib.redirect_stderr(first):
        logs.warn("one")
    logs.warn("two")
    assert first.getvalue() == "Warning: one\n" and capsys.readouterr().err == "Warning: two\n"


# -- the text and JSON formats -------------------------------------------------------------------------------

def test_the_text_format_has_time_level_logger_event_message_and_fields():
    stream = capture("text", "INFO")
    with logs.bind(request_id="abc123", site="default"):
        logs.log_event(log, logging.INFO, "notify.delivery", "sent", destination="ntfy", delivered=True,
                       reason="HTTP 200", note="two words")
    assert re.fullmatch(
        r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z INFO    homelab_probe\.test notify\.delivery sent "
        r'request_id=abc123 site=default destination=ntfy delivered=true reason="HTTP 200" note="two words"\n',
        stream.getvalue())


def test_a_text_record_without_an_event_or_fields_is_still_a_line():
    stream = capture("text", "INFO")
    log.info("plain")
    assert re.fullmatch(r"\S+ INFO    homelab_probe\.test - plain\n", stream.getvalue())
    stream = capture("text", "INFO")
    logs.log_event(log, logging.INFO, "watch.pass", "x", empty="", nothing=None)
    assert stream.getvalue().rstrip().endswith('x empty="" nothing=""')


def test_every_json_line_parses_and_has_the_required_fields():
    stream = capture("json", "DEBUG")
    with logs.bind(request_id="r1", user="admin", site="default"):
        logs.log_event(log, logging.INFO, "watch.pass", "pass finished", findings=2, complete=True)
    log.warning("no event, no fields")
    logs.warn("a degraded read")
    first, second, third = records(stream)
    required = {"ts", "level", "logger", "msg", "event", "request_id", "user", "site"}
    assert all(required <= set(r) for r in (first, second, third))
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", first["ts"])
    assert (first["level"], first["event"], first["request_id"], first["user"], first["site"]) == (
        "INFO", "watch.pass", "r1", "admin", "default")
    assert (first["findings"], first["complete"]) == (2, True)
    assert second["event"] is None and second["request_id"] is None
    assert third["event"] == "warning" and third["msg"] == "a degraded read"


def test_a_field_named_like_a_reserved_key_does_not_replace_it():
    stream = capture("json", "INFO")
    logs.log_event(log, logging.INFO, "watch.pass", "real message", msg="forged", level="forged")
    (record,) = records(stream)
    assert record["msg"] == "real message" and record["level"] == "INFO"
    assert record["field_msg"] == "forged" and record["field_level"] == "forged"


def test_non_scalar_fields_become_json_text_and_odd_names_become_safe_keys():
    stream = capture("json", "INFO")
    logs.log_event(log, logging.INFO, "watch.pass", "x", items=[1, {"b": 2}], **{"bad key\n": 1, "": 2})
    (record,) = records(stream)
    assert record["items"] == '[1, {"b": 2}]'
    assert record["bad_key"] == 1 and record["_"] == 2


# -- one event is one clean line -----------------------------------------------------------------------------

NASTY = "name\nsecond line\r\x1b[31mred\x1b[0m\u2028sep\u202eevil\x00end"


@pytest.mark.parametrize("fmt", ["cli", "text", "json"])
def test_hostile_text_still_gives_exactly_one_clean_line(fmt):
    stream = capture(fmt, "DEBUG")
    logs.log_event(log, logging.DEBUG, "http.request", f"GET {NASTY}", name=NASTY, **{NASTY: 1})
    logs.warn(NASTY)
    log.warning("%s", NASTY)
    output = stream.getvalue()
    assert len(output.splitlines()) == 3 and output.endswith("\n")
    assert not re.search(r"[\x00-\x09\x0b-\x1f\x7f\u2028\u2029\u202e]", output)
    if fmt == "json":
        assert all(isinstance(r["msg"], str) for r in records(stream))


def test_json_lines_are_plain_ascii_so_no_tool_can_split_them():
    stream = capture("json")
    log.warning("café \u2603 \U0001f600")
    assert stream.getvalue().isascii() and len(stream.getvalue().splitlines()) == 1
    assert records(stream)[0]["msg"] == "café \u2603 \U0001f600"


@pytest.mark.parametrize("fmt", ["cli", "text", "json"])
def test_a_long_message_is_cut(fmt):
    stream = capture(fmt)
    log.warning("x" * 10_000)
    output = stream.getvalue()
    assert len(output) < logs.MAX_LINE + 500 and ("…" in output or "\\u2026" in output)


def test_a_call_whose_arguments_do_not_fit_its_format_still_logs():
    stream = capture("json")
    handler = next(h for h in logging.getLogger(logs.ROOT).handlers if isinstance(h, logs._Handler))
    # handed to our handler directly: pytest's own capture handlers (also attached to this logger) would raise
    handler.handle(logging.LogRecord("homelab_probe.test", logging.WARNING, __file__, 1, "%d items", ("many",),
                                     None))
    (record,) = records(stream)
    assert record["msg"] == "%d items"


def test_a_handler_that_cannot_write_does_not_raise(monkeypatch):
    class Broken(io.StringIO):
        def write(self, text):
            raise OSError("closed")

    monkeypatch.setattr(logging, "raiseExceptions", False)
    logs.configure("json", "DEBUG", stream=Broken())
    log.warning("still fine")


# -- secrets never reach a line ------------------------------------------------------------------------------

@pytest.mark.parametrize("fmt", ["cli", "text", "json"])
def test_poisoned_values_never_appear_through_any_channel(fmt):
    logs.register_secrets(*REGISTERED)
    stream = capture(fmt, "DEBUG")
    try:
        raise RuntimeError(f"the server echoed {UNIFI_API_KEY} and password={PASSWORD}")
    except RuntimeError:
        log.exception("failed with %s", SESSION)
    for secret in REGISTERED:
        log.warning("message %s", secret)
        log.warning(f"fstring {secret}")
        logs.log_event(log, logging.INFO, "warning", "m", value=secret, nested={"deep": [secret]})
    logs.warn(f"degraded: {URL} and {SETUP}")
    logs.verbose(f"settings {UNIFI_API_KEY}")
    for name in ("password", "token", "api_key", "session_id", "setup_token", "authorization", "cookie",
                 "set-cookie", "x.csrf", "credentials", "SESSION"):
        logs.log_event(log, logging.INFO, "warning", "m", **{name: "unregistered-value-xyz"})
    log.warning("headers: Authorization: Bearer abcdef0123456789 and Cookie: sid=unreg-cookie-1; other=2")
    log.warning("Set-Cookie: unifises=unreg-set-cookie; Path=/")
    log.warning('{"Authorization": "Basic dXNlcjpwYXNz", "token": "unreg json token", "Accept": "x"}')
    log.warning("password=unreg-pass&api_key=unreg-key token: 'unreg quoted' secret=unreg-secret")
    log.warning("https://user:unreg-userinfo@host.example/path")
    output = stream.getvalue()
    for secret in (*REGISTERED, "unregistered-value-xyz", "abcdef0123456789", "unreg-cookie-1", "unreg-set-cookie",
                   "dXNlcjpwYXNz", "unreg json token", "unreg-pass", "unreg-key", "unreg quoted", "unreg-secret",
                   "unreg-userinfo", "other=2"):
        assert secret not in output, secret
    assert logs.REDACTED in output


def test_encoded_spellings_of_a_secret_are_hidden_too():
    secret = "p@ss word/with+chars&more"
    logs.register_secrets(secret)
    stream = capture("json")
    log.warning("raw %s | quoted %s | plus %s | json %s", secret, quote(secret, safe=""), quote_plus(secret),
                json.dumps(secret)[1:-1])
    output = stream.getvalue()
    for spelling in (secret, quote(secret, safe=""), quote_plus(secret)):
        assert spelling not in output
    assert "with+chars" not in output


def test_a_secret_hidden_by_invisible_characters_is_still_hidden():
    logs.register_secrets(UNIFI_API_KEY)
    stream = capture("json")
    log.warning("key %s", UNIFI_API_KEY[:5] + "\u200b" + UNIFI_API_KEY[5:])
    assert UNIFI_API_KEY not in stream.getvalue() and "\u200b" not in stream.getvalue()


def test_short_and_non_text_secrets_are_not_registered():
    logs.register_secrets("key", "   ", None, 12345, b"bytes-secret-value")
    assert logs._secrets == []
    stream = capture("cli", "DEBUG")
    logs.verbose("the key is mentioned here")
    assert stream.getvalue() == "[verbose] the key is mentioned here\n"


def test_scrub_is_idempotent_and_leaves_ordinary_text_alone():
    logs.register_secrets(UNIFI_API_KEY)
    samples = [f"a {UNIFI_API_KEY} b", "password=abc token: 'x y'", "Authorization: Bearer abcdefgh", "plain text",
               "Set-Cookie: a=b", '{"secret": "s"}', "https://u:p@h/", "monkey=3 keys=a,b tokens=2"]
    for sample in samples:
        assert logs.scrub(logs.scrub(sample)) == logs.scrub(sample)
    assert logs.scrub("GET /proxy/network/api/s/default/stat/sta?offset=0&limit=200 -> 200 (12 ms)") == \
        "GET /proxy/network/api/s/default/stat/sta?offset=0&limit=200 -> 200 (12 ms)"
    assert logs.scrub("monkey=3 keys=a,b tokens=2") == "monkey=3 keys=a,b tokens=2"


def test_every_handler_has_the_redaction_filter():
    for mode in ("cli", "text", "json"):
        logs.configure(mode)
        handlers = [h for h in logging.getLogger(logs.ROOT).handlers if isinstance(h, logs._Handler)]
        assert len(handlers) == 1
        assert any(isinstance(f, logs.RedactFilter) for f in handlers[0].filters)
    assert logging.getLogger(logs.ROOT).propagate is False


def test_configuring_again_replaces_the_handler_and_keeps_foreign_ones():
    foreign = logging.NullHandler()
    logger = logging.getLogger(logs.ROOT)
    logger.addHandler(foreign)
    try:
        logs.configure("json")
        logs.configure("text")
        assert len([h for h in logger.handlers if isinstance(h, logs._Handler)]) == 1
        assert foreign in logger.handlers
    finally:
        logger.removeHandler(foreign)


def test_bad_format_and_level_words_are_refused():
    with pytest.raises(ValueError, match="log format must be one of"):
        logs.configure("xml")
    with pytest.raises(ValueError, match="log level must be one of"):
        logs.configure("json", "loud")
    assert logs.parse_level("warning") == logging.WARNING and logs.parse_level(" Debug ") == logging.DEBUG


# -- ids and context -----------------------------------------------------------------------------------------

def test_request_ids_reach_the_controller_trace_also_from_worker_threads():
    client = UniFiClient("https://controller", "key", workers=4)
    client.session = FakeSession()
    stream = capture("json", "DEBUG")
    with logs.bind(request_id="run-0001", site="default"):
        collect_snapshot(client, "default", Needs(reservations=True, health=True, events=EventQuery(3600)))
    requests_logged = [r for r in records(stream) if r["event"] == "http.request"]
    assert len(requests_logged) > 8
    assert {r["request_id"] for r in requests_logged} == {"run-0001"}
    assert {r["site"] for r in requests_logged} == {"default"}
    assert all({"method", "path", "outcome", "duration_ms"} <= set(r) for r in requests_logged)
    assert {r["method"] for r in requests_logged} == {"GET", "POST"}
    stream = capture("json", "DEBUG")
    collect_snapshot(client, "default")
    assert {r["request_id"] for r in records(stream)} == {None}          # outside the block there is none


def test_bind_restores_the_previous_values_and_leaves_none_alone():
    with logs.bind(request_id="outer", user="u"):
        with logs.bind(request_id="inner"):
            assert logs.current_request_id() == "inner"
            stream = capture("json")
            log.warning("x")
            assert records(stream)[0]["user"] == "u"
        assert logs.current_request_id() == "outer"
    assert logs.current_request_id() == ""
    assert len(logs.new_id()) == 12 and logs.new_id() != logs.new_id()


def test_a_run_gets_its_own_request_id_and_site(fake_client, monkeypatch, capsys):
    monkeypatch.setenv("UNIFI_URL", "https://controller.example")
    monkeypatch.setenv("UNIFI_API_KEY", UNIFI_API_KEY)
    monkeypatch.setenv("LOG_FORMAT", "json")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, c: fake_client))
    assert cli.main(["--site", "default", "info"]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    requests_logged = [r for r in lines if r["event"] == "http.request"]
    assert requests_logged and len({r["request_id"] for r in requests_logged}) == 1
    assert re.fullmatch(r"[0-9a-f]{12}", requests_logged[0]["request_id"]) and requests_logged[0]["site"] == "default"
    run_id = requests_logged[0]["request_id"]
    run_records = [r for r in lines if r["event"] in {"http.request", "snapshot.read", "run.settings", "run.summary"}]
    assert {r["request_id"] for r in run_records} == {run_id}
    assert {r["site"] for r in run_records} == {"default"}
    assert {"run.settings", "run.summary"} <= {r["event"] for r in run_records}
    assert logs.scrub(f"x {UNIFI_API_KEY} y") == "x [redacted] y"       # the run registered its key


# -- the warnings sink ---------------------------------------------------------------------------------------

def test_collected_warnings_are_data_in_order_and_still_printed(capsys):
    with logs.collect_warnings() as outer:
        logs.warn("first")
        with logs.collect_warnings() as inner:
            logs.warn("second\nline")
        logs.warn("third")
    logs.warn("after")
    assert outer == ["first", "third"] and inner == ["second line"]
    assert capsys.readouterr().err == "Warning: first\nWarning: second line\nWarning: third\nWarning: after\n"


def test_a_snapshot_collection_returns_its_warnings_as_data(fake_client, capsys):
    real = fake_client.legacy_stat

    def legacy_stat(site, resource):
        if resource == "health":
            raise UniFiAPIError("unavailable")
        return real(site, resource)

    fake_client.legacy_stat = legacy_stat
    with logs.collect_warnings() as warnings_:
        collect_snapshot(fake_client, "default", Needs(health=True))
    assert any("stat/health unavailable" in w for w in warnings_)
    assert capsys.readouterr().err.count("Warning:") == len(warnings_)


# -- personal data -------------------------------------------------------------------------------------------

def fixture_identifiers():
    data = json.loads((ROOT / "homelab_probe" / "demo" / "controller.json").read_text())
    found = set()

    def walk(node, key=""):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, k)
        elif isinstance(node, list):
            for v in node:
                walk(v, key)
        elif isinstance(node, str):
            if key in ("name", "hostname", "macAddress", "mac", "ipAddress", "ip", "fixed_ip", "last_ip", "uplink_mac"):
                found.add(node)
            elif re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}|\d+\.\d+\.\d+\.\d+", node, re.I):
                found.add(node)

    walk(data)
    return {value for value in found if len(value) > 3 and value.lower() != "default"}   # the site is named so


COMMANDS = [["diagnose"], ["client", "desktop"], ["new-clients"], ["query", "clients"], ["query", "devices"],
            ["wan"], ["wifi"], ["topology"], ["audit"], ["events"], ["query", "reservations"], ["firewall"]]


def run_logged(fake_client, monkeypatch, capsys, level, argv):
    monkeypatch.setenv("UNIFI_URL", "https://controller.example")
    monkeypatch.setenv("UNIFI_API_KEY", UNIFI_API_KEY)
    monkeypatch.setenv("LOG_FORMAT", "json")
    monkeypatch.setenv("LOG_LEVEL", level)
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, c: fake_client))
    cli.main(argv)
    return [line for line in capsys.readouterr().err.splitlines() if line.startswith("{")]


def test_info_and_above_never_carry_client_names_macs_or_addresses_and_debug_has_the_controller(
        fake_client, monkeypatch, capsys):
    identifiers = fixture_identifiers()
    assert {"desktop", "bb:00:00:00:00:01", "10.0.0.10"} <= identifiers
    for argv in COMMANDS:
        info = run_logged(fake_client, monkeypatch, capsys, "INFO", argv)
        text = "\n".join(info)
        assert all(json.loads(line)["level"] in ("INFO", "WARNING", "ERROR") for line in info)
        for value in identifiers:
            assert not re.search(rf"(?<!\w){re.escape(value)}(?!\w)", text, re.I), (argv, value)   # whole words: the logger is homelab_probe.*
        assert "controller.example" not in text
    debug = "\n".join(run_logged(fake_client, monkeypatch, capsys, "DEBUG", ["info"]))
    assert "controller.example" in debug and '"event": "run.settings"' in debug
    assert '"event": "run.summary"' in debug


# -- notifications and the watch loop ----------------------------------------------------------------------------

def test_a_delivery_is_logged_with_kind_reason_and_time_but_never_the_url_or_token():
    logs.register_secrets(NTFY, TOKEN)
    stream = capture("json", "INFO")
    send([Destination("ntfy", NTFY, TOKEN)], events_for(finding(WARNING, "A")), False, 5, post=FakePost(200))
    send([Destination("ntfy", NTFY, TOKEN)], events_for(finding(WARNING, "A")), False, 5, post=FakePost(503))
    ok, failed = records(stream)
    assert (ok["event"], ok["level"], ok["destination"], ok["delivered"], ok["reason"]) == (
        "notify.delivery", "INFO", "ntfy", True, "HTTP 200")
    assert (failed["level"], failed["delivered"], failed["reason"]) == ("WARNING", False, "HTTP 503")
    assert isinstance(ok["duration_ms"], int) and re.fullmatch(r"notify ntfy -> HTTP 200 \(\d+ ms\)", ok["msg"])
    assert NTFY not in stream.getvalue() and TOKEN not in stream.getvalue() and "SECRETTOPIC" not in stream.getvalue()


def test_the_watch_loop_logs_each_pass_and_a_failed_read(fake_client, monkeypatch, capsys):
    monkeypatch.setenv("LOG_FORMAT", "json")
    monkeypatch.setenv("LOG_LEVEL", "INFO")

    def break_controller():
        fake_client.session.status = 500

    run_watch(fake_client, monkeypatch, Script(lambda: None, break_controller))
    lines = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.startswith("{")]
    passes = [r for r in lines if r["event"] == "watch.pass"]
    failed = [r for r in lines if r["event"] == "watch.unavailable"]
    assert passes and passes[0]["complete"] is True and isinstance(passes[0]["findings"], int)
    assert failed and failed[0]["level"] == "WARNING" and failed[0]["reason"] == "http"


def test_a_watch_pass_with_optional_data_missing_is_logged_as_unavailable(fake_client, monkeypatch, capsys):
    real = fake_client.legacy_stat

    def legacy_stat(site, resource):
        if resource == "health":
            raise UniFiAPIError("unavailable")
        return real(site, resource)

    monkeypatch.setattr(fake_client, "legacy_stat", legacy_stat)
    monkeypatch.setenv("LOG_FORMAT", "json")
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    run_watch(fake_client, monkeypatch, Script(lambda: None))
    lines = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.startswith("{")]
    assert any(r["event"] == "watch.unavailable" and r["complete"] is False for r in lines)


def test_a_retry_is_logged_with_its_pause_and_attempt():
    class Down:
        headers = {"X-API-KEY": "key"}

        def get(self, url, **kwargs):
            return type("R", (), {"status_code": 503, "ok": False, "text": "busy"})()

    client = UniFiClient("https://controller", "key", retries=1)
    client.session = Down()
    client._sleep = lambda seconds: None
    stream = capture("json", "DEBUG")
    with pytest.raises(UniFiAPIError):
        client._get("/x")
    retry = next(r for r in records(stream) if r["event"] == "http.retry")
    assert (retry["method"], retry["path"], retry["attempt"], retry["attempts"]) == ("GET", "/x", 2, 2)


# -- the settings --------------------------------------------------------------------------------------------

def test_log_settings_are_validated_in_any_case():
    assert (parse_log_level(None), parse_log_level(" debug "), parse_log_level("Warning")) == ("", "DEBUG", "WARNING")
    assert (parse_log_format(None), parse_log_format("JSON"), parse_log_format(" text ")) == ("", "json", "text")
    with pytest.raises(ConfigError, match="LOG_LEVEL must be one of DEBUG, INFO, WARNING, ERROR"):
        parse_log_level("trace")
    with pytest.raises(ConfigError, match="LOG_FORMAT must be one of text, json"):
        parse_log_format("yaml")


def test_load_config_reads_the_log_settings(monkeypatch):
    monkeypatch.setenv("UNIFI_URL", "https://controller.example")
    monkeypatch.setenv("UNIFI_API_KEY", UNIFI_API_KEY)
    config = load_config()
    assert (config.log_level, config.log_format) == ("", "")
    monkeypatch.setenv("LOG_LEVEL", "info")
    monkeypatch.setenv("LOG_FORMAT", "json")
    config = load_config()
    assert (config.log_level, config.log_format) == ("INFO", "json")
    monkeypatch.setenv("LOG_LEVEL", "loud")
    with pytest.raises(ConfigError, match="LOG_LEVEL"):
        load_config()


def test_a_bad_log_level_is_a_config_error_with_exit_code_3(monkeypatch, capsys):
    monkeypatch.setenv("UNIFI_URL", "https://controller.example")
    monkeypatch.setenv("UNIFI_API_KEY", UNIFI_API_KEY)
    monkeypatch.setenv("LOG_LEVEL", "loud")
    assert cli.main(["info"]) == cli.EXIT_ERROR
    assert "ERROR: LOG_LEVEL must be one of" in capsys.readouterr().err


def test_the_configuration_lists_every_secret_it_holds():
    smtp = SmtpSettings("smtp.example", 587, "starttls", "from@example.org", ("to@example.org",), "mailuser",
                        "mailpassword")
    config = Config("https://controller.example", UNIFI_API_KEY, notify_ntfy_url=NTFY, notify_ntfy_token="ntfytoken",
                    notify_webhook_url="https://hook.example/x", notify_webhook_token="hooktoken", notify_smtp=smtp)
    assert set(config.secret_values()) == {
        UNIFI_API_KEY, NTFY, "ntfytoken", "https://hook.example/x", "hooktoken", "smtp.example", "mailuser",
        "mailpassword", "from@example.org", "to@example.org"}
    assert Config("https://controller.example", UNIFI_API_KEY).secret_values() == (UNIFI_API_KEY,)


# -- the event list --------------------------------------------------------------------------------------------

def event_names_used():
    """Every event name a call in the package passes: ``log_event(logger, level, EVENT, ...)`` (the third
    argument), ``client.debug(message, EVENT, ...)``, ``verbose(message, EVENT)`` and the ``verbose`` of
    ``logs.py`` itself (the default)."""
    used = set()
    for path in PACKAGE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            wanted = {"log_event": 2, "debug": 1, "verbose": 1}.get(name)
            if wanted is None:
                continue
            argument = node.args[wanted] if len(node.args) > wanted else next(
                (k.value for k in node.keywords if k.arg == "event"), None)
            if argument is not None:
                used |= {n.value for n in ast.walk(argument) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    used |= {"run.settings"}                        # the default of ``verbose``
    return used


def test_every_event_name_used_is_listed_and_every_listed_name_is_used():
    used = event_names_used()
    assert used - set(logs.EVENTS) == set(), "add the new event to logs.EVENTS and docs/logging.md"
    assert set(logs.EVENTS) - used == set(), "an event nothing emits: remove it, or emit it"


def test_every_event_is_documented_with_its_meaning_in_the_logging_page():
    page = (ROOT / "docs" / "logging.md").read_text()
    for name in logs.EVENTS:
        assert re.search(rf"^\| `{re.escape(name)}` \|", page, re.M), f"docs/logging.md has no row for {name}"
    for word in (*logs.LEVEL_WORDS, "LOG_LEVEL", "LOG_FORMAT", "request_id"):
        assert word in page


def test_event_names_look_like_stable_identifiers():
    assert all(re.fullmatch(r"[a-z]+(\.[a-z_]+)?", name) for name in logs.EVENTS)
    assert all(text.endswith(".") and text[0].isupper() for text in logs.EVENTS.values())
