import socket
import ssl

import pytest
import tls_fixtures as fx

from homelab_probe import tlsprobe
from homelab_probe.tlsprobe import ProbeError

LOCAL = "127.0.0.1"


def reason_of(call, *args, **kwargs):
    with pytest.raises(ProbeError) as raised:
        call(*args, **kwargs)
    return raised.value.reason


# -- the address -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("url", ["http://192.168.1.1", "ftp://192.168.1.1", "192.168.1.1", "https://", "https:///x",
                                 "https://user:pw@192.168.1.1", "https://user@192.168.1.1", "https://[::1",
                                 "https://192.168.1.1:99999", "https://192.168.1.1:abc"])
def test_only_a_plain_https_address_is_accepted(url):
    assert reason_of(tlsprobe.split_url, url) == "bad_url"


def test_the_port_defaults_to_443():
    assert tlsprobe.split_url("https://192.168.1.1") == ("192.168.1.1", 443)
    assert tlsprobe.split_url(" https://unifi.lan:8443/path ") == ("unifi.lan", 8443)


@pytest.mark.parametrize("address", ["0.0.0.0", "0.1.2.3", "224.0.0.1", "169.254.169.254", "169.254.1.1", "::", "ff02::1",
                                     "fe80::1", "240.0.0.1", "::ffff:169.254.169.254", "::ffff:0.0.0.0", "nonsense"])
def test_addresses_no_controller_has_are_always_refused(address):
    assert tlsprobe.address_problem(address) == "blocked_address"
    assert tlsprobe.address_problem(address, allow_public=True) == "blocked_address"


@pytest.mark.parametrize("address", ["192.168.1.1", "10.0.0.1", "172.16.0.1", "127.0.0.1", "100.64.0.1", "fd00::1", "::1",
                                     "192.0.2.10", "::ffff:192.168.1.1"])
def test_private_and_loopback_addresses_are_allowed(address):
    assert tlsprobe.address_problem(address) is None


@pytest.mark.parametrize("address", ["8.8.8.8", "1.1.1.1", "2606:4700::1111", "::ffff:8.8.8.8"])
def test_public_addresses_need_the_option(address):
    assert tlsprobe.address_problem(address) == "public_address"
    assert tlsprobe.address_problem(address, allow_public=True) is None


def test_a_name_is_resolved_and_every_address_must_be_acceptable():
    calls = []

    def resolver(host, port):
        calls.append((host, port))
        return {"unifi.lan": ["192.168.1.1"], "mixed.lan": ["192.168.1.1", "169.254.169.254"],
                "none.lan": []}[host]

    assert tlsprobe.resolve_target("https://unifi.lan:8443", resolver=resolver) == ("unifi.lan", 8443, "192.168.1.1")
    assert calls == [("unifi.lan", 8443)]
    assert reason_of(tlsprobe.resolve_target, "https://mixed.lan", resolver=resolver) == "blocked_address"
    assert reason_of(tlsprobe.resolve_target, "https://none.lan", resolver=resolver) == "unresolvable"


def test_an_ip_literal_is_not_resolved():
    def resolver(host, port):
        raise AssertionError("an address is not looked up")

    assert tlsprobe.resolve_target("https://192.168.1.1", resolver=resolver)[2] == "192.168.1.1"
    assert tlsprobe.resolve_target("https://[fd00::1]:8443", resolver=resolver) == ("fd00::1", 8443, "fd00::1")
    assert reason_of(tlsprobe.resolve_target, "https://169.254.169.254", resolver=resolver) == "blocked_address"


def test_the_system_resolver(monkeypatch):
    found = [(socket.AF_INET, 0, 0, "", ("192.168.1.1", 443)), (socket.AF_INET, 0, 0, "", ("192.168.1.1", 443)),
             (socket.AF_INET6, 0, 0, "", ("fe80::1%en0", 443, 0, 0))]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: found)
    assert tlsprobe.system_resolver("unifi.lan", 443) == ["192.168.1.1", "fe80::1"]

    def broken(*a, **k):
        raise socket.gaierror("no")

    monkeypatch.setattr(socket, "getaddrinfo", broken)
    assert tlsprobe.system_resolver("unifi.lan", 443) == []


