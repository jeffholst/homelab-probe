"""The fake controller is the demo controller: ``homelab_probe/demo`` (synthetic, sanitized, no real network data)."""

import os

import pytest

from homelab_probe import logs
from homelab_probe.accounts import ScryptParams
from homelab_probe.client import UniFiClient
from homelab_probe.demo.session import (  # noqa: F401  (the tests import these names from here)
    FIXTURE,
    INTEGRATION,
    LEGACY,
    LEGACY_REST,
    LEGACY_V2,
    SYSTEM_LOG,
    FakeResponse,
)
from homelab_probe.demo.session import (
    DemoSession as FakeSession,
)

CONFIG_VARIABLES = ("UNIFI_URL", "UNIFI_API_KEY", "UNIFI_SITE_ID", "UNIFI_VERIFY_SSL", "ALLOW_INSECURE_HTTP", "UNIFI_TIMEOUT",
                    "UNIFI_PARALLEL_REQUESTS", "HLP_ENV", "NOTIFY_NTFY_URL", "NOTIFY_NTFY_TOKEN", "NOTIFY_WEBHOOK_URL",
                    "NOTIFY_WEBHOOK_TOKEN", "NOTIFY_SMTP_HOST", "NOTIFY_SMTP_PORT", "NOTIFY_SMTP_SECURITY",
                    "NOTIFY_SMTP_USER", "NOTIFY_SMTP_PASSWORD", "NOTIFY_EMAIL_FROM", "NOTIFY_EMAIL_TO", "LOG_LEVEL",
                    "LOG_FORMAT", "AUDIT_LOG_MAX_MB", "AUDIT_LOG_FILES",
                    "SESSION_IDLE_MINUTES", "SESSION_MAX_HOURS")


@pytest.fixture(autouse=True)
def _isolated_environment(request, tmp_path, monkeypatch):
    """No test may depend on the developer's real .env, settings file or environment.

    Each test starts in an empty working directory (``.env`` and ``hlp.toml``
    are looked up in the current directory) with the configuration variables unset. A test
    that needs a file creates it there, or changes directory itself.
    """
    if request.node.get_closest_marker("live"):
        yield                      # the live tests exist to read the developer's real settings and controller
        return
    saved = dict(os.environ)       # load_dotenv writes os.environ directly, outside monkeypatch
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    for name in CONFIG_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("homelab_probe.client.RETRY_BACKOFF_S", 0)    # retries must not make the suite wait
    monkeypatch.setattr("homelab_probe.accounts.PARAMS", ScryptParams(n=16, r=8, p=1))    # scrypt at its real cost is slow
    logs.reset()                   # the logger, its secrets and its context are process-wide
    yield
    logs.reset()
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture
def fake_client():
    client = UniFiClient("https://controller", "key")
    client.session = FakeSession()
    return client
