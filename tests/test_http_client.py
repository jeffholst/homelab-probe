"""Transport behavior of UniFiClient: retries, timeouts, TLS, the shared error handling, and what
each command does when a legacy read fails."""

import os
import sys
import warnings

import pytest
import requests
import urllib3

from homelab_probe import cli
from homelab_probe import client as client_module
from homelab_probe.client import GET_RETRIES, UniFiAPIError, UniFiClient
from homelab_probe.config import DEFAULT_TIMEOUT, Config, ConfigError, load_config, parse_timeout, parse_verify

KEY = "sekret-key-0123456789"
URL = "https://controller.example"


class Response:
    def __init__(self, status=200, body=None, text=None, not_json=False):
        self.status_code, self.ok = status, 200 <= status < 300
        self._body = None if not_json else ({} if body is None else body)
        self.text = text if text is not None else str(body)

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


class Scripted:
    """A session that plays back a list of outcomes (a Response, or an exception to raise)."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.headers = {"X-API-KEY": KEY}

    def _next(self, method, url, kwargs):
        self.calls.append((method, url, kwargs))
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def get(self, url, **kwargs):
        return self._next("GET", url, kwargs)

    def post(self, url, **kwargs):
        return self._next("POST", url, kwargs)


def make(*outcomes, **kwargs):
    client = UniFiClient(URL, KEY, **kwargs)
    client.session = Scripted(*outcomes)
    slept = []
    client._sleep = slept.append
    return client, slept


def ok(body=None):
    return Response(200, body if body is not None else {"data": []})


# -- retries ------------------------------------------------------------------------------------

def test_a_get_is_retried_after_a_transient_gateway_error_and_then_succeeds():
    client, slept = make(Response(503, text="busy"), ok({"applicationVersion": "10"}))
    assert client.info() == {"applicationVersion": "10"}
    assert len(client.session.calls) == 2 and slept == [client_module.RETRY_BACKOFF_S]


@pytest.mark.parametrize("failure", [requests.exceptions.ConnectionError("reset by peer"),
                                     requests.exceptions.ReadTimeout("slow")])
def test_a_get_is_retried_after_a_connection_failure_or_timeout(failure):
    client, slept = make(failure, ok())
    assert client.info() == {"data": []} and len(client.session.calls) == 2


def test_a_get_gives_up_after_the_limit_and_says_how_often_it_tried():
    client, slept = make(Response(502, text="bad gateway"))
    with pytest.raises(UniFiAPIError, match=r"HTTP 502 for .*/info \(after 3 attempts\): bad gateway"):
        client.info()
    assert len(client.session.calls) == 1 + GET_RETRIES and len(slept) == GET_RETRIES


def test_the_wait_doubles_between_attempts(monkeypatch):
    monkeypatch.setattr(client_module, "RETRY_BACKOFF_S", 0.5)
    client, slept = make(Response(504, text="x"), retries=3)
    with pytest.raises(UniFiAPIError):
        client.info()
    assert slept == [0.5, 1.0, 2.0] and len(client.session.calls) == 4


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500])
def test_other_statuses_are_not_retried(status):
    client, slept = make(Response(status, text="no"))
    with pytest.raises(UniFiAPIError):
        client.info()
    assert len(client.session.calls) == 1 and slept == []


def test_a_bad_body_and_a_tls_failure_are_not_retried():
    client, _ = make(Response(200, text="<html>", not_json=True))
    with pytest.raises(UniFiAPIError, match="Non-JSON response"):
        client.info()
    assert len(client.session.calls) == 1
    client, _ = make(requests.exceptions.SSLError("bad cert"))
    with pytest.raises(UniFiAPIError, match="TLS certificate verification failed"):
        client.info()
    assert len(client.session.calls) == 1


def test_retries_can_be_turned_off():
    client, slept = make(Response(503, text="busy"), retries=0)
    with pytest.raises(UniFiAPIError):
        client.info()
    assert len(client.session.calls) == 1 and slept == []


def test_the_event_log_post_is_never_retried():
    for outcome in (Response(503, text="busy"), requests.exceptions.ConnectionError("reset"),
                    requests.exceptions.ReadTimeout("slow")):
        client, slept = make(outcome)
        with pytest.raises(UniFiAPIError):
            client.system_log("default", {"pageNumber": 0})
        assert [c[0] for c in client.session.calls] == ["POST"] and slept == []


# -- the shared error messages ---------------------------------------------------------------------

def test_connection_errors_name_the_url_on_get_and_post():
    client, _ = make(requests.exceptions.ConnectionError("connection refused"))
    with pytest.raises(UniFiAPIError, match=r"Connection error for https://controller.example/.*\(after 3 attempts\): "
                                            "connection refused"):
        client.info()
    client, _ = make(requests.exceptions.ConnectionError("connection refused"))
    with pytest.raises(UniFiAPIError) as caught:
        client.system_log("default", {"pageNumber": 0})
    assert "Connection error for" in str(caught.value) and "attempts" not in str(caught.value)


def test_a_timeout_is_a_short_readable_message_with_the_limit():
    client, _ = make(requests.exceptions.ReadTimeout("HTTPSConnectionPool(host=...): Read timed out. (read timeout=15)"))
    with pytest.raises(UniFiAPIError) as caught:
        client.info()
    message = str(caught.value)
    assert message.startswith("timed out after 15 s (after 3 attempts): https://controller.example")
    assert "--timeout" in message and "HTTPSConnectionPool" not in message
    client, _ = make(requests.exceptions.ConnectTimeout("x"), timeout=30)
    with pytest.raises(UniFiAPIError, match="timed out after 30 s"):
        client.info()
    client, _ = make(requests.exceptions.ReadTimeout("x"))
    with pytest.raises(UniFiAPIError) as caught:
        client.system_log("default", {"pageNumber": 0})
    assert str(caught.value).startswith("timed out after 15 s: ")          # one attempt: no "after N attempts"


def test_a_403_has_its_own_actionable_message():
    client, _ = make(Response(403, text='{"error": "forbidden"}'))
    with pytest.raises(UniFiAPIError) as caught:
        client.info()
    message = str(caught.value)
    assert message.startswith("403 Forbidden for https://controller.example")
    assert "key is valid" in message and "Integrations" in message and "forbidden" not in message


def test_401_and_other_http_errors_keep_their_messages():
    client, _ = make(Response(401, text="x"))
    with pytest.raises(UniFiAPIError, match="401 Unauthorized .* invalid API key"):
        client.info()
    client, _ = make(Response(500, text="boom"))
    with pytest.raises(UniFiAPIError, match="HTTP 500 for .*: boom"):
        client.info()


def test_the_key_never_appears_in_the_new_messages():
    for outcome in (requests.exceptions.ConnectionError(f"reset, header {KEY}"),
                    Response(502, text=f"echo {KEY}"), Response(500, text=f"echo {KEY}"),
                    requests.exceptions.ReadTimeout(f"slow {KEY}"), OSError(f"cannot open {KEY}")):
        client, _ = make(outcome)
        with pytest.raises(UniFiAPIError) as caught:
            client.info()
        assert KEY not in str(caught.value) and KEY not in repr(caught.value)


# -- TLS, CA bundles and the timeout reaching the library --------------------------------------------

@pytest.mark.parametrize("outcome, kind, status", [
    (requests.exceptions.SSLError("bad certificate"), "tls", None),
    (requests.exceptions.ReadTimeout("slow"), "timeout", None),
    (requests.exceptions.ConnectionError("refused"), "connection", None),
    (requests.exceptions.InvalidURL("bad"), "request", None),
    (OSError("cannot read the CA bundle"), "request", None),
    (Response(401, text="no"), "unauthorized", 401),
    (Response(403, text="no"), "forbidden", 403),
    (Response(404, text="gone"), "http", 404),
    (Response(500, text="boom"), "http", 500),
    (Response(200, not_json=True), "bad_body", None),
])
def test_every_failure_says_what_kind_it_was_so_a_caller_need_not_read_the_message(outcome, kind, status):
    client, _ = make(outcome, retries=0)
    with pytest.raises(UniFiAPIError) as caught:
        client.info()
    assert (caught.value.kind, caught.value.status) == (kind, status)


def test_a_ca_bundle_tls_failure_and_an_unknown_site_and_a_bad_event_log_have_their_kinds(tmp_path):
    client, _ = make(requests.exceptions.SSLError("x"), retries=0, verify_ssl=str(tmp_path))
    with pytest.raises(UniFiAPIError) as caught:
        client.info()
    assert caught.value.kind == "tls"
    client, _ = make(ok({"data": [{"id": "s1", "internalReference": "default", "name": "Default"}]}))
    with pytest.raises(UniFiAPIError) as caught:
        client.resolve_site("elsewhere")
    assert (caught.value.kind, caught.value.status) == ("site", None)
    client, _ = make(Response(200, {"unexpected": True}))
    with pytest.raises(UniFiAPIError) as caught:
        client.system_log("default", {})
    assert caught.value.kind == "bad_body"


def test_an_error_built_by_hand_has_no_kind_and_no_status():
    error = UniFiAPIError("plain")
    assert (str(error), error.kind, error.status) == ("plain", "", None)


def test_the_timeout_and_verify_setting_reach_every_request(tmp_path):
    bundle = tmp_path / "ca.pem"
    bundle.write_text("x")
    client, _ = make(ok(), ok({"data": []}), verify_ssl=str(bundle), timeout=42)
    client.info()
    client.system_log("default", {"pageNumber": 0})
    assert [c[0] for c in client.session.calls] == ["GET", "POST"]
    assert all(c[2]["timeout"] == 42 and c[2]["verify"] == str(bundle) for c in client.session.calls)


def test_from_config_passes_the_timeout_and_the_ca_bundle(tmp_path):
    bundle = tmp_path / "ca.pem"
    bundle.write_text("x")
    client = UniFiClient.from_config(Config(URL, KEY, verify_ssl=str(bundle), timeout=7))
    assert (client.timeout, client.verify_ssl) == (7, str(bundle))
    assert UniFiClient.from_config(Config(URL, KEY)).timeout == DEFAULT_TIMEOUT


def test_the_tls_message_points_at_a_ca_bundle_or_names_the_one_in_use(tmp_path):
    client, _ = make(requests.exceptions.SSLError("bad"))
    with pytest.raises(UniFiAPIError) as caught:
        client.info()
    assert "point UNIFI_VERIFY_SSL at a CA bundle" in str(caught.value)
    bundle = str(tmp_path / "lab-ca.pem")
    client, _ = make(requests.exceptions.SSLError("bad"), verify_ssl=bundle)
    with pytest.raises(UniFiAPIError) as caught:
        client.system_log("default", {"pageNumber": 0})
    assert bundle in str(caught.value) and "not signed by anything in the CA bundle" in str(caught.value)


def test_an_unreadable_bundle_inside_the_library_is_an_api_error_not_a_traceback():
    client, _ = make(OSError("Could not find a suitable TLS CA certificate bundle, invalid path: /nope.pem"))
    with pytest.raises(UniFiAPIError, match="cannot make the request"):
        client.info()


# -- the insecure-request warning is quiet only for this client ---------------------------------------

class Noisy(Scripted):
    def get(self, url, **kwargs):
        warnings.warn("Unverified HTTPS request", urllib3.exceptions.InsecureRequestWarning, stacklevel=1)
        return ok()


def test_the_warning_is_suppressed_only_inside_this_clients_request_and_only_when_verify_is_off():
    quiet = UniFiClient(URL, KEY, verify_ssl=False)
    quiet.session = Noisy()
    loud = UniFiClient(URL, KEY, verify_ssl=True)
    loud.session = Noisy()
    before = list(warnings.filters)
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        quiet.info()
        assert [w for w in seen if issubclass(w.category, urllib3.exceptions.InsecureRequestWarning)] == []
        loud.info()
        assert len([w for w in seen if issubclass(w.category, urllib3.exceptions.InsecureRequestWarning)]) == 1
        warnings.warn("after", urllib3.exceptions.InsecureRequestWarning, stacklevel=1)   # the filter did not stay on
        assert len([w for w in seen if issubclass(w.category, urllib3.exceptions.InsecureRequestWarning)]) == 2
    assert list(warnings.filters) == before


def test_creating_a_client_changes_no_global_warning_filters():
    before = list(warnings.filters)
    UniFiClient(URL, KEY, verify_ssl=False)
    assert list(warnings.filters) == before


# -- UNIFI_VERIFY_SSL as a CA bundle and UNIFI_TIMEOUT ------------------------------------------------------------

def test_verify_accepts_words_and_existing_bundle_paths(tmp_path):
    assert parse_verify(None) is True and parse_verify("") is True and parse_verify("  ") is True
    assert parse_verify("TRUE") is True and parse_verify("off") is False
    bundle = tmp_path / "lab-ca.pem"
    bundle.write_text("x")
    assert parse_verify(str(bundle)) == str(bundle)
    assert parse_verify(f"  {bundle}  ") == str(bundle)
    assert parse_verify(str(tmp_path)) == str(tmp_path)                       # a directory of certificates
    os.environ["HOME"] = str(tmp_path)
    assert parse_verify("~/lab-ca.pem") == str(bundle)


@pytest.mark.parametrize("text", ["/no/such/ca.pem", "ca.pem", "./missing.crt", "~/nope/ca.cer", "C:\\certs\\ca.pem"])
def test_a_missing_bundle_is_rejected(text):
    with pytest.raises(ConfigError, match="CA bundle that does not exist"):
        parse_verify(text)


@pytest.mark.skipif(sys.platform.startswith("win") or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="needs POSIX permissions and a non-root user")
def test_an_unreadable_bundle_is_rejected(tmp_path):
    bundle = tmp_path / "ca.pem"
    bundle.write_text("x")
    bundle.chmod(0)
    try:
        with pytest.raises(ConfigError, match="cannot be read"):
            parse_verify(str(bundle))
    finally:
        bundle.chmod(0o600)


@pytest.mark.parametrize("text", ["sometimes", "off-ish", "maybe", "2"])
def test_other_words_still_get_the_accepted_words_message(text):
    with pytest.raises(ConfigError, match="UNIFI_VERIFY_SSL must be one of true, yes, 1, on, false, no, 0, off, "
                                          "or the path of a CA bundle"):
        parse_verify(text)


def test_load_config_carries_the_bundle_and_the_timeout(monkeypatch, tmp_path):
    bundle = tmp_path / "ca.pem"
    bundle.write_text("x")
    monkeypatch.setenv("UNIFI_URL", URL)
    monkeypatch.setenv("UNIFI_API_KEY", KEY)
    monkeypatch.setenv("UNIFI_VERIFY_SSL", str(bundle))
    monkeypatch.setenv("UNIFI_TIMEOUT", "45")
    cfg = load_config()
    assert (cfg.verify_ssl, cfg.timeout) == (str(bundle), 45.0)
    monkeypatch.delenv("UNIFI_TIMEOUT")
    assert load_config().timeout == DEFAULT_TIMEOUT


@pytest.mark.parametrize("text, seconds", [(None, 15.0), ("", 15.0), ("30", 30.0), (" 2.5 ", 2.5),
                                           ("1", 1.0), ("600", 600.0)])
def test_timeout_values(text, seconds):
    assert parse_timeout(text) == seconds


@pytest.mark.parametrize("text", ["fast", "0", "0.5", "-3", "601", "inf", "nan", "10s"])
def test_bad_timeouts_are_rejected(text):
    with pytest.raises(ConfigError, match="UNIFI_TIMEOUT must be"):
        parse_timeout(text)


# -- --timeout on the command line ---------------------------------------------------------------------

def run(fake_client, monkeypatch, argv, seen=None):
    fake_client.session.fx["legacy"]["device"][0]["overheating"] = False
    monkeypatch.setenv("UNIFI_URL", URL)
    monkeypatch.setenv("UNIFI_API_KEY", KEY)

    def from_config(cls, cfg):
        if seen is not None:
            seen.append(cfg)
        return fake_client

    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(from_config))
    return cli.main(argv)


def test_the_option_beats_the_env_file_which_beats_the_default(fake_client, monkeypatch):
    seen = []
    assert run(fake_client, monkeypatch, ["info"], seen) == 0 and seen[-1].timeout == DEFAULT_TIMEOUT
    monkeypatch.setenv("UNIFI_TIMEOUT", "20")
    assert run(fake_client, monkeypatch, ["info"], seen) == 0 and seen[-1].timeout == 20
    assert run(fake_client, monkeypatch, ["--timeout", "45", "info"], seen) == 0 and seen[-1].timeout == 45


@pytest.mark.parametrize("value", ["fast", "0", "700"])
def test_a_bad_option_is_a_usage_error(fake_client, monkeypatch, capsys, value):
    with pytest.raises(SystemExit) as caught:
        run(fake_client, monkeypatch, ["--timeout", value, "info"])
    assert caught.value.code == cli.EXIT_USAGE and "UNIFI_TIMEOUT must be" in capsys.readouterr().err


def test_a_bad_timeout_in_the_environment_is_a_config_error(fake_client, monkeypatch, capsys):
    monkeypatch.setenv("UNIFI_TIMEOUT", "soon")
    assert run(fake_client, monkeypatch, ["info"]) == cli.EXIT_ERROR
    assert "UNIFI_TIMEOUT must be a number of seconds" in capsys.readouterr().err


# -- what each command does when a legacy read fails ---------------------------------------------------

def break_alluser(fake_client, monkeypatch):
    legacy_stat = fake_client.legacy_stat

    def fail(site_ref, resource):
        if resource == "alluser":
            raise UniFiAPIError("alluser unavailable")
        return legacy_stat(site_ref, resource)

    monkeypatch.setattr(fake_client, "legacy_stat", fail)


@pytest.mark.parametrize("argv", [["new-clients"], ["snapshot"]])
def test_commands_whose_answer_would_be_wrong_without_alluser_fail(fake_client, monkeypatch, capsys, tmp_path, argv):
    break_alluser(fake_client, monkeypatch)
    if argv == ["snapshot"]:
        argv = ["snapshot", "--dir", str(tmp_path / "snaps")]
    assert run(fake_client, monkeypatch, argv) == cli.EXIT_ERROR
    assert "alluser unavailable" in capsys.readouterr().err
    assert not (tmp_path / "snaps").exists()                      # no misleading snapshot was written


def test_diff_against_the_network_fails_when_alluser_cannot_be_read(fake_client, monkeypatch, capsys, tmp_path):
    folder = str(tmp_path / "snaps")
    assert run(fake_client, monkeypatch, ["snapshot", "--dir", folder]) == 0
    capsys.readouterr()
    break_alluser(fake_client, monkeypatch)
    assert run(fake_client, monkeypatch, ["diff", "--dir", folder]) == cli.EXIT_ERROR
    assert "alluser unavailable" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["client", "desktop", "--no-emoji"], ["diagnose", "--no-events", "--json"],
                                  ["query", "clients", "--include-offline"], ["query", "reservations"]])
def test_commands_that_can_work_without_alluser_warn_and_carry_on(fake_client, monkeypatch, capsys, argv):
    break_alluser(fake_client, monkeypatch)
    assert run(fake_client, monkeypatch, argv) in (0, 1)           # 1: diagnose findings, not an error
    captured = capsys.readouterr()
    assert "legacy stat/alluser unavailable" in captured.err and captured.out.strip()
