"""What the tool keeps is kept per site (issue #140): snapshots in ``snapshots/<site id>/``, the notification state in
``snapshots/<site id>/notify-state.json``; the files of earlier versions are still read, for the site they belong to."""

import json
import os
from pathlib import Path

import pytest
from test_notify import FakePost, make_env

from homelab_probe import cli, history
from homelab_probe import notify as notify_module
from homelab_probe.client import UniFiClient
from homelab_probe.demo.session import DemoSession
from homelab_probe.documents import site_identity
from homelab_probe.util import site_key


class SiteSession(DemoSession):
    """The synthetic controller, but its only site has another id, internal reference and name."""

    def __init__(self, site_id="site-1", ref="default", name="Default"):
        super().__init__()
        self.site_id, self.ref = site_id, ref
        self.fx["sites"] = [{"id": site_id, "internalReference": ref, "name": name}]

    def get(self, url, params=None, verify=True, timeout=None):
        url = url.replace(f"/sites/{self.site_id}/", "/sites/site-1/")
        url = url.replace(f"/api/s/{self.ref}/", "/api/s/default/")                 # the fixture is of the site "default"
        return super().get(url, params, verify, timeout)


def run(monkeypatch, session, *argv):
    client = UniFiClient("https://controller", "key")
    client.session = session
    session.fx["legacy"]["device"][0]["overheating"] = False
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, c: client))
    monkeypatch.setenv("UNIFI_URL", "https://controller.example")
    monkeypatch.setenv("UNIFI_API_KEY", "sekret-api-key-0123456789")
    site = session.fx["sites"][0]["internalReference"]
    return cli.main(["--site", site, *argv])


A = lambda: SiteSession("site-a", "default", "Home")          # noqa: E731
B = lambda: SiteSession("site-b", "branch", "Branch")         # noqa: E731


def names(directory):
    return sorted(p.name for p in Path(directory).glob("snapshot-*.json"))


# -- the names ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("site_id, key", [
    ("site-1", "site-1"), ("5b6c7d8e-1234-4abc-8def-0123456789ab", "5b6c7d8e-1234-4abc-8def-0123456789ab"),
    ("../../etc", "_.._.._etc"), ("a/b\\c", "a_b_c"), ("", "unknown"), (None, "unknown"),
    (".", "_."), ("..", "_.."), (".hidden", "_.hidden"), ("a b?#", "a_b__"), ("é", "_"), ("x" * 200, "x" * 64),
    ("nul\x00name", "nul_name"),
])
def test_a_site_id_is_made_safe_as_a_file_name(site_id, key):
    assert site_key(site_id) == key
    assert "/" not in key and "\\" not in key and key not in ("", ".", "..")


def test_the_same_id_always_gives_the_same_name_and_different_ids_do_not_collide_by_case():
    assert site_key("Site-A") == site_key("Site-A") != site_key("site-a")


# -- snapshots ---------------------------------------------------------------------------------------------------

def test_a_snapshot_goes_into_the_directory_of_its_site(monkeypatch):
    assert run(monkeypatch, A(), "snapshot") == 0
    assert run(monkeypatch, B(), "snapshot") == 0
    assert len(names("snapshots/site-a")) == 1 and len(names("snapshots/site-b")) == 1
    assert names("snapshots") == []                                       # nothing straight into snapshots/
    assert oct(os.stat("snapshots/site-a").st_mode & 0o777) == "0o700"
    assert json.loads(next(Path("snapshots/site-b").glob("*.json")).read_text())["site"]["id"] == "site-b"


def test_dir_names_the_directory_as_it_always_did(monkeypatch, tmp_path):
    assert run(monkeypatch, A(), "snapshot", "--dir", str(tmp_path / "mine")) == 0
    assert len(names(tmp_path / "mine")) == 1 and not Path("snapshots").exists()


def test_keep_counts_only_the_snapshots_of_this_site(monkeypatch, capsys):
    for _ in range(3):
        run(monkeypatch, A(), "snapshot")
    for _ in range(2):
        run(monkeypatch, B(), "snapshot")
    capsys.readouterr()
    run(monkeypatch, B(), "snapshot", "--keep", "2")
    assert len(names("snapshots/site-b")) == 2 and len(names("snapshots/site-a")) == 3
    run(monkeypatch, A(), "snapshot", "--keep", "2")
    assert len(names("snapshots/site-a")) == 2 and len(names("snapshots/site-b")) == 2


def old_snapshot(site_id, stamp="20200101-000000Z", directory="snapshots"):
    """A snapshot as an earlier version saved it: straight into snapshots/, whatever the site."""
    session = SiteSession(site_id)
    client = UniFiClient("https://controller", "key")
    client.session = session
    from homelab_probe.documents import snapshot_document

    record = snapshot_document(client, site_id).data
    path = Path(directory) / f"snapshot-{stamp}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record))
    return path