# -- fetching and pinning ----------------------------------------------------------------------------------------

def test_the_certificate_is_fetched_without_being_verified():
    with fx.tls_server(fx.OTHERNAME_CERT, fx.OTHERNAME_KEY) as port:
        found = tlsprobe.fetch_certificate(f"https://{LOCAL}:{port}")
    assert found.pem.strip() == fx.OTHERNAME_CERT.strip()
    assert len(found.fingerprint.split(":")) == 32
    assert found.fingerprint == found.fingerprint.upper()


def test_the_fingerprint_is_the_sha256_of_the_certificate():
    import hashlib

    der = ssl.PEM_cert_to_DER_cert(fx.GOOD_CERT)
    digest = hashlib.sha256(der).hexdigest().upper()
    with fx.tls_server(fx.GOOD_CERT, fx.GOOD_KEY) as port:
        found = tlsprobe.fetch_certificate(f"https://{LOCAL}:{port}")
    assert found.fingerprint.replace(":", "") == digest


def test_a_pinned_certificate_is_accepted_for_its_own_address():
    with fx.tls_server(fx.GOOD_CERT, fx.GOOD_KEY) as port:
        found = tlsprobe.fetch_certificate(f"https://{LOCAL}:{port}")
        tlsprobe.check_pin(f"https://{LOCAL}:{port}", found.pem)
        tlsprobe.check_pin(f"https://localhost:{port}", found.pem, resolver=lambda host, port: [LOCAL])


def test_a_certificate_for_another_name_is_a_hostname_mismatch():
    with fx.tls_server(fx.OTHERNAME_CERT, fx.OTHERNAME_KEY) as port:
        found = tlsprobe.fetch_certificate(f"https://{LOCAL}:{port}")
        assert reason_of(tlsprobe.check_pin, f"https://{LOCAL}:{port}", found.pem) == "hostname_mismatch"


def test_a_different_certificate_than_the_pinned_one_is_untrusted():
    with fx.tls_server(fx.GOOD_CERT, fx.GOOD_KEY) as port:
        assert reason_of(tlsprobe.check_pin, f"https://{LOCAL}:{port}", fx.OTHERCA_CERT) == "untrusted"


def test_a_pin_that_is_not_a_certificate_is_untrusted():
    with fx.tls_server(fx.GOOD_CERT, fx.GOOD_KEY) as port:
        assert reason_of(tlsprobe.check_pin, f"https://{LOCAL}:{port}", "not a certificate") == "untrusted"


# -- failures ---------------------------------------------------------------------------------------------------

def free_port():
    with socket.socket() as sock:
        sock.bind((LOCAL, 0))
        return sock.getsockname()[1]


def test_a_refused_connection():
    port = free_port()
    assert reason_of(tlsprobe.fetch_certificate, f"https://{LOCAL}:{port}") == "connection"
    assert reason_of(tlsprobe.check_pin, f"https://{LOCAL}:{port}", fx.GOOD_CERT) == "connection"


def test_a_timeout_is_told_apart(monkeypatch):
    def slow(*args, **kwargs):
        raise TimeoutError("timed out")

    monkeypatch.setattr(socket, "create_connection", slow)
    assert reason_of(tlsprobe.fetch_certificate, f"https://{LOCAL}:9") == "timeout"


def silent_server():
    """A server that accepts and says nothing, so a handshake waits."""
    listener = socket.socket()
    listener.bind((LOCAL, 0))
    listener.listen(5)
    return listener


def test_a_handshake_that_never_completes_times_out():
    listener = silent_server()
    try:
        port = listener.getsockname()[1]
        assert reason_of(tlsprobe.fetch_certificate, f"https://{LOCAL}:{port}", timeout=0.2) == "timeout"
        assert reason_of(tlsprobe.check_pin, f"https://{LOCAL}:{port}", fx.GOOD_CERT, timeout=0.2) == "timeout"
    finally:
        listener.close()


