"""Email (SMTP) as the third notification destination: configuration, the message, TLS, failures, the command line.

No test touches a network: ``FakeSMTP`` stands in for ``smtplib.SMTP`` and ``SMTP_SSL`` and records what it was asked.
"""

import json
import re
import smtplib
import socket
import ssl
from pathlib import Path

import pytest
from test_notify import finding

from unifi_sentinel import cli
from unifi_sentinel import notify as notify_module
from unifi_sentinel.config import ConfigError, SmtpSettings, load_config, validate_smtp
from unifi_sentinel.diagnose import CRITICAL, WARNING
from unifi_sentinel.notify import Destination, destinations_from_config, plan, render_text, send

HOST, USER, PASSWORD = "smtp.secret-mail.example", "alerts-account@secret-mail.example", "SECRET-APP-PASSWORD-9137"
SENDER, TO_A, TO_B = "sentinel@secret-mail.example", "owner@secret-mail.example", "partner@other-secret.example"
SECRETS = (HOST, USER, PASSWORD, SENDER, TO_A, TO_B)


def settings(**changes):
    base = {"host": HOST, "port": 587, "security": "starttls", "sender": SENDER, "recipients": (TO_A,),
            "user": USER, "password": PASSWORD}
    return SmtpSettings(**{**base, **changes})


def events_for(*findings):
    return plan(list(findings), {"version": 1, "active": {}}, 1_800_000_000.0)[0]


class FakeSMTP:
    """A recording stand-in for smtplib.SMTP / SMTP_SSL. ``fail`` maps a step ('connect', 'starttls', 'login',
    'send') to the exception it raises; ``refused`` is what send_message reports."""

    def __init__(self, fail=None, refused=None):
        self.fail, self.refused = fail or {}, refused or {}
        self.log, self.opened, self.closed, self.sent = [], [], 0, []

    def _step(self, name):
        self.log.append(name)
        if name in self.fail:
            raise self.fail[name]

    def __call__(self, host, port, **kwargs):
        self.opened.append((host, port, kwargs))
        self._step("connect")
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed += 1
        return False

    def starttls(self, context=None):
        self.context = context
        self._step("starttls")

    def login(self, user, password):
        self.credentials = (user, password)
        self._step("login")

    def send_message(self, message, from_addr, to_addrs):
        self.sent.append((message, from_addr, list(to_addrs)))
        self._step("send")
        return self.refused


def deliver(smtp_settings, *findings_, redact=False, plain=None, tls=None, trace=None):
    plain, tls = plain or FakeSMTP(), tls or FakeSMTP()
    results = send([Destination("email", smtp=smtp_settings)], events_for(*findings_) or events_for(
        finding(CRITICAL, "Gateway", "offline")), redact, 12.0, trace=trace, smtp_plain=plain, smtp_ssl=tls)
    return results, plain, tls


# -- configuration ----------------------------------------------------------------------------------------------

BASE = {"NOTIFY_SMTP_HOST": HOST, "NOTIFY_EMAIL_FROM": SENDER, "NOTIFY_EMAIL_TO": TO_A}


def parse(extra=None, allow=False, drop=()):
    env = {**BASE, **(extra or {})}
    for name in drop:
        env.pop(name, None)
    return validate_smtp(env, allow)


def test_nothing_configured_means_no_email():
    assert validate_smtp({}) == (None, []) and validate_smtp({"NOTIFY_SMTP_HOST": "  "}) == (None, [])


def test_the_minimal_setup_is_starttls_on_587_without_a_login():
    smtp, warnings = parse()
    assert (smtp.host, smtp.port, smtp.security, smtp.user, smtp.password) == (HOST, 587, "starttls", "", "")
    assert smtp.sender == SENDER and smtp.recipients == (TO_A,) and warnings == []


@pytest.mark.parametrize("security, port", [("starttls", 587), ("STARTTLS", 587), (" ssl ", 465)])
def test_the_default_port_follows_the_security_mode(security, port):
    assert parse({"NOTIFY_SMTP_SECURITY": security})[0].port == port


