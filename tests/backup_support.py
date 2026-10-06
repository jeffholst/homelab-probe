"""A data directory with every kind of file a backup holds, for the backup and restore tests."""

from homelab_probe.accounts import AccountStore
from homelab_probe.notes import NotesStore
from homelab_probe.triage import TriageStore

NOW = 1_900_000_000.0
KEY = "the-api-key-0123456789"
NOTE = "the secret reason for the note"
CERT = "-----BEGIN CERTIFICATE-----\nMIIBszCCAVmgAwIBAgIUAAAA\n-----END CERTIFICATE-----\n"
ENV = f"UNIFI_URL=https://controller.example\nUNIFI_API_KEY={KEY}\nNOTIFY_NTFY_URL=https://ntfy.example.com/lab-alerts\n"
TOML = "[thresholds]\nslow_link_mbps = 100\n"


def make_data(base, *, admin=True):
    """A data directory with every kind of file a backup holds (and some it must leave out)."""
    store = AccountStore(base)
    if admin:
        store.add("alice", "admin", "correct horse battery")
    store.add("bob", "viewer", "correct horse battery")
    (base / "hlp.toml").write_text(TOML)
    (base / ".env").write_text(ENV)
    (base / "certs").mkdir()
    (base / "certs" / "controller.pem").write_text(CERT)
    site = base / "snapshots" / "site-1"
    NotesStore(site, "site-1").add("device:AA:BB:CC:00:00:01", NOTE, "alice", NOW)
    NotesStore(site, "site-1").add("finding:" + "a" * 16, "another", "alice", NOW)
    TriageStore(site, "site-1").set_state("b" * 16, "wan.availability", "acknowledged", "alice", NOW)
    (site / "snapshot-20260101-000000Z.json").write_text('{"schema_version": 1}')
    (site / "notify-state.json").write_text("{}")
    (base / "snapshots" / "snapshot-20250101-000000Z.json").write_text('{"schema_version": 1}')
    (base / "audit.log").write_text('{"event": "x"}\n')
    (base / "audit.log.1").write_text('{"event": "old"}\n')
    return base


