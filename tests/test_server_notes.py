"""Notes over the API (issue #225)."""

import json
import os

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("tomlkit")

from fastapi.testclient import TestClient  # noqa: E402
from server_support import CONFIG, auth_for, logged_in  # noqa: E402

from homelab_probe.demo.session import DemoSession  # noqa: E402
from homelab_probe.server import notes_api  # noqa: E402
from homelab_probe.server.app import create_app  # noqa: E402
from homelab_probe.server.auth import AuthState  # noqa: E402
from homelab_probe.server.service import ControllerService  # noqa: E402

SITE = "/api/v1/unifi/sites/default"
NOTES = f"{SITE}/notes"
FILE = "snapshots/site-1/notes.json"
MAC = "aa:bb:cc:00:00:01"
SUBJECT = f"device:{MAC}"


def make_app(tmp_path, **kwargs):
    accounts = AuthState.for_directory(tmp_path, CONFIG) if (tmp_path / "users.json").exists() else auth_for(tmp_path)
    return create_app(CONFIG, state_dir=tmp_path, hosts=["testserver"], auth=accounts,
                      service=ControllerService(CONFIG, session=DemoSession(), ttl=0), **kwargs)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(notes_api, "CLOCK", lambda: 1_900_000_000.0)


@pytest.fixture
def app(tmp_path):
    return make_app(tmp_path)


@pytest.fixture
def admin(app):
    return logged_in(app, "alice")


@pytest.fixture
def viewer(app):
    return logged_in(app, "bob")


def add(client, text="moved to the shelf", subject=SUBJECT):
    return client.post(NOTES, json={"subject": subject, "text": text})


def audit(tmp_path):
    return [json.loads(line) for line in (tmp_path / "audit.log").read_text().splitlines()]


def test_an_administrator_adds_edits_lists_and_deletes_a_note(admin, viewer):
    created = add(admin)
    assert created.status_code == 201
    note = created.json()
    assert note["subject"] == "device:AA:BB:CC:00:00:01" and note["author"] == "alice" and note["created_at"].endswith("Z")
    edited = admin.patch(f"{NOTES}/{note['id']}", json={"text": "back in the rack", "revision": note["revision"]})
    assert edited.status_code == 200 and edited.json()["text"] == "back in the rack" and edited.json()["author"] == "alice"
    body = viewer.get(NOTES).json()                                   # a viewer reads
    assert body["total"] == 1 and body["items"][0]["text"] == "back in the rack"
    assert viewer.get(NOTES, params={"subject": "device:AABB.CC00.0001"}).json()["total"] == 1
    assert viewer.get(NOTES, params={"subject": "client:" + MAC}).json()["total"] == 0
    gone = admin.delete(f"{NOTES}/{note['id']}")
    assert gone.status_code == 200 and gone.json()["id"] == note["id"] and admin.get(NOTES).json()["total"] == 0


def test_the_author_is_the_login_not_something_the_request_says(admin):
    response = admin.post(NOTES, json={"subject": SUBJECT, "text": "x", "author": "bob"})
    assert response.status_code == 422
    assert add(admin).json()["author"] == "alice"


def test_a_viewer_and_a_visitor_change_nothing(app, viewer, admin, tmp_path):
    ident = add(admin).json()["id"]
    before = (tmp_path / FILE).read_text()
    assert add(viewer).status_code == 403
    assert viewer.patch(f"{NOTES}/{ident}", json={"text": "y", "revision": "0" * 16}).status_code == 403
    assert viewer.delete(f"{NOTES}/{ident}").status_code == 403
    anonymous = TestClient(app)
    headers = {"Origin": "http://testserver"}
    assert anonymous.get(NOTES).status_code == 401
    assert anonymous.post(NOTES, json={"subject": SUBJECT, "text": "x"}, headers=headers).status_code == 401
    assert (tmp_path / FILE).read_text() == before


def test_a_change_needs_the_csrf_token_and_an_origin(app, admin, tmp_path):
    ident = add(admin).json()["id"]
    no_token = logged_in(app, "alice")
    del no_token.headers["X-CSRF-Token"]
    for call in (lambda: add(no_token), lambda: no_token.patch(f"{NOTES}/{ident}", json={"text": "y", "revision": "0" * 16}),
                 lambda: no_token.delete(f"{NOTES}/{ident}")):
        assert call().json()["error"] == "csrf_token"
    foreign = logged_in(app, "alice")
    foreign.headers["Origin"] = "http://evil.example"
    assert add(foreign).status_code == 403
    assert admin.get(NOTES).json()["total"] == 1


