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
    edited = admin.patch(f"{NOTES}/{note['id']}", json={"text": "back in the rack"})
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
    assert viewer.patch(f"{NOTES}/{ident}", json={"text": "y"}).status_code == 403
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
    for call in (lambda: add(no_token), lambda: no_token.patch(f"{NOTES}/{ident}", json={"text": "y"}),
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
    for response in (add(admin), admin.patch(f"{NOTES}/{ident}", json={"text": "y"}), admin.delete(f"{NOTES}/{ident}")):
        assert response.status_code == 403 and response.json()["error"] == "read_only"
    assert admin.get(NOTES).json()["total"] == 1


def test_a_note_is_audited_by_id_and_kind_never_by_its_text_or_the_mac(admin, tmp_path):
    ident = add(admin, "the secret reason").json()["id"]
    admin.patch(f"{NOTES}/{ident}", json={"text": "another secret"})
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
    {"subject": SUBJECT, "text": "  \x1b "}, {"subject": SUBJECT, "text": "x" * 2001}, {"subject": SUBJECT},
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
    assert admin.patch(f"{NOTES}/{ident}", json={"text": "y"}).status_code in (404, 422)
    assert admin.delete(f"{NOTES}/{ident}").status_code in (404, 422)


def test_an_unknown_note_and_an_unknown_site_are_404(admin):
    assert admin.patch(f"{NOTES}/0123456789abcdef", json={"text": "y"}).json()["error"] == "notes_not_found"
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
