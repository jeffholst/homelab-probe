"""`serve` without the web extra says what to install (issue #183). This module needs no FastAPI, so it also runs in
the CI job that installs the command line only."""

import builtins
import sys

import pytest

from homelab_probe import cli


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("UNIFI_URL", "https://controller.example")
    monkeypatch.setenv("UNIFI_API_KEY", "the-api-key-0123456789")


def test_without_the_extra_it_says_what_to_install_and_exits_3(configured, monkeypatch, capsys):
    real = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name.startswith("fastapi") or name.startswith("homelab_probe.server") or name == "uvicorn":
            raise ModuleNotFoundError(f"No module named {name!r}", name=name.split(".")[0])
        return real(name, *args, **kwargs)

    for name in [m for m in sys.modules if m.startswith("homelab_probe.server")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(builtins, "__import__", blocked)
    assert cli.main(["serve"]) == 3
    err = capsys.readouterr().err
    assert "uv run --extra web hlp.py serve" in err
    assert "python -m pip install 'homelab-probe[web]'" in err and "Traceback" not in err
