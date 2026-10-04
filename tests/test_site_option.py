"""`--site NAME|REF|UUID`: a global option that beats UNIFI_SITE_ID, like --timeout beats UNIFI_TIMEOUT."""

import pytest

from homelab_probe import cli
from homelab_probe.config import MAX_SITE_LENGTH, ConfigError, validate_site

SECOND_SITE = {"id": "site-2", "internalReference": "lab", "name": "Lab Site"}


def run(fake_client, monkeypatch, *argv, site_id=None):
    monkeypatch.setenv("UNIFI_URL", "https://controller.example")
    monkeypatch.setenv("UNIFI_API_KEY", "key")
    if site_id is not None:
        monkeypatch.setenv("UNIFI_SITE_ID", site_id)
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, c: fake_client))
    return cli.main(list(argv))


def with_second_site(fake_client):
    fake_client.session.fx["sites"].append(dict(SECOND_SITE))


def devices_requests(fake_client):
    return [path for path in fake_client.session.calls if path.endswith("/devices")]


@pytest.mark.parametrize("site", ["Default", "default", "site-1"])
def test_a_site_can_be_chosen_by_name_internal_reference_or_uuid(fake_client, monkeypatch, capsys, site):
    assert run(fake_client, monkeypatch, "--site", site, "query", "devices") == 0
    assert "Gateway" in capsys.readouterr().out and devices_requests(fake_client)[0].endswith("/sites/site-1/devices")


def test_the_option_beats_site_id_in_the_environment(fake_client, monkeypatch, capsys):
    assert run(fake_client, monkeypatch, "query", "devices", site_id="no-such-site") == cli.EXIT_ERROR
    assert "Site 'no-such-site' not found" in capsys.readouterr().err
    assert run(fake_client, monkeypatch, "--site", "Default", "query", "devices", site_id="no-such-site") == 0
    assert "Gateway" in capsys.readouterr().out
    assert run(fake_client, monkeypatch, "query", "devices", site_id="a/b") == cli.EXIT_ERROR
    assert "UNIFI_SITE_ID" in capsys.readouterr().err
    assert run(fake_client, monkeypatch, "--site", "Default", "query", "devices", site_id="a/b") == 0
    assert "Gateway" in capsys.readouterr().out


def test_the_option_selects_the_site_that_is_read(fake_client, monkeypatch, capsys):
    """The fake controller only serves site-1, so asking for the second site fails on its devices."""
    with_second_site(fake_client)
    assert run(fake_client, monkeypatch, "--site", "Lab Site", "query", "devices") == cli.EXIT_ERROR
    assert "404" in capsys.readouterr().err
    assert devices_requests(fake_client)[0].endswith("/sites/site-2/devices")
    fake_client.session.calls.clear()
    assert run(fake_client, monkeypatch, "--site", "lab", "query", "devices") == cli.EXIT_ERROR     # by reference
    assert devices_requests(fake_client)[0].endswith("/sites/site-2/devices")


def test_without_the_option_site_id_and_the_default_still_apply(fake_client, monkeypatch, capsys):
    with_second_site(fake_client)
    assert run(fake_client, monkeypatch, "query", "devices") == 0
    assert devices_requests(fake_client)[0].endswith("/sites/site-1/devices")
    assert run(fake_client, monkeypatch, "query", "devices", site_id="Lab Site") == cli.EXIT_ERROR
    assert devices_requests(fake_client)[-1].endswith("/sites/site-2/devices")


def test_an_unknown_site_lists_the_sites_there_are(fake_client, monkeypatch, capsys):
    with_second_site(fake_client)
    assert run(fake_client, monkeypatch, "--site", "nothing", "query", "devices") == cli.EXIT_ERROR
    err = capsys.readouterr().err
    assert "Site 'nothing' not found" in err and "default" in err and "lab" in err


@pytest.mark.parametrize("site", ["a/b", "a\\b", "a?b", "a#b", "bad\x07name", "x" * (MAX_SITE_LENGTH + 1), "   ", ""])
def test_a_bad_site_is_a_usage_error_that_names_the_option_before_any_request(fake_client, monkeypatch, capsys, site):
    with pytest.raises(SystemExit) as stop:
        run(fake_client, monkeypatch, "--site", site, "query", "devices")
    assert stop.value.code == cli.EXIT_USAGE
    err = capsys.readouterr().err
    assert "--site" in err and "UNIFI_SITE_ID" not in err and fake_client.session.calls == []


def test_the_option_belongs_before_the_command_like_the_other_global_options(fake_client, monkeypatch, capsys):
    with pytest.raises(SystemExit) as stop:
        run(fake_client, monkeypatch, "query", "devices", "--site", "default")
    assert stop.value.code == cli.EXIT_USAGE and "unrecognized arguments: --site" in capsys.readouterr().err


def test_surrounding_space_is_ignored_and_a_name_with_a_space_is_fine(fake_client, monkeypatch, capsys):
    with_second_site(fake_client)
    assert run(fake_client, monkeypatch, "--site", "  Default  ", "query", "devices") == 0
    assert validate_site("My Lab", "--site") == "My Lab" and validate_site("  x  ") == "x"


def test_the_environment_variable_still_reports_itself_by_its_own_name():
    with pytest.raises(ConfigError, match="UNIFI_SITE_ID 'a/b' contains '/'"):
        validate_site("a/b")
    with pytest.raises(ConfigError, match="--site 'a/b' contains '/'"):
        validate_site("a/b", "--site")
    assert validate_site(None) == validate_site("") == "default"


def test_verbose_shows_the_site_that_is_used(fake_client, monkeypatch, capsys):
    assert run(fake_client, monkeypatch, "--verbose", "--site", "Default", "query", "devices", site_id="elsewhere") == 0
    first = next(line for line in capsys.readouterr().err.splitlines() if "settings from" in line)
    assert "site Default" in first and "elsewhere" not in first


@pytest.mark.parametrize("argv", [["diagnose", "--no-events"], ["wan"], ["topology"], ["audit"], ["firewall"],
                                  ["client", "desktop", "--no-events"], ["new-clients"], ["wifi"]])
def test_every_command_that_reads_a_site_uses_the_chosen_one(fake_client, monkeypatch, capsys, argv):
    with_second_site(fake_client)
    run(fake_client, monkeypatch, "--site", "lab", *argv)
    capsys.readouterr()
    assert devices_requests(fake_client) and all(path.endswith("/sites/site-2/devices")
                                                 for path in devices_requests(fake_client))


def test_the_event_command_uses_the_chosen_site_too(fake_client, monkeypatch, capsys):
    with_second_site(fake_client)
    run(fake_client, monkeypatch, "--site", "lab", "events")
    capsys.readouterr()
    assert [path for path, _ in fake_client.session.posts] == ["/proxy/network/v2/api/site/lab/system-log/all"]
