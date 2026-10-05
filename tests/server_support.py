"""Shared by the server tests: accounts, a logged-in browser and the headers an unsafe request needs."""

from pathlib import Path
from typing import Optional

from fastapi.testclient import TestClient

from homelab_probe.config import Config
from homelab_probe.server.auth import AuthState
from homelab_probe.server.sessions import SessionStore
from homelab_probe.server.throttle import LoginThrottle

CONFIG = Config(controller_url="https://controller.example", api_key="the-api-key-0123456789", parallel=4)
PASSWORD = "correct horse battery"
USERS = {"alice": "admin", "bob": "viewer"}


def auth_for(directory: Path, config: Config = CONFIG, clock: Optional[object] = None) -> AuthState:
    """An ``AuthState`` over ``directory`` with the accounts ``alice`` (admin) and ``bob`` (viewer), both with the
    password ``PASSWORD``. ``clock`` (a callable) drives the sessions and the throttle, for the tests that move time."""
    state = AuthState.for_directory(directory, config)
    for name, role in USERS.items():
        state.accounts.store.add(name, role, PASSWORD)
    if clock is not None:
        state.sessions = SessionStore(config.session_idle_minutes * 60, config.session_max_hours * 3600, clock=clock)
        state.throttle = LoginThrottle(clock=clock)
    return state


def origin_of(client: TestClient) -> str:
    return str(client.base_url).rstrip("/")


def login(client: TestClient, username: str = "bob", password: str = PASSWORD) -> str:
    """Log ``client`` in (its cookie jar keeps the session) and return the CSRF token."""
    response = client.post("/api/v1/auth/login", json={"username": username, "password": password},
                           headers={"Origin": origin_of(client)})
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]


def logged_in(app, username: str = "bob", **kwargs) -> TestClient:
    """A client that is logged in as ``username`` and sends the Origin and the CSRF token on every request."""
    client = TestClient(app, **kwargs)
    token = login(client, username)
    client.headers.update({"Origin": origin_of(client), "X-CSRF-Token": token})
    return client
