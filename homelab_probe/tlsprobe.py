"""Looking at the certificate a controller shows, for the guided setup (``server/wizard.py``).

A controller at home usually has a self-signed certificate, so the setup offers to **pin** it: fetch the certificate the
controller shows, let the owner compare its fingerprint, and save it as the file ``UNIFI_VERIFY_SSL`` points at. This
module does the two network steps and nothing else:

* ``fetch_certificate`` completes a TLS handshake **without verifying** (the point is to see an unknown certificate)
  and returns it. Nothing is sent after the handshake, so no API key can reach a server that was not verified.
* ``check_pin`` completes a second handshake that trusts **only** that certificate and the address the owner typed,
  the same way the client will later verify it, and says why it failed when it does.

The address comes from a person who holds the setup token, so it is checked first (``resolve_target``): ``https`` only,
no credentials in the URL, and the address must not be one that no controller has (unspecified, multicast, link-local,
which includes the cloud metadata address, reserved) nor, unless ``allow_public`` says so, a public one. The connection
goes to the address that was checked, not to a second lookup of the name. Failures are ``ProbeError`` with a fixed
``reason`` and fixed words: never the text of the library error, which can carry the address or a path.
"""

import hashlib
import ipaddress
import socket
import ssl
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple
from urllib.parse import urlsplit

from urllib3.util.ssl_ import create_urllib3_context

DEFAULT_PORT = 443
DEFAULT_TIMEOUT = 5.0

REASONS = {
    "bad_url": "the address is not a usable address starting with https",
    "unresolvable": "the host name could not be resolved",
    "blocked_address": "this address cannot be a controller on your network",
    "public_address": "this is a public address; the guided setup only connects to addresses on your own network",
    "timeout": "the connection timed out",
    "connection": "the connection failed",
    "not_tls": "the server did not complete a TLS handshake",
    "no_certificate": "the server did not show a certificate",
    "invalid_ca": "a certificate in the chain is not usable as a CA certificate",
    "hostname_mismatch": "the certificate is not valid for this host name or address",
    "untrusted": "the certificate was not accepted",
}

Resolver = Callable[[str, int], List[str]]


class ProbeError(Exception):
    """A step that failed, with a ``reason`` (a key of ``REASONS``) and a message made of fixed words."""

    def __init__(self, reason: str) -> None:
        super().__init__(REASONS[reason])
        self.reason = reason


@dataclass(frozen=True)
class Certificate:
    pem: str
    fingerprint: str             # SHA-256 of the DER form, as hex pairs separated by colons


def system_resolver(host: str, port: int) -> List[str]:
    """Every address of ``host`` (``[]`` when it cannot be resolved), in the order the system prefers."""
    try:
        found = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return []
    addresses: List[str] = []
    for item in found:
        address = str(item[4][0]).split("%", 1)[0]
        if address not in addresses:
            addresses.append(address)
    return addresses


def split_url(url: str) -> Tuple[str, int]:
    """(host, port) of an ``https://`` address; ``ProbeError("bad_url")`` for anything else."""
    try:
        parts = urlsplit(url.strip())
        host, port = parts.hostname, parts.port
    except ValueError:
        raise ProbeError("bad_url") from None
    if parts.scheme != "https" or not host or parts.username is not None or parts.password is not None:
        raise ProbeError("bad_url")
    return host, DEFAULT_PORT if port is None else port


def address_problem(address: str, allow_public: bool = False) -> Optional[str]:
    """The reason (a key of ``REASONS``) why a connection to this address is refused, or None."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return "blocked_address"
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_loopback:                      # before is_reserved, which also covers ::1
        return None
    if (ip.is_unspecified or ip.is_multicast or ip.is_link_local or ip.is_reserved
            or (isinstance(ip, ipaddress.IPv4Address) and ip.packed[0] == 0)):
        return "blocked_address"
    if ip.is_global and not allow_public:
        return "public_address"
    return None


def resolve_target(url: str, allow_public: bool = False,
                   resolver: Resolver = system_resolver) -> Tuple[str, int, str]:
    """(host, port, address to connect to) for an address that may be probed, else ``ProbeError``. Every address
    the name resolves to must be acceptable, so a name cannot hide a refused address behind an allowed one."""
    host, port = split_url(url)
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        addresses = resolver(host, port)
    if not addresses:
        raise ProbeError("unresolvable")
    for address in addresses:
        problem = address_problem(address, allow_public)
        if problem is not None:
            raise ProbeError(problem)
    return host, port, addresses[0]


def _connect(address: str, port: int, timeout: float) -> socket.socket:
    try:
        return socket.create_connection((address, port), timeout=timeout)
    except TimeoutError:
        raise ProbeError("timeout") from None
    except OSError:
        raise ProbeError("connection") from None


def fetch_certificate(url: str, allow_public: bool = False, timeout: float = DEFAULT_TIMEOUT,
                      resolver: Resolver = system_resolver) -> Certificate:
    """The certificate the server at ``url`` shows. It is **not verified** (that is what it is fetched for) and
    nothing is sent over the connection."""
    host, port, address = resolve_target(url, allow_public, resolver)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with _connect(address, port, timeout) as raw:
        try:
            with context.wrap_socket(raw, server_hostname=host) as secured:
                der = secured.getpeercert(binary_form=True)
        except TimeoutError:
            raise ProbeError("timeout") from None
        except (ssl.SSLError, OSError):
            raise ProbeError("not_tls") from None
    if not der:
        raise ProbeError("no_certificate")
    fingerprint = ":".join(f"{byte:02X}" for byte in hashlib.sha256(der).digest())
    return Certificate(ssl.DER_cert_to_PEM_cert(der), fingerprint)


def classify(error: ssl.SSLError) -> str:
    """Why a verification failed, as a key of ``REASONS``: from OpenSSL's verify code when there is one (24 is an
    invalid CA, 62 and 64 a host name or address that is not in the certificate), else from its wording."""
    code = getattr(error, "verify_code", None)
    text = str(getattr(error, "verify_message", "") or error)
    if code == 24 or "invalid CA certificate" in text:
        return "invalid_ca"
    if code in (62, 64) or "Hostname mismatch" in text or "IP address mismatch" in text:
        return "hostname_mismatch"
    return "untrusted"


def check_pin(url: str, pem: str, allow_public: bool = False, timeout: float = DEFAULT_TIMEOUT,
              resolver: Resolver = system_resolver) -> None:
    """Return when the server at ``url`` is accepted with ``pem`` as the only trusted certificate and the host
    verified against it; raise ``ProbeError`` with the reason when it is not. The context is the one ``requests``
    builds for ``verify=<file>``, so the answer is the one the client will get."""
    host, port, address = resolve_target(url, allow_public, resolver)
    context = create_urllib3_context()
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    try:
        context.load_verify_locations(cadata=pem)
    except (ssl.SSLError, ValueError):
        raise ProbeError("untrusted") from None
    with _connect(address, port, timeout) as raw:
        try:
            with context.wrap_socket(raw, server_hostname=host):
                return
        except TimeoutError:
            raise ProbeError("timeout") from None
        except ssl.SSLCertVerificationError as error:
            raise ProbeError(classify(error)) from None
        except (ssl.SSLError, OSError):
            raise ProbeError("not_tls") from None