def test_a_port_can_be_chosen():
    assert parse({"NOTIFY_SMTP_PORT": "2525"})[0].port == 2525 and parse({"NOTIFY_SMTP_PORT": " 25 "})[0].port == 25


@pytest.mark.parametrize("port", ["0", "65536", "-1", "abc", "5 87", "58.7", "٥٨٧", "1e3"])
def test_a_bad_port_is_refused(port):
    with pytest.raises(ConfigError, match="NOTIFY_SMTP_PORT must be a port number"):
        parse({"NOTIFY_SMTP_PORT": port})


def test_login_needs_both_user_and_password():
    smtp, _ = parse({"NOTIFY_SMTP_USER": USER, "NOTIFY_SMTP_PASSWORD": "app password with spaces"})
    assert smtp.user == USER and smtp.password == "app password with spaces"
    for only in ({"NOTIFY_SMTP_USER": USER}, {"NOTIFY_SMTP_PASSWORD": PASSWORD}):
        with pytest.raises(ConfigError, match="go together"):
            parse(only)


@pytest.mark.parametrize("name", ["NOTIFY_SMTP_USER", "NOTIFY_SMTP_PASSWORD"])
def test_control_characters_in_the_login_are_refused(name):
    with pytest.raises(ConfigError, match=f"{name} must not contain control characters"):
        parse({"NOTIFY_SMTP_USER": "u", "NOTIFY_SMTP_PASSWORD": "p", name: "bad\r\nvalue"})


def test_plain_smtp_needs_the_lab_opt_in_and_warns_every_time():
    with pytest.raises(ConfigError, match="ALLOW_INSECURE_HTTP"):
        parse({"NOTIFY_SMTP_SECURITY": "none"})
    smtp, warnings = parse({"NOTIFY_SMTP_SECURITY": "none"}, allow=True)
    assert smtp.security == "none" and smtp.port == 25 and warnings == [
        "NOTIFY_SMTP_SECURITY=none: notification email is sent unencrypted (allowed by ALLOW_INSECURE_HTTP)"]


def test_a_password_is_refused_over_plain_smtp_even_with_the_opt_in():
    with pytest.raises(ConfigError, match="refused") as caught:
        parse({"NOTIFY_SMTP_SECURITY": "none", "NOTIFY_SMTP_USER": USER, "NOTIFY_SMTP_PASSWORD": PASSWORD}, allow=True)
    assert PASSWORD not in str(caught.value)


def test_an_unknown_security_word_lists_the_valid_ones():
    with pytest.raises(ConfigError, match="starttls, ssl, none"):
        parse({"NOTIFY_SMTP_SECURITY": "tls"})


@pytest.mark.parametrize("host", ["smtps://mail.example", "mail.example:587", "mail example", "mail.example\r\nX: y",
                                  "-bad.example", "mail-.example", "mail.-bad.example", "mail.bad-.example",
                                  "mail..example", "[a:b]", "[2001:db8::25", "2001:db8::25]", "a@b.example",
                                  "x" * 300])
def test_a_bad_host_is_refused(host):
    with pytest.raises(ConfigError, match="NOTIFY_SMTP_HOST must be a host name"):
        parse({"NOTIFY_SMTP_HOST": host})


@pytest.mark.parametrize("host, expected", [
    ("mail.example.com", "mail.example.com"), ("localhost", "localhost"), ("relay-1.lan", "relay-1.lan"),
    ("10.0.0.25", "10.0.0.25"), ("[2001:db8::25]", "2001:db8::25"),
])
def test_good_hosts_are_kept_or_normalized(host, expected):
    assert parse({"NOTIFY_SMTP_HOST": host})[0].host == expected


def test_normalized_ipv6_host_is_passed_to_the_smtp_transport():
    smtp, _ = parse({"NOTIFY_SMTP_HOST": "[2001:db8::25]"})
    _, plain, _ = deliver(smtp)
    assert plain.opened[0][0] == "2001:db8::25"


