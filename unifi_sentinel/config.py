"""Configuration loading from environment variables / .env."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    controller_url: str
    api_key: str
    site: str = "default"
    verify_ssl: bool = True


def load_config() -> Config:
    """Load and validate configuration from the environment (and .env)."""
    load_dotenv()

    controller_url = os.getenv("CONTROLLER_URL")
    api_key = os.getenv("API_KEY")

    if not controller_url:
        raise ConfigError(
            "CONTROLLER_URL is not set. Copy example.env to .env and configure it."
        )
    if not api_key or api_key == "your-api-key-here":
        raise ConfigError(
            "API_KEY is not set or is the placeholder value. Create one under "
            "Settings > Control Plane > Integrations and add it to .env."
        )

    verify_ssl = os.getenv("VERIFY_SSL", "true").lower() not in ("false", "0", "no")

    return Config(
        controller_url=controller_url.rstrip("/"),
        api_key=api_key,
        site=os.getenv("SITE_ID", "default"),
        verify_ssl=verify_ssl,
    )