def test_snapshots_saved_before_there_was_a_directory_per_site_belong_to_the_site_they_name(monkeypatch):
    mine = old_snapshot("site-a", "20200101-000000Z")
    theirs = old_snapshot("site-b", "20200102-000000Z")
    run(monkeypatch, A(), "snapshot", "--keep", "1")
    assert not mine.exists() and theirs.exists()                          # only this site's old file was counted
    assert len(names("snapshots/site-a")) == 1


def test_diff_without_a_name_picks_the_newest_snapshot_of_this_site(monkeypatch, capsys):
    run(monkeypatch, B(), "snapshot")
    old_snapshot("site-b", "20990101-000000Z")                             # the newest of all, but not ours
    mine = old_snapshot("site-a", "20200101-000000Z")
    capsys.readouterr()
    assert run(monkeypatch, A(), "diff") == 0
    assert mine.name in capsys.readouterr().out


def test_diff_last_two_uses_this_sites_snapshots_new_and_old(monkeypatch, capsys):
    old_snapshot("site-a", "20200101-000000Z")
    run(monkeypatch, A(), "snapshot")
    old_snapshot("site-b", "20990101-000000Z")
    capsys.readouterr()
    assert run(monkeypatch, A(), "diff", "--last-two") == 0
    assert "20200101-000000Z" in capsys.readouterr().out


def test_diff_last_two_needs_two_snapshots_of_this_site(monkeypatch):
    run(monkeypatch, A(), "snapshot")
    old_snapshot("site-b", "20200101-000000Z")
    old_snapshot("site-b", "20200102-000000Z")
    assert run(monkeypatch, A(), "diff", "--last-two") == cli.EXIT_ERROR            # only one of its own


def test_a_name_is_looked_for_in_the_site_directory_then_in_snapshots(monkeypatch, capsys):
    run(monkeypatch, A(), "snapshot")
    inside = names("snapshots/site-a")[0]
    legacy = old_snapshot("site-a", "20200101-000000Z")
    capsys.readouterr()
    assert run(monkeypatch, A(), "diff", inside) == 0
    assert run(monkeypatch, A(), "diff", legacy.name) == 0
    assert run(monkeypatch, A(), "diff", "snapshot-19990101-000000Z.json") == cli.EXIT_ERROR
    assert "also looked in snapshots/site-a/, snapshots/" in capsys.readouterr().err


def test_a_site_that_cannot_be_found_stops_diff_with_the_way_out(monkeypatch, capsys):
    session = SiteSession()
    session.status = 503
    assert run(monkeypatch, session, "diff") == cli.EXIT_ERROR
    assert "give --dir DIR to work without it" in capsys.readouterr().err


def test_with_dir_diff_needs_no_controller_for_the_saved_snapshots(monkeypatch, tmp_path, capsys):
    run(monkeypatch, A(), "snapshot", "--dir", str(tmp_path))
    run(monkeypatch, A(), "snapshot", "--dir", str(tmp_path))
    session = SiteSession()
    capsys.readouterr()
    assert run(monkeypatch, session, "diff", "--dir", str(tmp_path), "--last-two") == 0
    assert session.calls == []


def test_two_snapshot_files_are_compared_without_asking_which_site(monkeypatch, tmp_path):
    first = old_snapshot("site-a", "20200101-000000Z", tmp_path)
    second = old_snapshot("site-a", "20200102-000000Z", tmp_path)
    session = SiteSession()
    assert run(monkeypatch, session, "diff", str(first), str(second)) == 0
    assert session.calls == []


def test_one_file_against_the_live_network_does_not_ask_which_site_either(monkeypatch, tmp_path):
    first = old_snapshot("site-a", "20200101-000000Z", tmp_path)
    session = A()
    assert run(monkeypatch, session, "diff", str(first)) == 0


def test_listing_skips_files_that_say_nothing_usable_about_their_site(tmp_path):
    base = tmp_path / "snapshots"
    base.mkdir()
    (base / "snapshot-20200101-000000Z.json").write_text("not json")
    (base / "snapshot-20200102-000000Z.json").write_text(json.dumps({"site": {"id": 5}}))
    (base / "snapshot-20200103-000000Z.json").write_text(json.dumps({"site": "x"}))
    assert history.recorded_site_id(base / "snapshot-20200101-000000Z.json") is None
    assert history.recorded_site_id(base / "snapshot-20200102-000000Z.json") is None
    assert history.recorded_site_id(base / "snapshot-20200103-000000Z.json") is None
    assert history.site_snapshots(base, {"id": "x"}) == []


def test_the_site_identity_names_id_name_and_reference():
    assert site_identity({"id": "i", "name": "n", "internalReference": "r"}) == {"id": "i", "name": "n", "ref": "r"}
    assert site_identity({}) == {"id": "", "name": "", "ref": ""}