def test_from_and_to_are_required_and_a_stray_setting_without_a_host_is_an_error():
    for name in ("NOTIFY_EMAIL_FROM", "NOTIFY_EMAIL_TO"):
        with pytest.raises(ConfigError, match="NOTIFY_EMAIL_FROM and NOTIFY_EMAIL_TO are required"):
            parse(drop=(name,))
    for name in ("NOTIFY_SMTP_PORT", "NOTIFY_EMAIL_TO", "NOTIFY_SMTP_USER", "NOTIFY_SMTP_SECURITY"):
        with pytest.raises(ConfigError, match=f"{name} is set but NOTIFY_SMTP_HOST is not"):
            validate_smtp({name: "x"})


@pytest.mark.parametrize("address", [
    "Owner <owner@example.com>", "owner", "owner@", "@example.com", "ow ner@example.com", "owner@exa mple.com",
    "owner@example.com\nBcc: evil@example.net", "owner@exam\rple.com", "a@b@c.example", '"quoted"@example.com',
    "owner@example.com;other@example.com", "owner@-example.com", "o" * 65 + "@example.com",
])
def test_addresses_are_plain_and_cannot_carry_a_header(address):
    for name in ("NOTIFY_EMAIL_FROM", "NOTIFY_EMAIL_TO"):
        with pytest.raises(ConfigError, match=f"{name}.*plain address") as caught:
            parse({name: address})
        assert "evil" not in str(caught.value) and "Owner <" not in str(caught.value)


def test_several_recipients_are_split_trimmed_deduplicated_and_counted():
    smtp, _ = parse({"NOTIFY_EMAIL_TO": f" {TO_A} , {TO_B},{TO_A}\r\n"})
    assert smtp.recipients == (TO_A, TO_B)
    with pytest.raises(ConfigError, match="entry 2 must be a plain address"):
        parse({"NOTIFY_EMAIL_TO": f"{TO_A},not-an-address"})
    with pytest.raises(ConfigError, match="more than 20"):
        parse({"NOTIFY_EMAIL_TO": ",".join(f"user{i}@example.com" for i in range(21))})


@pytest.mark.parametrize("extra", [
    {"NOTIFY_SMTP_SECURITY": "bogus"}, {"NOTIFY_SMTP_PORT": "x"}, {"NOTIFY_SMTP_USER": USER},
    {"NOTIFY_EMAIL_FROM": "bad address"}, {"NOTIFY_EMAIL_TO": f"{TO_A},bad address"},
    {"NOTIFY_SMTP_SECURITY": "none", "NOTIFY_SMTP_USER": USER, "NOTIFY_SMTP_PASSWORD": PASSWORD},
])
def test_no_configuration_error_repeats_a_value(extra):
    with pytest.raises(ConfigError) as caught:
        parse({"NOTIFY_SMTP_PASSWORD_UNUSED": "x", **extra}, allow=True)
    assert not any(secret in str(caught.value) for secret in SECRETS)


def test_the_settings_and_the_destination_hide_everything_in_repr(monkeypatch):
    smtp = settings(recipients=(TO_A, TO_B))
    shown = repr(smtp) + repr(Destination("email", smtp=smtp))
    assert not any(secret in shown for secret in SECRETS)
    for name, value in {**BASE, "NOTIFY_SMTP_USER": USER, "NOTIFY_SMTP_PASSWORD": PASSWORD,
                        "CONTROLLER_URL": "https://controller.example", "API_KEY": "key"}.items():
        monkeypatch.setenv(name, value)
    config = load_config()
    assert config.notify_smtp is not None and not any(secret in repr(config) for secret in SECRETS)


