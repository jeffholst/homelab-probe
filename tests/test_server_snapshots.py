"""Saved snapshots over the API (issue #186): list, save and diff under /api/v1/unifi/sites/{site}."""

import json
import os
import stat

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("tomlkit")

from fastapi.testclient import TestClient  # noqa: E402
from server_support import CONFIG, auth_for, logged_in  # noqa: E402

from homelab_probe import history  # noqa: E402
from homelab_probe.config import ConfigError  # noqa: E402
from homelab_probe.demo.session import DemoSession  # noqa: E402
from homelab_probe.server import snapshots_api  # noqa: E402
from homelab_probe.server.app import create_app  # noqa: E402
from homelab_probe.server.service import ControllerService  # noqa: E402

SITE = "/api/v1/unifi/sites/default"
LIST = f"{SITE}/snapshots"
DIFF = f"{SITE}/diff"


def make_app(tmp_path, **kwargs):
    session = DemoSession()
    app = create_app(CONFIG, state_dir=tmp_path, hosts=["testserver"], auth=auth_for(tmp_path),
                     service=ControllerService(CONFIG, session=session, ttl=0), **kwargs)
    app.state.fake = session
    return app


@pytest.fixture
def app(tmp_path):
    return make_app(tmp_path)


@pytest.fixture
def admin(app):
    return logged_in(app, "alice")


@pytest.fixture
def viewer(app):
    return logged_in(app, "bob")


def audit_lines(tmp_path):
    return [json.loads(line) for line in (tmp_path / "audit.log").read_text().splitlines()]


def save(client, **body):
    return client.post(LIST, json=body)


def old_file(tmp_path, stamp, site_id="site-1", directory=None):
    """A snapshot as an earlier version saved it (straight into snapshots/), or one placed in ``directory``."""
    base = directory or (tmp_path / "snapshots")
    base.mkdir(parents=True, exist_ok=True)
    session = DemoSession()
    from homelab_probe.client import UniFiClient
    from homelab_probe.documents import snapshot_document

    client = UniFiClient("https://controller", "key")
    client.session = session
    record = snapshot_document(client, "default", echo=False).data
    record["site"]["id"] = site_id
    path = base / f"snapshot-{stamp}.json"
    path.write_text(json.dumps(record))
    return path


# -- listing ----------------------------------------------------------------------------------------------------

def test_nothing_saved_is_an_empty_list(viewer):
    assert viewer.get(LIST).json() == {"site": {"id": "site-1", "name": "Default"}, "total": 0, "items": []}


def test_the_list_is_newest_first_with_what_each_holds(admin, tmp_path):
    for _ in range(3):
        assert save(admin).status_code == 201
    body = admin.get(LIST).json()
    assert body["total"] == 3 and len(body["items"]) == 3
    on_disk = history.list_snapshots(tmp_path / "snapshots" / "site-1")
    assert [item["name"] for item in body["items"]] == [p.name for p in reversed(on_disk)]
    first = body["items"][0]
    assert first["readable"] is True and first["devices"] > 0 and first["clients"] > 0
    assert first["captured_at"].startswith("20")
    assert admin.get(LIST, params={"limit": 2}).json()["items"] == body["items"][:2]
    assert admin.get(LIST, params={"limit": 2}).json()["total"] == 3


@pytest.mark.parametrize("limit", [0, 201, "x"])
def test_the_limit_is_bounded(viewer, limit):
    assert viewer.get(LIST, params={"limit": limit}).status_code == 422


def test_older_snapshots_saved_straight_into_snapshots_are_listed_for_their_own_site_only(admin, tmp_path):
    mine = old_file(tmp_path, "20200101-000000Z")
    old_file(tmp_path, "20200102-000000Z", site_id="another-site")
    body = admin.get(LIST).json()
    assert [item["name"] for item in body["items"]] == [mine.name]


def test_a_file_that_is_not_a_snapshot_is_listed_as_unreadable(admin, tmp_path):
    directory = tmp_path / "snapshots" / "site-1"
    directory.mkdir(parents=True)
    (directory / "snapshot-20200101-000000Z.json").write_text("not json")
    item = admin.get(LIST).json()["items"][0]
    assert item == {"name": "snapshot-20200101-000000Z.json", "readable": False}


def test_listing_needs_a_login_and_a_known_site(app, viewer):
    assert TestClient(app).get(LIST).status_code == 401
    assert viewer.get("/api/v1/unifi/sites/nope/snapshots").status_code == 404
    assert viewer.get("/api/v1/unifi/sites/a%2Fb/snapshots").status_code in (404, 422)


def test_a_directory_that_cannot_be_read_is_a_500_with_a_fixed_message(viewer, tmp_path, monkeypatch):
    def broken(base, site):
        raise ConfigError(f"cannot read snapshot directory {base}: Permission denied")

    monkeypatch.setattr(snapshots_api, "site_snapshots", broken)
    response = viewer.get(LIST)
    assert response.status_code == 500 and response.json()["error"] == "snapshots_unreadable"
    assert str(tmp_path) not in response.text and "Permission" not in response.text


