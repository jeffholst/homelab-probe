"""The fake controller is the demo controller: ``unifi_sentinel/demo`` (synthetic, sanitized, no real network data)."""

import os

import pytest

from unifi_sentinel import logs
from unifi_sentinel.client import UniFiClient
from unifi_sentinel.demo.session import (  # noqa: F401  (the tests import these names from here)
    FIXTURE,
    INTEGRATION,
    LEGACY,
    LEGACY_REST,
    LEGACY_V2,
    SYSTEM_LOG,
    FakeResponse,
)
from unifi_sentinel.demo.session import (
    DemoSession as FakeSession,
)

CONFIG_VARIABLES = ("CONTROLLER_URL", "API_KEY", "SITE_ID", "VERIFY_SSL", "ALLOW_INSECURE_HTTP", "TIMEOUT",
                    "PARALLEL_REQUESTS", "UNIFI_SENTINEL_ENV", "NOTIFY_NTFY_URL", "NOTIFY_NTFY_TOKEN", "NOTIFY_WEBHOOK_URL",
                    "NOTIFY_WEBHOOK_TOKEN", "NOTIFY_SMTP_HOST", "NOTIFY_SMTP_PORT", "NOTIFY_SMTP_SECURITY",
                    "NOTIFY_SMTP_USER", "NOTIFY_SMTP_PASSWORD", "NOTIFY_EMAIL_FROM", "NOTIFY_EMAIL_TO", "LOG_LEVEL",
                    "LOG_FORMAT")


@pytest.fixture(autouse=True)
def _isolated_environment(request, tmp_path, monkeypatch):
    """No test may depend on the developer's real .env, settings file or environment.

    Each test starts in an empty working directory (``.env`` and ``unifi-sentinel.toml``
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
    monkeypatch.setattr("unifi_sentinel.client.RETRY_BACKOFF_S", 0)    # retries must not make the suite wait
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