def test_a_server_that_is_not_tls():
    import threading

    listener = silent_server()
    port = listener.getsockname()[1]

    def answer():
        for _ in range(2):
            connection, _ = listener.accept()
            connection.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            connection.close()

    thread = threading.Thread(target=answer, daemon=True)
    thread.start()
    try:
        assert reason_of(tlsprobe.fetch_certificate, f"https://{LOCAL}:{port}") == "not_tls"
        assert reason_of(tlsprobe.check_pin, f"https://{LOCAL}:{port}", fx.GOOD_CERT) == "not_tls"
    finally:
        thread.join(2)
        listener.close()


def test_a_server_that_shows_no_certificate(monkeypatch):
    monkeypatch.setattr(ssl.SSLSocket, "getpeercert", lambda self, binary_form=False: None)
    with fx.tls_server(fx.GOOD_CERT, fx.GOOD_KEY) as port:
        assert reason_of(tlsprobe.fetch_certificate, f"https://{LOCAL}:{port}") == "no_certificate"


def test_a_blocked_address_is_refused_before_any_connection(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("no connection may be made")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    assert reason_of(tlsprobe.fetch_certificate, "https://169.254.169.254") == "blocked_address"
    assert reason_of(tlsprobe.check_pin, "https://8.8.8.8", fx.GOOD_CERT) == "public_address"


def test_the_connection_goes_to_the_address_that_was_checked(monkeypatch):
    seen = []

    def refuse(target, timeout=None):
        seen.append(target)
        raise ConnectionRefusedError("no")

    monkeypatch.setattr(socket, "create_connection", refuse)
    assert reason_of(tlsprobe.fetch_certificate, "https://unifi.lan:8443", resolver=lambda h, p: ["192.168.1.1"]) \
        == "connection"
    assert seen == [("192.168.1.1", 8443)]


def test_a_pin_the_library_cannot_load_is_untrusted(monkeypatch):
    def broken(self, *args, **kwargs):
        raise ssl.SSLError("bad")

    monkeypatch.setattr(ssl.SSLContext, "load_verify_locations", broken)
    assert reason_of(tlsprobe.check_pin, "https://192.168.1.1", fx.GOOD_CERT) == "untrusted"


# -- classification ---------------------------------------------------------------------------------------------

def verify_error(code, message):
    error = ssl.SSLCertVerificationError(1, f"[SSL: CERTIFICATE_VERIFY_FAILED] {message}")
    error.verify_code, error.verify_message = code, message
    return error


@pytest.mark.parametrize("code, message, expected", [
    (24, "invalid CA certificate", "invalid_ca"),
    (62, "Hostname mismatch, certificate is not valid for 'x'.", "hostname_mismatch"),
    (64, "IP address mismatch, certificate is not valid for '1.2.3.4'.", "hostname_mismatch"),
    (18, "self-signed certificate", "untrusted"),
    (20, "unable to get local issuer certificate", "untrusted"),
])
def test_a_failed_verification_is_classified_by_its_verify_code(code, message, expected):
    assert tlsprobe.classify(verify_error(code, message)) == expected


def test_classification_falls_back_to_the_wording():
    assert tlsprobe.classify(ssl.SSLError("certificate verify failed: invalid CA certificate")) == "invalid_ca"
    assert tlsprobe.classify(ssl.SSLError("Hostname mismatch")) == "hostname_mismatch"
    assert tlsprobe.classify(ssl.SSLError("IP address mismatch")) == "hostname_mismatch"
    assert tlsprobe.classify(ssl.SSLError("something else")) == "untrusted"


def test_an_invalid_ca_from_the_handshake_is_reported(monkeypatch):
    def fail(self, *args, **kwargs):
        raise verify_error(24, "invalid CA certificate")

    monkeypatch.setattr(ssl.SSLContext, "wrap_socket", fail)
    with fx.tls_server(fx.GOOD_CERT, fx.GOOD_KEY) as port:
        assert reason_of(tlsprobe.check_pin, f"https://{LOCAL}:{port}", fx.GOOD_CERT) == "invalid_ca"


def test_every_reason_has_a_message_and_none_has_an_address():
    for reason, message in tlsprobe.REASONS.items():
        assert message and message[0].islower() and "://" not in message
        assert str(ProbeError(reason)) == message