def test_a_read_only_server_refuses_every_change_and_still_lists(tmp_path):
    seeded = logged_in(make_app(tmp_path), "alice")
    ident = add(seeded).json()["id"]
    admin = logged_in(make_app(tmp_path, read_only=True), "alice")
    for response in (add(admin), admin.patch(f"{NOTES}/{ident}", json={"text": "y", "revision": "0" * 16}), admin.delete(f"{NOTES}/{ident}")):
        assert response.status_code == 403 and response.json()["error"] == "read_only"
    assert admin.get(NOTES).json()["total"] == 1


def test_a_note_is_audited_by_id_and_kind_never_by_its_text_or_the_mac(admin, tmp_path):
    created = add(admin, "the secret reason").json()
    ident = created["id"]
    admin.patch(f"{NOTES}/{ident}", json={"text": "another secret", "revision": created["revision"]})
    admin.delete(f"{NOTES}/{ident}")
    entries = audit(tmp_path)[-3:]
    assert [e["event"] for e in entries] == ["note.added", "note.edited", "note.deleted"]
    assert all(e["actor"] == "alice" and e["note"] == ident and e["subject_kind"] == "device" for e in entries)
    text = (tmp_path / "audit.log").read_text()
    assert "secret" not in text and MAC not in text.lower() and "AA:BB" not in text


def test_a_note_lives_in_the_site_directory_owner_only_and_survives_a_restart(admin, tmp_path):
    add(admin)
    assert oct(os.stat(tmp_path / FILE).st_mode & 0o777) == "0o600"
    assert json.loads((tmp_path / FILE).read_text())["site"] == "site-1"
    restarted = logged_in(make_app(tmp_path), "bob")
    assert restarted.get(NOTES).json()["items"][0]["text"] == "moved to the shelf"


@pytest.mark.parametrize("body", [
    {"subject": "Garage AP", "text": "x"}, {"subject": "device:", "text": "x"}, {"subject": SUBJECT, "text": ""},
    {"subject": SUBJECT, "text": "  \x1b "}, {"subject": SUBJECT, "text": "x" * 4001}, {"subject": SUBJECT},
    {"text": "x"}, {"subject": SUBJECT, "text": 5}, {"subject": "x" * 65, "text": "x"}, {"subject": SUBJECT, "text": "x" * 5000}])
def test_a_bad_note_is_refused_and_writes_nothing(admin, tmp_path, body):
    response = admin.post(NOTES, json=body)
    assert response.status_code == 422
    assert not (tmp_path / FILE).exists()


def test_the_text_is_cleaned_and_html_is_just_text(admin):
    note = add(admin, "<b>bold</b>\x1b[0m\r\nsecond line\u202e").json()
    assert note["text"] == "<b>bold</b>[0m\nsecond line"


@pytest.mark.parametrize("ident", ["x", "0123", "../../x", "0123456789ABCDEF", "g" * 16])
def test_a_note_id_is_sixteen_hex_digits(admin, ident):
    assert admin.patch(f"{NOTES}/{ident}", json={"text": "y", "revision": "0" * 16}).status_code in (404, 422)
    assert admin.delete(f"{NOTES}/{ident}").status_code in (404, 422)


def test_an_unknown_note_and_an_unknown_site_are_404(admin):
    assert admin.patch(f"{NOTES}/0123456789abcdef", json={"text": "y", "revision": "0" * 16}
                       ).json()["error"] == "notes_not_found"
    assert admin.delete(f"{NOTES}/0123456789abcdef").status_code == 404
    assert admin.get("/api/v1/unifi/sites/nope/notes").status_code == 404
    assert admin.post("/api/v1/unifi/sites/nope/notes", json={"subject": SUBJECT, "text": "x"}).status_code == 404


def test_a_bad_subject_filter_is_422(viewer):
    assert viewer.get(NOTES, params={"subject": "Garage AP"}).status_code == 422
    assert viewer.get(NOTES, params={"subject": "x" * 65}).status_code == 422


