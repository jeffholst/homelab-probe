"""Demo mode: every command against the synthetic controller, with nothing read from the machine or sent anywhere.

``unifi-sentinel --demo <command>`` builds its configuration and its client here instead of from ``.env``: the address
is on the ``.invalid`` top-level domain (which never resolves), the API key is a placeholder, and the session is a
``DemoSession`` that answers from the packaged fixture in memory. ``cli.main`` skips ``load_config`` entirely in this
mode, so no ``.env``, environment variable, settings file or notification destination can leak into a demo, and it
refuses the commands that would write to or read from the user's own state (``doctor``, ``snapshot``, ``diff``,
``--notify``).
"""

from ..client import UniFiClient
from ..config import DEFAULT_PARALLEL, DEFAULT_TIMEOUT, Config
from .session import FIXTURE, DemoSession

__all__ = ["DEMO_URL", "FIXTURE", "DemoSession", "demo_client", "demo_config"]

DEMO_URL = "https://demo.invalid"
DEMO_KEY = "demo-placeholder-not-a-secret"


def demo_config(site: str = "default", timeout: float = DEFAULT_TIMEOUT, parallel: int = DEFAULT_PARALLEL) -> Config:
    """The configuration of a demo run: never read from a file or the environment."""
    return Config(controller_url=DEMO_URL, api_key=DEMO_KEY, site=site, timeout=timeout, parallel=parallel)


def demo_client(config: Config) -> UniFiClient:
    """A client whose session is the synthetic controller (the real session it starts with is replaced at once and
    never used; the address would not resolve anyway)."""
    client = UniFiClient(config.controller_url, config.api_key, config.verify_ssl, config.timeout,
                         workers=config.parallel)
    client.session = DemoSession()
    return client
