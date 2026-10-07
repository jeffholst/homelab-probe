"""A stub UniFi controller for the end-to-end tests of the setup (issue #281).

It is an HTTPS server on 127.0.0.1 that answers what the setup's connection test and health check read, with the
synthetic demo network (`homelab_probe.demo`), so no real controller is ever involved. It holds nothing else:

* it shows one of the throwaway certificates of `tests/tls_fixtures.py` (they protect nothing): `good` is valid for
  127.0.0.1, `other` only for other.example, so it cannot be pinned;
* it answers only a request with the API key it was started with (`--key`); any other key gets `401`, like a
  controller that refuses a key;
* it prints one JSON line, `{"port": ..., "fingerprint": ...}`, when it is ready, and serves until it is stopped.

Run by `e2e/serve.mjs`: `uv run --project <repo> python web/e2e/stub_controller.py --key KEY --cert good`.
"""

import argparse
import hashlib
import json
import ssl
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict
from urllib.parse import parse_qsl, urlsplit

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests"))

import tls_fixtures as fixtures  # noqa: E402

from homelab_probe.demo.session import DemoSession  # noqa: E402


def fingerprint(pem: str) -> str:
    """SHA-256 of the certificate, as the setup shows it: hex pairs separated by colons."""
    der = ssl.PEM_cert_to_DER_cert(pem)
    return ":".join(f"{byte:02X}" for byte in hashlib.sha256(der).digest())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--key", required=True, help="The only API key the stub accepts")
    parser.add_argument("--cert", choices=("good", "other"), default="good")
    args = parser.parse_args()
    cert, key = (fixtures.GOOD_CERT, fixtures.GOOD_KEY) if args.cert == "good" else (
        fixtures.OTHERNAME_CERT, fixtures.OTHERNAME_KEY)
    session = DemoSession()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_: Any) -> None:       # nothing is logged: the tests read the harness output
            pass

        def send(self, status: int, body: Any) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def allowed(self) -> bool:
            if self.headers.get("X-API-KEY") == args.key:
                return True
            self.send(401, {"error": "unauthorized"})
            return False

        def do_GET(self) -> None:
            if not self.allowed():
                return
            parts = urlsplit(self.path)
            params: Dict[str, Any] = {k: int(v) if v.isdigit() else v for k, v in parse_qsl(parts.query)}
            answer = session.get(f"https://stub{parts.path}", params)
            self.send(answer.status_code, answer.json())

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            if not self.allowed():
                return
            answer = session.post(f"https://stub{urlsplit(self.path).path}", json=body)
            self.send(answer.status_code, answer.json())

    with tempfile.TemporaryDirectory() as directory:
        cert_file, key_file = Path(directory) / "cert.pem", Path(directory) / "key.pem"
        cert_file.write_text(cert)
        key_file.write_text(key)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert_file), str(key_file))
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    print(json.dumps({"port": server.server_address[1], "fingerprint": fingerprint(cert)}), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