def test_load_config_reads_email_and_carries_the_plain_smtp_warning(monkeypatch):
    for name, value in {"CONTROLLER_URL": "https://controller.example", "API_KEY": "key", **BASE,
                        "NOTIFY_SMTP_SECURITY": "none", "ALLOW_INSECURE_HTTP": "true"}.items():
        monkeypatch.setenv(name, value)
    config = load_config()
    assert config.notify_smtp.security == "none" and any("unencrypted" in w for w in config.warnings)
    monkeypatch.delenv("ALLOW_INSECURE_HTTP")
    with pytest.raises(ConfigError, match="ALLOW_INSECURE_HTTP"):
        load_config()


def test_email_is_a_destination_after_ntfy_and_the_webhook(monkeypatch):
    for name, value in {"CONTROLLER_URL": "https://controller.example", "API_KEY": "key", **BASE,
                        "NOTIFY_NTFY_URL": "https://ntfy.example/t", "NOTIFY_WEBHOOK_URL": "https://hook.example/h"}.items():
        monkeypatch.setenv(name, value)
    assert [d.kind for d in destinations_from_config(load_config())] == ["ntfy", "webhook", "email"]
    monkeypatch.delenv("NOTIFY_SMTP_HOST")
    monkeypatch.delenv("NOTIFY_EMAIL_FROM")
    monkeypatch.delenv("NOTIFY_EMAIL_TO")
    assert [d.kind for d in destinations_from_config(load_config())] == ["ntfy", "webhook"]


# -- the message -----------------------------------------------------------------------------------------------------

def test_one_plain_text_message_goes_to_every_recipient():
    results, plain, _ = deliver(settings(recipients=(TO_A, TO_B)), finding(CRITICAL, "Gateway", "offline"),
                                finding(WARNING, "Office AP", "weak signal"))
    assert results == [("email", True, "sent")] and len(plain.sent) == 1
    message, from_addr, to_addrs = plain.sent[0]
    events = events_for(finding(CRITICAL, "Gateway", "offline"), finding(WARNING, "Office AP", "weak signal"))
    title, body = render_text(events)
    assert (from_addr, to_addrs) == (SENDER, [TO_A, TO_B])
    assert message["Subject"] == title and message["From"] == SENDER and message["To"] == f"{TO_A}, {TO_B}"
    assert message.get_content_type() == "text/plain" and not message.is_multipart()
    assert message.get_content().strip() == body
    assert message["Date"] and message["Message-ID"].endswith("@secret-mail.example>")


def test_the_subject_is_ascii_and_a_name_with_line_breaks_cannot_reach_a_header():
    nasty = finding(CRITICAL, "Gateway — café\r\nBcc: evil@example.net", "offline ✓")
    _, plain, _ = deliver(settings(), nasty)
    message = plain.sent[0][0]
    assert message["Subject"].isascii() and "\n" not in message["Subject"] and "Bcc" not in str(message["Subject"])
    assert message["Bcc"] is None and "evil@example.net" not in "".join(f"{k}: {v}" for k, v in message.items())
    assert "café" in message.get_content()                           # the body keeps the name, cleaned of controls
    assert "\r" not in message.get_content()


def test_redaction_leaves_no_name_address_or_mac_in_the_message():
    sensitive = finding(CRITICAL, "Gateway-of-the-Smiths", "offline at 10.1.2.3 (AA:BB:CC:DD:EE:FF)", code="device.offline")
    _, plain, _ = deliver(settings(), sensitive, redact=True)
    text = plain.sent[0][0].as_string()
    assert "Smiths" not in text and "10.1.2.3" not in text and "AA:BB:CC" not in text
    assert "A UniFi device is not online".lower() in text.lower() or "device" in text.lower()


def test_the_message_id_and_greeting_use_the_senders_domain_not_this_machines_name():
    _, plain, _ = deliver(settings())
    (host, port, kwargs), = plain.opened
    assert kwargs["local_hostname"] == "secret-mail.example"
    assert socket.getfqdn() not in plain.sent[0][0]["Message-ID"] or socket.getfqdn().endswith("secret-mail.example")


# -- TLS ---------------------------------------------------------------------------------------------------------------

