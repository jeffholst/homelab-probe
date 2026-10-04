import pytest

from homelab_probe import client as client_module
from homelab_probe.client import UniFiAPIError


def test_info_and_site_resolution(fake_client):
    assert fake_client.info()["applicationVersion"] == "10.0.0"
    for ref in ("default", "Default", "site-1"):
        assert fake_client.resolve_site(ref)["id"] == "site-1"


def test_unknown_site_lists_available(fake_client):
    with pytest.raises(UniFiAPIError, match="Available"):
        fake_client.resolve_site("nope")


def test_pagination_collects_every_page(fake_client, monkeypatch):
    monkeypatch.setattr(client_module, "PAGE_SIZE", 1)
    assert len(fake_client.devices("site-1")) == 4
    devices_calls = [c for c in fake_client.session.calls if c.endswith("/devices")]
    assert len(devices_calls) == 4


def test_legacy_stat(fake_client):
    assert len(fake_client.legacy_stat("default", "sta")) == 2


def test_unauthorized_and_http_errors(fake_client):
    fake_client.session.status = 401
    with pytest.raises(UniFiAPIError, match="401"):
        fake_client.info()
    fake_client.session.status = 500
    with pytest.raises(UniFiAPIError, match="HTTP 500"):
        fake_client.info()