# -- saving -----------------------------------------------------------------------------------------------------

def test_a_snapshot_is_saved_in_the_directory_of_its_site_owner_only(admin, tmp_path):
    response = save(admin)
    assert response.status_code == 201
    body = response.json()
    summary = body["snapshot"]
    path = tmp_path / "snapshots" / "site-1" / summary["name"]
    assert path.is_file() and stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    record = history.load_snapshot(path)
    assert record["site"]["id"] == "site-1" and len(record["devices"]) == summary["devices"]
    assert body["removed"] == [] and body["generated_at"].endswith("Z") and isinstance(body["warnings"], list)
    assert str(tmp_path) not in response.text


def test_the_file_is_what_the_command_would_save(admin, tmp_path):
    from homelab_probe.client import UniFiClient
    from homelab_probe.documents import snapshot_document

    saved = history.load_snapshot(tmp_path / "snapshots" / "site-1" / save(admin).json()["snapshot"]["name"])
    client = UniFiClient("https://controller", "key")
    client.session = DemoSession()
    expected = snapshot_document(client, "default", echo=False).data
    for key in ("schema_version", "site", "controller", "devices", "clients", "reservations"):
        assert saved[key] == expected[key], key


def test_two_saves_in_one_second_make_two_files(admin, tmp_path):
    save(admin)
    save(admin)
    assert len(list((tmp_path / "snapshots" / "site-1").glob("snapshot-*.json"))) == 2


def test_keep_prunes_the_oldest_of_this_site_and_nothing_else(admin, tmp_path):
    other = old_file(tmp_path, "20100101-000000Z", site_id="another-site")
    older = old_file(tmp_path, "20100102-000000Z")                          # an old one of this site, in snapshots/
    for _ in range(2):
        save(admin)
    body = save(admin, keep=2).json()
    assert older.name in body["removed"] and not older.exists() and other.exists()
    assert admin.get(LIST).json()["total"] == 2


def test_keep_larger_than_what_exists_removes_nothing(admin):
    save(admin)
    assert save(admin, keep=10).json()["removed"] == []


@pytest.mark.parametrize("body", [{"keep": 0}, {"keep": 10001}, {"keep": "x"}, {"keep": 1.5}, {"nonsense": 1}])
def test_a_bad_body_is_a_422_and_writes_nothing(admin, tmp_path, body):
    assert save(admin, **body).status_code == 422
    assert not (tmp_path / "snapshots").exists()


def test_only_an_administrator_with_the_csrf_token_may_save(app, tmp_path, viewer):
    assert TestClient(app).post(LIST, json={}, headers={"Origin": "http://testserver"}).status_code == 401
    assert save(viewer).status_code == 403
    plain = TestClient(app)
    assert plain.post(LIST, json={}).status_code == 403
    no_token = logged_in(app, "alice")
    del no_token.headers["X-CSRF-Token"]
    assert save(no_token).json()["error"] == "csrf_token"
    assert not (tmp_path / "snapshots").exists()


def test_a_read_only_server_does_not_save(tmp_path):
    admin = logged_in(make_app(tmp_path, read_only=True), "alice")
    response = save(admin)
    assert response.status_code == 403 and response.json()["error"] == "read_only"
    assert not (tmp_path / "snapshots").exists()
    assert admin.get(LIST).status_code == 200                                # reading still works


def test_a_controller_that_cannot_be_read_saves_nothing(app, admin, tmp_path):
    app.state.fake.status = 503
    response = save(admin)
    assert response.status_code in (502, 504) and not (tmp_path / "snapshots").exists()