def test_starttls_runs_before_the_login_and_verifies_the_certificate():
    results, plain, tls = deliver(settings())
    assert results == [("email", True, "sent")] and tls.opened == []
    assert plain.log == ["connect", "starttls", "login", "send"] and plain.closed == 1
    (host, port, kwargs), = plain.opened
    assert (host, port, kwargs["timeout"]) == (HOST, 587, 12.0) and "context" not in kwargs
    assert plain.context.verify_mode == ssl.CERT_REQUIRED and plain.context.check_hostname is True
    assert plain.credentials == (USER, PASSWORD)


def test_implicit_tls_connects_securely_and_never_sends_starttls():
    results, plain, tls = deliver(settings(security="ssl", port=465))
    assert results == [("email", True, "sent")] and plain.opened == []
    assert tls.log == ["connect", "login", "send"] and tls.closed == 1
    (host, port, kwargs), = tls.opened
    assert (host, port) == (HOST, 465)
    assert kwargs["context"].verify_mode == ssl.CERT_REQUIRED and kwargs["context"].check_hostname is True


def test_no_login_is_attempted_without_a_user():
    _, plain, _ = deliver(settings(user="", password=""))
    assert plain.log == ["connect", "starttls", "send"]


def test_plain_smtp_without_a_password_sends_without_tls():
    results, plain, _ = deliver(settings(security="none", port=25, user="", password=""))
    assert results == [("email", True, "sent")] and plain.log == ["connect", "send"]


def test_a_password_is_never_sent_without_encryption_even_if_the_settings_were_built_by_hand():
    results, plain, tls = deliver(settings(security="none", port=25))
    assert results == [("email", False, "a password is never sent without encryption")]
    assert plain.opened == [] and tls.opened == [] and not any(secret in str(results) for secret in SECRETS)


# -- failures ------------------------------------------------------------------------------------------------------------

LEAK = f"{HOST} {USER} {PASSWORD} {SENDER} {TO_A}"


@pytest.mark.parametrize("step, error, reason", [
    ("login", smtplib.SMTPAuthenticationError(535, f"5.7.8 {LEAK} rejected".encode()), "authentication failed"),
    ("send", smtplib.SMTPRecipientsRefused({TO_A: (550, f"{LEAK} no such user".encode())}), "recipient refused"),
    ("send", smtplib.SMTPSenderRefused(550, f"{LEAK}".encode(), SENDER), "sender refused"),
    ("starttls", smtplib.SMTPNotSupportedError(f"STARTTLS extension not supported by server {LEAK}"),
     "the server does not offer the required security or login"),
    ("starttls", ssl.SSLCertVerificationError(1, f"certificate verify failed: Hostname mismatch {LEAK}"), "TLS error"),
    ("connect", ssl.SSLError(1, f"handshake failure {LEAK}"), "TLS error"),
    ("connect", TimeoutError(f"timed out {LEAK}"), "timed out"),
    ("connect", TimeoutError(f"timed out {LEAK}"), "timed out"),
    ("send", smtplib.SMTPServerDisconnected(f"Connection unexpectedly closed {LEAK}"), "server disconnected"),
    ("send", smtplib.SMTPDataError(554, f"{LEAK} rejected as spam".encode()), "server rejected the message"),
    ("send", smtplib.SMTPException(LEAK), "server rejected the message"),
    ("connect", ConnectionRefusedError(61, f"Connection refused {LEAK}"), "connection error"),
    ("connect", socket.gaierror(8, f"nodename nor servname provided {LEAK}"), "connection error"),
    ("connect", OSError(LEAK), "connection error"),
])
def test_each_failure_gives_a_fixed_reason_and_never_the_secrets(step, error, reason):
    results, plain, _ = deliver(settings(recipients=(TO_A, TO_B)), plain=FakeSMTP(fail={step: error}))
    assert results == [("email", False, reason)]
    assert not any(secret in str(results) + repr(results) for secret in SECRETS)


def test_a_failure_after_connecting_still_closes_the_connection():
    _, plain, _ = deliver(settings(), plain=FakeSMTP(fail={"login": smtplib.SMTPAuthenticationError(535, b"x")}))
    assert plain.closed == 1 and "send" not in plain.log


