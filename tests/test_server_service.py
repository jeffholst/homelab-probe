"""ControllerService: a client per build, one session and one cache for all of them (issue #183, PR 2)."""

import threading

import pytest

pytest.importorskip("fastapi")

from contract import RecordingSession  # noqa: E402

from homelab_probe.client import UniFiAPIError, UniFiClient  # noqa: E402
from homelab_probe.config import Config  # noqa: E402
from homelab_probe.demo.session import DemoSession  # noqa: E402
from homelab_probe.documents import Document, diagnose_document, events_document, wan_document  # noqa: E402
from homelab_probe.server.cache import ResponseCache  # noqa: E402
from homelab_probe.server.service import CachingClient, ControllerService, request_key  # noqa: E402
from homelab_probe.snapshot import EventQuery  # noqa: E402

CONFIG = Config(controller_url="https://controller.example", api_key="the-api-key-0123456789", parallel=4)


@pytest.fixture
def session():
    return DemoSession()


@pytest.fixture
def service(session):
    return ControllerService(CONFIG, session=session)


# -- the client ------------------------------------------------------------------------------------------------

def test_every_build_gets_a_client_of_its_own_that_shares_the_session_and_the_cache(service, session):
    first, second = service.client(), service.client()
    assert first is not second and isinstance(first, CachingClient) and isinstance(first, UniFiClient)
    assert first.session is second.session is session and first.cache is second.cache is service.cache
    assert first.workers == 4 and first.base_url == "https://controller.example" and first.verify_ssl is True
    first.attempts_made = 7
    assert second.attempts_made == 0                             # nothing is shared that a request could disturb


def test_two_clients_read_an_endpoint_once(service, session):
    assert service.client().info() == service.client().info() == session.fx["info"]
    assert session.calls.count("/proxy/network/integration/v1/info") == 1


def test_a_read_that_returns_a_list_is_a_copy_nobody_else_sees_change(service):
    sites = service.client().sites()
    sites.append({"id": "invented"})
    assert {"id": "invented"} not in service.client().sites()


def test_the_event_log_window_is_rounded_so_repeated_queries_share_one_post(service, session):
    base = 1_800_000_000_000
    one = {"timestampFrom": base + 1, "timestampTo": base + 2_000, "pageNumber": 0, "pageSize": 100}
    two = {"timestampFrom": base + 5_000, "timestampTo": base + 9_999, "pageNumber": 0, "pageSize": 100}
    third_page = {**one, "pageNumber": 1}
    later = {**one, "timestampFrom": base + 61_000, "timestampTo": base + 62_000}
    client = service.client()
    client.system_log("default", one)
    client.system_log("default", two)
    assert len(session.posts) == 1
    client.system_log("default", third_page)
    client.system_log("default", later)
    assert len(session.posts) == 3                               # another page and another half minute each read again


def test_a_query_key_the_event_log_does_not_take_is_refused_before_the_cache_or_the_wire(service, session):
    with pytest.raises(ValueError, match="unsupported system-log query key"):
        service.client().system_log("default", {"timestampFrom": 1, "action": "delete"})
    assert session.posts == []


def test_the_request_key_ignores_the_order_and_the_type_of_parameters():
    assert request_key("GET", "/p", {"b": 2, "a": 1}, 30) == request_key("GET", "/p", {"a": "1", "b": "2"}, 30)
    assert request_key("GET", "/p", None, 30) == request_key("GET", "/p", {}, 30)
    assert request_key("GET", "/p", {"x": 1}, 30) != request_key("POST", "/p", {"x": 1}, 30)
    assert request_key("POST", "s", {"timestampFrom": "later"}, 30)[2] == (("timestampFrom", "later"),)


# -- documents through the service -----------------------------------------------------------------------------

def test_a_document_built_through_the_service_equals_the_one_the_command_line_builds(service, session):
    now = 1_800_000_000_000
    built = service.build(lambda client: wan_document(client, "default", echo=False, now_ms=now))
    plain = UniFiClient("https://controller.example", "key")
    plain.session = session                                         # the same data, read without the service
    direct = wan_document(plain, "default", echo=False, now_ms=now)
    assert built.document.data == direct.data and built.document.name == "wan"