# -- the notification state -------------------------------------------------------------------------------------

@pytest.fixture
def post(monkeypatch):
    fake = FakePost(200)
    monkeypatch.setattr(notify_module.requests, "post", fake)
    make_env(monkeypatch)
    return fake


def diagnose(monkeypatch, session, *extra):
    return run(monkeypatch, session, "diagnose", "--no-events", "--notify", *extra)


def test_each_site_remembers_its_own_findings_and_says_which_site_it_is(monkeypatch, post):
    diagnose(monkeypatch, A())
    diagnose(monkeypatch, B())
    for key in ("site-a", "site-b"):
        state = json.loads(Path(f"snapshots/{key}/notify-state.json").read_text())
        assert state["site"] == key and state["active"]
    assert not Path("snapshots/notify-state.json").exists()
    assert len(post.calls) == 2                                           # both were new for their own site


def test_alternating_sites_make_no_false_recoveries(monkeypatch, post):
    diagnose(monkeypatch, A())                                            # site A reports its warnings
    diagnose(monkeypatch, B(), "--notify-min", "critical")                # site B sees fewer: not A's business
    bodies = " ".join(str(call[1].get("data")) for call in post.calls)
    assert "RECOVERED" not in bodies
    again = len(post.calls)
    diagnose(monkeypatch, A())                                            # A's memory was not touched by B
    assert len(post.calls) == again


def test_a_state_of_another_site_is_refused(monkeypatch, post, tmp_path, capsys):
    state = tmp_path / "state.json"
    assert diagnose(monkeypatch, A(), "--notify-state", str(state)) in (0, 1, 2)
    capsys.readouterr()
    assert diagnose(monkeypatch, B(), "--notify-state", str(state)) == cli.EXIT_ERROR
    assert "remembers the findings of another site" in capsys.readouterr().err
    assert json.loads(state.read_text())["site"] == "site-a"             # untouched


def test_a_state_that_names_no_site_is_accepted_and_stamped(monkeypatch, post, tmp_path):
    state = tmp_path / "state.json"
    diagnose(monkeypatch, A(), "--notify-baseline", "--notify-state", str(state))
    document = json.loads(state.read_text())
    del document["site"]
    state.write_text(json.dumps(document))
    diagnose(monkeypatch, B(), "--notify-state", str(state))
    assert json.loads(state.read_text())["site"] == "site-b"


def legacy_state(monkeypatch, tmp_path):
    """The shared state file of earlier versions: findings remembered, no site recorded."""
    path = Path("snapshots/notify-state.json")
    diagnose(monkeypatch, A(), "--notify-baseline", "--notify-state", str(path))
    document = json.loads(path.read_text())
    del document["site"]
    path.write_text(json.dumps(document))
    return path


def test_the_shared_state_of_earlier_versions_is_read_for_the_default_site_and_left_alone(monkeypatch, post, tmp_path):
    legacy = legacy_state(monkeypatch, tmp_path)
    before = legacy.read_text()
    diagnose(monkeypatch, A())
    assert post.calls == []                                               # everything was already reported
    assert legacy.read_text() == before


def test_the_next_save_writes_the_sites_own_file_and_that_one_is_used_from_then_on(monkeypatch, post, tmp_path):
    legacy_state(monkeypatch, tmp_path)
    diagnose(monkeypatch, A(), "--notify-min", "critical")                # a change: a finding is no longer reported
    own = Path("snapshots/site-a/notify-state.json")
    assert json.loads(own.read_text())["site"] == "site-a"
    Path("snapshots/notify-state.json").write_text("garbage")             # the legacy file is not looked at any more
    diagnose(monkeypatch, A(), "--notify-min", "critical")
    assert json.loads(own.read_text())["site"] == "site-a"


def test_the_shared_state_is_never_paired_with_another_site(monkeypatch, post, tmp_path):
    legacy_state(monkeypatch, tmp_path)
    diagnose(monkeypatch, B())                                            # reference "branch": not the default site
    assert len(post.calls) == 1                                           # all new for it: its own, empty memory


def test_an_explicit_state_file_wins_over_the_legacy_one(monkeypatch, post, tmp_path):
    legacy_state(monkeypatch, tmp_path)
    diagnose(monkeypatch, A(), "--notify-state", str(tmp_path / "mine.json"))
    assert len(post.calls) == 1                                           # mine.json was empty


def test_the_diagnose_document_says_which_site_was_read():
    from homelab_probe.documents import diagnose_document

    client = UniFiClient("https://controller", "key")
    client.session = B()
    client.session.fx["legacy"]["device"][0]["overheating"] = False
    document = diagnose_document(client, "branch", echo=False)
    assert document.meta["site"] == {"id": "site-b", "name": "Branch", "ref": "branch"}
    assert "site-b" not in document.to_json()                              # the JSON output itself is unchanged