def test_an_address_the_message_builder_rejects_is_a_fixed_reason_not_a_crash():
    results, plain, _ = deliver(settings(sender="sentinel@secret-mail.example\nBcc: evil@example.net"))
    assert results == [("email", False, "the message could not be built (an invalid address)")]
    assert plain.sent == []


def test_some_recipients_refused_is_still_a_delivery_that_says_so():
    results, plain, _ = deliver(settings(recipients=(TO_A, TO_B)),
                                plain=FakeSMTP(refused={TO_B: (550, f"{LEAK}".encode())}))
    assert results == [("email", True, "sent to 1 of 2 recipients (the others were refused)")]
    assert not any(secret in str(results) for secret in SECRETS)


def test_every_destination_is_tried_whatever_the_others_did():
    class Post:
        def __call__(self, url, **kwargs):
            raise notify_module.requests.exceptions.ConnectionError(f"{LEAK} {url}")

    events = events_for(finding(CRITICAL, "Gateway", "offline"))
    results = send([Destination("ntfy", "https://ntfy.example/topic-secret"), Destination("email", smtp=settings())],
                   events, False, 5.0, post=Post(), smtp_plain=FakeSMTP(), smtp_ssl=FakeSMTP())
    assert results == [("ntfy", False, "connection error"), ("email", True, "sent")]


def test_the_trace_line_names_the_destination_and_the_outcome_only():
    lines = []
    deliver(settings(), trace=lines.append)
    assert len(lines) == 1 and re.fullmatch(r"notify email -> sent \(\d+ ms\)", lines[0])
    lines.clear()
    deliver(settings(), plain=FakeSMTP(fail={"login": smtplib.SMTPAuthenticationError(535, LEAK.encode())}),
            trace=lines.append)
    assert re.fullmatch(r"notify email -> authentication failed \(\d+ ms\)", lines[0])


# -- the command line -----------------------------------------------------------------------------------------------------

def email_env(monkeypatch, **extra):
    for name, value in {"CONTROLLER_URL": "https://controller.example", "API_KEY": "sekret-api-key-0123456789",
                        "NOTIFY_SMTP_HOST": HOST, "NOTIFY_SMTP_USER": USER, "NOTIFY_SMTP_PASSWORD": PASSWORD,
                        "NOTIFY_EMAIL_FROM": SENDER, "NOTIFY_EMAIL_TO": f"{TO_A},{TO_B}", **extra}.items():
        monkeypatch.setenv(name, value)


def run(fake_client, monkeypatch, *argv, plain=None):
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, c: fake_client))
    monkeypatch.setattr(notify_module.smtplib, "SMTP", plain or FakeSMTP())
    return cli.main(["diagnose", "--no-events", "--json", "--notify", *argv])


def test_email_alone_is_enough_for_notify_and_sends_one_message_then_stays_quiet(fake_client, monkeypatch, capsys,
                                                                                tmp_path):
    email_env(monkeypatch)
    smtp, state = FakeSMTP(), tmp_path / "state.json"
    assert run(fake_client, monkeypatch, "--notify-state", str(state), plain=smtp) == 1
    captured = capsys.readouterr()
    assert "Notification to email: sent" in captured.err and json.loads(captured.out)["summary"]["warning"] >= 1
    (message, from_addr, to_addrs), = smtp.sent
    assert (from_addr, to_addrs) == (SENDER, [TO_A, TO_B]) and "NEW  Garage AP: device is offline" in message.get_content()
    assert json.loads(state.read_text())["active"]
    assert run(fake_client, monkeypatch, "--notify-state", str(state), plain=smtp) == 1
    assert "nothing new, worse or fixed" in capsys.readouterr().err and len(smtp.sent) == 1