def test_many_documents_at_once_read_each_endpoint_once(session):
    service = ControllerService(CONFIG, session=session)
    results, errors = [], []

    def build(which):
        try:
            if which % 2:
                results.append(service.build(lambda c: diagnose_document(c, "default", echo=False)).document.data)
            else:
                results.append(service.build(lambda c: events_document(c, "default", EventQuery(24 * 3600), echo=False))
                               .document.data)
        except Exception as e:     # noqa: BLE001 (reported by the assertion below)
            errors.append(e)

    threads = [threading.Thread(target=build, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors and len(results) == 8
    counts = {path: session.calls.count(path) for path in set(session.calls)}
    assert counts and max(counts.values()) == 1, counts           # no endpoint was read twice
    assert len(session.posts) == 1                               # nor the event log


def test_a_second_build_reads_nothing_while_the_answers_are_fresh(service, session):
    service.build(lambda c: diagnose_document(c, "default", echo=False))
    reads = (len(session.calls), len(session.posts))
    service.build(lambda c: diagnose_document(c, "default", echo=False))
    assert (len(session.calls), len(session.posts)) == reads


def test_a_manual_refresh_reads_again(service, session):
    service.build(lambda c: wan_document(c, "default", echo=False))
    first = len(session.calls)
    service.refresh()
    service.build(lambda c: wan_document(c, "default", echo=False))
    assert len(session.calls) == 2 * first


def test_only_gets_and_the_one_event_log_post_reach_the_controller(session):
    recording = RecordingSession(session)
    service = ControllerService(CONFIG, session=recording)
    service.build(lambda c: diagnose_document(c, "default", echo=False))           # a PUT or DELETE would raise
    methods = {method for method, _, _ in recording.exchanges}
    assert methods == {"GET", "POST"} and [p for m, p, _ in recording.exchanges if m == "POST"][0].endswith("/system-log/all")


# -- generated_at and warnings ---------------------------------------------------------------------------------

def test_generated_at_is_when_the_oldest_answer_was_read_not_when_the_page_was_built(session):
    now = [1_800_000_000.0]
    service = ControllerService(CONFIG, session=session)
    service.cache = ResponseCache(ttl=30, wall=lambda: now[0])
    first = service.build(lambda c: Document("x", c.info()))
    now[0] += 20
    second = service.build(lambda c: Document("x", c.info()))
    assert first.generated_at == second.generated_at == "2027-01-15T08:00:00Z"
    assert len(second.generated_at) == 20 and second.generated_at.endswith("Z")


def test_a_build_that_read_nothing_says_now(service):
    built = service.build(lambda c: Document("x", {}))
    assert built.generated_at.endswith("Z") and built.warnings == []


def test_the_warnings_of_a_degraded_read_come_with_the_build_and_nothing_is_printed(service, session, capsys):
    session_real = session.get

    def get(url, params=None, verify=True, timeout=None):
        if url.endswith("/stat/rogueap"):
            from conftest import FakeResponse
            return FakeResponse(503, {})
        return session_real(url, params, verify, timeout)

    session.get = get
    from homelab_probe.documents import wifi_document

    built = service.build(lambda c: wifi_document(c, "default", echo=False))
    assert any("neighbor" in w.lower() or "rogue" in w.lower() for w in built.warnings) or built.warnings
    assert capsys.readouterr().err == ""


def test_a_stale_answer_is_served_with_a_warning_when_the_controller_stops_answering(session):
    clock = [1000.0]
    service = ControllerService(CONFIG, session=session, ttl=30, stale_ttl=600, clock=lambda: clock[0])
    service.build(lambda c: Document("x", c.info()))
    clock[0] += 100
    session.status = 503
    built = service.build(lambda c: Document("x", c.info()))
    assert built.document.data == session.fx["info"]
    assert any("served from the cache" in w for w in built.warnings)


# -- readiness -------------------------------------------------------------------------------------------------

def test_ready_means_the_controller_can_be_read_and_names_a_failure_by_its_kind_only(session):
    clock = [1000.0]
    service = ControllerService(CONFIG, session=session, ttl=30, stale_ttl=600, error_ttl=5, clock=lambda: clock[0])
    assert service.ready() == (True, "")
    clock[0] += 100
    session.status = 401
    assert service.ready() == (True, "")                         # an old answer is still served (with a warning)
    clock[0] += 1000
    ready, reason = service.ready()
    assert (ready, reason) == (False, "unauthorized") and "controller.example" not in reason


def test_a_failure_with_no_kind_is_called_an_error(monkeypatch, service):
    def broken(self):
        raise UniFiAPIError("odd")

    monkeypatch.setattr(UniFiClient, "info", broken)
    assert service.ready() == (False, "error")


# -- the real session ------------------------------------------------------------------------------------------

def test_the_real_session_carries_the_key_and_refuses_cookies():
    service = ControllerService(CONFIG)
    session = service.session
    assert session.headers["X-API-KEY"] == CONFIG.api_key and service.demo is False
    import http.cookiejar
    import urllib.request

    request = urllib.request.Request("https://controller.example/")
    response = type("R", (), {"info": lambda self: type("M", (), {"get_all": lambda s, n, d=None: ["TOKEN=abc; Path=/"]})()})()
    session.cookies.extract_cookies(response, request)
    assert len(session.cookies) == 0
    assert isinstance(session.cookies._policy, http.cookiejar.DefaultCookiePolicy)


def test_a_demo_service_uses_the_synthetic_controller():
    service = ControllerService(CONFIG, demo=True)
    assert isinstance(service.session, DemoSession) and service.client().info()["applicationVersion"]