def test_too_many_notes_is_a_409_with_a_fixed_message(admin, monkeypatch):
    from homelab_probe import notes

    monkeypatch.setattr(notes, "MAX_PER_SUBJECT", 1)
    assert add(admin).status_code == 201
    response = add(admin)
    assert response.status_code == 409 and response.json()["error"] == "notes_full"


def test_a_damaged_file_is_a_fixed_500_that_does_not_name_the_path(admin, tmp_path):
    add(admin)
    (tmp_path / FILE).write_text("not json")
    for response in (admin.get(NOTES), add(admin)):
        assert response.status_code == 500 and response.json()["error"] == "notes_unreadable"
        assert str(tmp_path) not in response.text


def test_a_symbolic_link_in_the_place_of_the_site_directory_is_refused(admin, tmp_path):
    (tmp_path / "snapshots").mkdir()
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "snapshots" / "site-1").symlink_to(tmp_path / "elsewhere")
    assert add(admin).status_code in (500, 503) and list((tmp_path / "elsewhere").iterdir()) == []


def test_a_file_of_another_site_is_not_shown(admin, tmp_path):
    add(admin)
    path = tmp_path / FILE
    document = json.loads(path.read_text())
    document["site"] = "site-2"
    path.write_text(json.dumps(document))
    assert admin.get(NOTES).status_code == 409 or admin.get(NOTES).status_code == 500


def test_the_findings_list_counts_the_notes_of_a_finding(admin, viewer):
    top = viewer.get(f"{SITE}/findings").json()["items"][0]
    assert top["note_count"] == 0
    assert add(admin, subject=f"finding:{top['id']}").status_code == 201
    assert add(admin, subject=f"finding:{top['id']}").status_code == 201
    again = viewer.get(f"{SITE}/findings").json()["items"]
    assert next(i for i in again if i["id"] == top["id"])["note_count"] == 2
    assert all(i["note_count"] == 0 for i in again if i["id"] != top["id"])


def test_a_finding_note_stays_with_the_finding_when_it_is_triaged(admin, viewer):
    top = viewer.get(f"{SITE}/findings").json()["items"][0]
    add(admin, subject=f"finding:{top['id']}")
    admin.put(f"{SITE}/findings/{top['id']}/triage", json={"state": "acknowledged"})
    item = next(i for i in viewer.get(f"{SITE}/findings").json()["items"] if i["id"] == top["id"])
    assert item["note_count"] == 1 and item["triage"]["state"] == "acknowledged"


# -- review: conflicts, retained subjects, a controller that cannot be read ----------------------------------------------

def test_a_stale_edit_is_a_409_conflict_and_nobody_loses_a_saved_change(admin, app):
    note = add(admin).json()
    second = logged_in(app, "alice")
    assert admin.patch(f"{NOTES}/{note['id']}", json={"text": "first change", "revision": note["revision"]}
                       ).status_code == 200
    late = second.patch(f"{NOTES}/{note['id']}", json={"text": "second change", "revision": note["revision"]})
    assert late.status_code == 409 and late.json()["error"] == "notes_conflict"
    current = admin.get(NOTES).json()["items"][0]
    assert current["text"] == "first change"
    ok = second.patch(f"{NOTES}/{note['id']}", json={"text": "second change", "revision": current["revision"]})
    assert ok.status_code == 200 and ok.json()["text"] == "second change"


@pytest.mark.parametrize("body", [{"text": "x"}, {"text": "x", "revision": "short"}, {"text": "x", "revision": 5},
                                  {"text": "x", "revision": "G" * 16}])
def test_an_edit_must_say_which_revision_it_read(admin, body):
    note = add(admin).json()
    assert admin.patch(f"{NOTES}/{note['id']}", json=body).status_code == 422


def test_the_documented_maximum_is_accepted_and_the_schema_says_it(admin, viewer):
    assert add(admin, "x" * 4000).status_code == 201 and add(admin, "x" * 4001).status_code == 422
    schemas = viewer.get("/api/v1/openapi.json").json()["components"]["schemas"]
    assert schemas["NewNote"]["properties"]["text"]["maxLength"] == 4000 == schemas["EditedNote"]["properties"]["text"][
        "maxLength"]