def test_a_failed_email_keeps_the_state_and_is_retried_and_names_only_the_destination(fake_client, monkeypatch, capsys,
                                                                                      tmp_path):
    email_env(monkeypatch)
    state = tmp_path / "state.json"
    bad = FakeSMTP(fail={"login": smtplib.SMTPAuthenticationError(535, LEAK.encode())})
    assert run(fake_client, monkeypatch, "--notify-state", str(state), plain=bad) == 1
    err = capsys.readouterr().err
    assert "Notification to email: FAILED (authentication failed)" in err and not state.exists()
    assert not any(secret in err for secret in SECRETS)
    good = FakeSMTP()
    run(fake_client, monkeypatch, "--notify-state", str(state), plain=good)
    assert len(good.sent) == 1 and state.exists()                           # the same findings are sent the next run


def test_dry_run_and_baseline_open_no_connection(fake_client, monkeypatch, capsys, tmp_path):
    email_env(monkeypatch)
    smtp = FakeSMTP()
    run(fake_client, monkeypatch, "--notify-dry-run", "--notify-state", str(tmp_path / "s.json"), plain=smtp)
    assert "Notification dry run" in capsys.readouterr().err and smtp.opened == []
    run(fake_client, monkeypatch, "--notify-baseline", "--notify-state", str(tmp_path / "s.json"), plain=smtp)
    assert smtp.opened == [] and (tmp_path / "s.json").exists()


def test_redaction_reaches_the_email(fake_client, monkeypatch, capsys, tmp_path):
    email_env(monkeypatch)
    smtp = FakeSMTP()
    run(fake_client, monkeypatch, "--notify-redact", "--notify-state", str(tmp_path / "s.json"), plain=smtp)
    text = smtp.sent[0][0].as_string()
    assert "Garage AP" not in text and "Office Switch" not in text and "10.0.0" not in text


def test_nothing_secret_is_printed_by_a_verbose_run(fake_client, monkeypatch, capsys, tmp_path):
    email_env(monkeypatch)
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, c: fake_client))
    monkeypatch.setattr(notify_module.smtplib, "SMTP", FakeSMTP())
    cli.main(["--verbose", "diagnose", "--no-events", "--notify", "--notify-state", str(tmp_path / "s.json")])
    captured = capsys.readouterr()
    assert "notify email -> sent" in captured.err
    assert not any(secret in captured.out + captured.err for secret in (*SECRETS, "sekret-api-key"))


def test_the_missing_destination_hint_mentions_email(fake_client, monkeypatch, capsys):
    monkeypatch.setenv("CONTROLLER_URL", "https://controller.example")
    monkeypatch.setenv("API_KEY", "key")
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, c: fake_client))
    assert cli.main(["diagnose", "--no-events", "--notify"]) == cli.EXIT_ERROR
    assert "NOTIFY_SMTP_HOST" in capsys.readouterr().err


def test_a_bad_email_setting_stops_every_command_before_any_request(fake_client, monkeypatch, capsys):
    email_env(monkeypatch, NOTIFY_EMAIL_TO="not an address")
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, c: fake_client))
    assert cli.main(["info"]) == cli.EXIT_ERROR
    err = capsys.readouterr().err
    assert "NOTIFY_EMAIL_TO entry 1" in err and "not an address" not in err and fake_client.session.calls == []


# -- the guard: still the one outbound channel ---------------------------------------------------------------------------

def test_only_notify_py_speaks_smtp_and_it_still_never_touches_the_controller_client():
    package = Path(notify_module.__file__).parent
    senders = [p.name for p in package.rglob("*.py") if re.search(r"\bsmtplib\b", p.read_text(encoding="utf-8"))]
    assert sorted(senders) == ["notify.py"]
    source = Path(notify_module.__file__).read_text(encoding="utf-8")
    assert len(re.findall(r"smtplib\.SMTP(?:_SSL)?\b", source)) >= 2 and "UniFiClient" not in source
    assert "starttls" in source and "create_default_context" in source           # the only connections verify TLS
    assert "ssl._create_unverified_context" not in source and "CERT_NONE" not in source and "check_hostname = False" not in source