def test_a_failed_write_is_a_500_with_a_fixed_message(admin, tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(snapshots_api, "save_snapshot", broken)
    response = save(admin)
    assert response.status_code == 500 and response.json()["error"] == "snapshot_not_written"
    assert "No space" not in response.text and str(tmp_path) not in response.text


def test_a_save_is_audited_with_the_name_and_the_counts(admin, tmp_path):
    name = save(admin, keep=5).json()["snapshot"]["name"]
    entry = audit_lines(tmp_path)[-1]
    assert entry["event"] == "snapshot.saved" and entry["actor"] == "alice" and entry["name"] == name
    assert entry["devices"] > 0 and entry["clients"] > 0 and entry["removed"] == 0


# -- comparing --------------------------------------------------------------------------------------------------

def rename_a_device(app):
    app.state.fake.fx["devices"][0]["name"] = "Renamed Device"
    app.state.fake.fx["legacy"]["device"][0]["name"] = "Renamed Device"


def test_the_newest_snapshot_is_compared_with_the_network_now(app, admin):
    name = save(admin).json()["snapshot"]["name"]
    unchanged = admin.get(DIFF).json()
    assert unchanged["total"] == 0 and unchanged["old"] == name and unchanged["new"] is None
    assert unchanged["generated_at"].endswith("Z") and unchanged["version"] == 1
    rename_a_device(app)
    changed = admin.get(DIFF).json()
    assert changed["total"] >= 1 and any("Renamed Device" in json.dumps(d) for d in changed["devices"].values())


def test_two_named_snapshots_are_compared_without_the_network(app, admin):
    first = save(admin).json()["snapshot"]["name"]
    rename_a_device(app)
    second = save(admin).json()["snapshot"]["name"]
    app.state.fake.calls.clear()
    body = admin.get(DIFF, params={"old": first, "new": second}).json()
    assert body["old"] == first and body["new"] == second and body["total"] >= 1
    assert not [call for call in app.state.fake.calls if "/devices" in call or "/stat/" in call]


def test_an_older_snapshot_of_this_site_can_be_named(admin, tmp_path):
    legacy = old_file(tmp_path, "20200101-000000Z")
    assert admin.get(DIFF, params={"old": legacy.name}).json()["old"] == legacy.name


@pytest.mark.parametrize("name", [
    "../snapshot-20200101-000000Z.json", "/etc/passwd", "snapshot-20200101-000000Z.json/../x", "x.json",
    "snapshot-1.json", "..\\snapshot-20200101-000000Z.json", "snapshot-20200101-000000Z.json\x00", "",
])
def test_a_name_that_is_not_a_bare_snapshot_name_never_reaches_the_disk(admin, tmp_path, monkeypatch, name):
    save(admin)
    opened = []
    real = history.load_snapshot
    monkeypatch.setattr(snapshots_api, "load_snapshot", lambda path: opened.append(path) or real(path))
    response = admin.get(DIFF, params={"old": name})
    if name:
        assert response.status_code == 404 and response.json()["error"] == "snapshot_not_found"
    assert all(str(path).startswith(str(tmp_path / "snapshots")) for path in opened)


def test_a_snapshot_of_another_site_cannot_be_named(admin, tmp_path):
    other = old_file(tmp_path, "20200101-000000Z", site_id="another-site")
    save(admin)
    assert admin.get(DIFF, params={"old": other.name}).status_code == 404
    assert admin.get(DIFF, params={"new": other.name}).status_code == 404


def test_without_any_snapshot_there_is_nothing_to_compare(admin):
    response = admin.get(DIFF)
    assert response.status_code == 404 and response.json()["error"] == "snapshot_not_found"


def test_a_damaged_snapshot_is_a_500_with_a_fixed_message(admin, tmp_path):
    directory = tmp_path / "snapshots" / "site-1"
    directory.mkdir(parents=True)
    (directory / "snapshot-20200101-000000Z.json").write_text("{not json")
    response = admin.get(DIFF)
    assert response.status_code == 500 and response.json()["error"] == "snapshots_unreadable"
    assert str(tmp_path) not in response.text


def test_the_comparison_needs_a_login(app):
    assert TestClient(app).get(DIFF).status_code == 401


def test_the_comparison_works_for_a_viewer_and_in_a_read_only_server(tmp_path):
    old_file(tmp_path, "20200101-000000Z")
    old_file(tmp_path, "20200102-000000Z")
    viewer = logged_in(make_app(tmp_path, read_only=True), "bob")
    assert viewer.get(DIFF).status_code == 200
    assert viewer.get(DIFF, params={"old": "snapshot-20200101-000000Z.json",
                                    "new": "snapshot-20200102-000000Z.json"}).json()["total"] == 0


def test_a_controller_that_cannot_be_read_stops_every_snapshot_route_because_the_site_is_resolved_first(tmp_path):
    old_file(tmp_path, "20200101-000000Z")
    old_file(tmp_path, "20200102-000000Z")
    app = make_app(tmp_path)
    app.state.fake.status = 503
    viewer = logged_in(app, "bob")
    assert viewer.get(LIST).status_code in (502, 504)
    assert viewer.get(DIFF).status_code in (502, 504)
    assert viewer.get(f"{SITE}/diff", params={"old": "snapshot-20200101-000000Z.json",
                                              "new": "snapshot-20200102-000000Z.json"}).status_code in (502, 504)


def test_a_snapshot_is_of_now_not_of_what_the_cache_still_holds(tmp_path):
    session = DemoSession()
    app = create_app(CONFIG, state_dir=tmp_path, hosts=["testserver"], auth=auth_for(tmp_path),
                     service=ControllerService(CONFIG, session=session))            # the default 30-second cache
    admin = logged_in(app, "alice")
    assert admin.get(f"{SITE}/devices").status_code == 200                           # fills the cache
    session.fx["devices"][0]["name"] = "Renamed Device"
    session.fx["legacy"]["device"][0]["name"] = "Renamed Device"
    name = admin.post(LIST, json={}).json()["snapshot"]["name"]
    record = history.load_snapshot(tmp_path / "snapshots" / "site-1" / name)
    assert "Renamed Device" in [d["name"] for d in record["devices"]]