def test_the_retained_subjects_are_listed_with_their_last_known_name_and_can_be_searched(admin, viewer):
    admin.post(NOTES, json={"subject": SUBJECT, "text": "moved", "name": "Garage AP"})
    admin.post(NOTES, json={"subject": "client:11:22:33:44:55:66", "text": "left the network"})
    found = viewer.get(f"{NOTES}/subjects").json()
    assert found["total"] == 2
    by = {s["subject"]: s for s in found["items"]}
    device = by["device:AA:BB:CC:00:00:01"]
    assert (device["kind"], device["note_count"], device["last_known"]["name"]) == ("device", 1, "Garage AP")
    assert device["last_known"]["recorded_at"].endswith("Z") and device["last_note_at"].endswith("Z")
    assert by["client:11:22:33:44:55:66"]["last_known"] is None
    assert viewer.get(f"{NOTES}/subjects", params={"q": "garage"}).json()["total"] == 1
    assert viewer.get(f"{NOTES}/subjects", params={"q": "x" * 65}).status_code == 422
    assert TestClient(admin.app).get(f"{NOTES}/subjects").status_code == 401
    assert admin.get(f"{NOTES}/subjects").json()["items"][0]["subject"] in by          # shown to both roles


def test_a_name_can_be_refreshed_by_an_edit_and_is_cleaned(admin):
    note = admin.post(NOTES, json={"subject": SUBJECT, "text": "x", "name": "Old\x1b[0m"}).json()
    assert note["context"]["name"] == "Old[0m"
    changed = admin.patch(f"{NOTES}/{note['id']}", json={"text": "x", "revision": note["revision"], "name": "New"})
    assert changed.json()["context"]["name"] == "New"
    assert admin.post(NOTES, json={"subject": SUBJECT, "text": "x", "name": "n" * 121}).status_code == 422


def test_a_controller_that_cannot_be_read_hides_no_notes_of_a_site_that_is_here(admin, viewer, monkeypatch):
    from homelab_probe.client import UniFiAPIError
    from homelab_probe.server import snapshots_api

    add(admin)

    def down(request, name):
        raise UniFiAPIError("the controller cannot be reached", kind="connection")

    monkeypatch.setattr(notes_api, "site_of", down)
    monkeypatch.setattr(snapshots_api, "site_of", down)
    assert viewer.get("/api/v1/unifi/sites/site-1/notes").json()["total"] == 1                  # by the site's id
    assert viewer.get("/api/v1/unifi/sites/site-1/notes/subjects").json()["total"] == 1
    assert viewer.get(NOTES).status_code in (502, 504)                                          # by name: needs the controller
    assert add(admin).status_code in (502, 504)                                                 # a write needs it too
    assert viewer.get("/api/v1/unifi/sites/unknown-site/notes").status_code in (502, 504)


def test_a_local_read_is_only_of_a_real_directory_whose_file_names_that_site(admin, app, tmp_path):
    from types import SimpleNamespace

    add(admin)
    request = SimpleNamespace(app=app)
    assert notes_api.local_store(request, "site-1").listing()[0]["text"] == "moved to the shelf"
    assert notes_api.local_store(request, "site-2") is None and notes_api.local_store(request, "default") is None
    path = tmp_path / FILE
    document = json.loads(path.read_text())
    path.write_text(json.dumps({**document, "site": "another"}))
    assert notes_api.local_store(request, "site-1") is None                       # the full path says why
    path.write_text("not json")
    assert notes_api.local_store(request, "site-1") is None
    path.write_text(json.dumps(document))
    (tmp_path / "snapshots" / "linked").symlink_to(tmp_path / "snapshots" / "site-1")
    assert notes_api.local_store(request, "linked") is None


def test_notes_that_cannot_be_read_are_not_zero_notes_in_the_findings_list(admin, viewer, tmp_path):
    add(admin, subject="finding:" + "a" * 16)
    (tmp_path / FILE).write_text("not json")
    body = viewer.get(f"{SITE}/findings").json()
    assert body["notes_available"] is False and any("Note counts are not shown" in text for text in body["limitations"])
    assert all(item["note_count"] == 0 for item in body["items"])
    (tmp_path / FILE).unlink()
    assert viewer.get(f"{SITE}/findings").json()["notes_available"] is True


def test_a_damaged_file_is_a_fixed_500_for_the_subjects_too(admin, viewer, tmp_path):
    add(admin)
    (tmp_path / FILE).write_text("not json")
    response = viewer.get(f"{NOTES}/subjects")
    assert response.status_code == 500 and response.json()["error"] == "notes_unreadable"
    assert str(tmp_path) not in response.text
