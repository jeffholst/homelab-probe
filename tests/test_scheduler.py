"""The scheduler of `hlp serve --scheduler` (issue #222)."""

import dataclasses
import io
import json
import os
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("tomlkit")

from fastapi.testclient import TestClient  # noqa: E402
from server_support import CONFIG, auth_for  # noqa: E402
from test_notify import FakePost  # noqa: E402

from homelab_probe import cli, logs  # noqa: E402
from homelab_probe import notify as notify_module  # noqa: E402
from homelab_probe.client import UniFiAPIError  # noqa: E402
from homelab_probe.config import ConfigError  # noqa: E402
from homelab_probe.demo.session import DemoSession  # noqa: E402
from homelab_probe.server import runner, scheduler  # noqa: E402
from homelab_probe.server.app import create_app  # noqa: E402
from homelab_probe.server.service import ControllerService  # noqa: E402
from homelab_probe.server.wizard import MODE_SETUP, SetupState  # noqa: E402

NTFY = "https://ntfy.example/topic-9f8e7d6c5b4a"
STATE = Path("snapshots/site-1/notify-state.json")


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def post(monkeypatch):
    fake = FakePost(200)
    monkeypatch.setattr(notify_module.requests, "post", fake)
    return fake


def make_app(tmp_path, clock, destination=True, **changes):
    config = dataclasses.replace(CONFIG, notify_ntfy_url=NTFY if destination else "", **changes)
    session = DemoSession()
    session.fx["legacy"]["device"][0]["overheating"] = False
    app = create_app(config, state_dir=tmp_path, hosts=["testserver"], auth=auth_for(tmp_path),
                     service=ControllerService(config, session=session, ttl=0), scheduler=True)
    app.state.scheduler._clock = clock
    app.state.fake = session
    return app


@pytest.fixture
def app(tmp_path, clock, post):
    return make_app(tmp_path, clock)


def capture():
    """A stream that receives the log records as JSON lines, and a function that reads them."""
    stream = io.StringIO()
    logs.configure("json", "INFO", stream=stream)
    return lambda: [json.loads(line) for line in stream.getvalue().splitlines()]


def job(results, name):
    return next(result for result in results if result.job == name)


def state(tmp_path):
    return json.loads((tmp_path / STATE).read_text())


# -- when it runs -----------------------------------------------------------------------------------------------

def test_nothing_runs_before_the_setup_is_finished(tmp_path, clock, post):
    app = make_app(tmp_path, clock)
    app.state.setup = SetupState(MODE_SETUP, "no_config")
    assert app.state.scheduler.ready() is False and app.state.scheduler.tick() == []
    assert app.state.fake.calls == []
    app.state.setup.mode = None                                   # the setup finished
    assert app.state.scheduler.ready() is True and app.state.scheduler.tick()


def test_without_a_service_there_is_nothing_to_run(tmp_path, clock, post):
    app = make_app(tmp_path, clock)
    app.state.service = None
    assert app.state.scheduler.tick() == []


def test_the_diagnose_job_runs_at_once_and_then_every_interval(app, clock, tmp_path):
    first = app.state.scheduler.tick()
    assert [r.job for r in first] == ["diagnose", "snapshot"]
    assert app.state.scheduler.tick() == []                                    # nothing is due yet
    clock.advance(14 * 60)
    assert app.state.scheduler.tick() == []
    clock.advance(60)
    assert [r.job for r in app.state.scheduler.tick()] == ["diagnose"]         # 15 minutes
    clock.advance(24 * 3600)
    assert [r.job for r in app.state.scheduler.tick()] == ["diagnose", "snapshot"]


def test_the_intervals_come_from_the_settings_and_zero_hours_means_no_snapshots(tmp_path, clock, post):
    app = make_app(tmp_path, clock, scheduler_diagnose_minutes=5, scheduler_snapshot_hours=0)
    assert [r.job for r in app.state.scheduler.tick()] == ["diagnose"]
    clock.advance(5 * 60)
    assert [r.job for r in app.state.scheduler.tick()] == ["diagnose"]
    assert not (tmp_path / "snapshots" / "site-1").exists() or not list((tmp_path / "snapshots" / "site-1").glob("snapshot-*"))


# -- the diagnose job and the notification state shared with cron ----------------------------------------------

def test_a_first_run_records_the_findings_as_reported_instead_of_announcing_all_of_them(app, tmp_path, post):
    result = job(app.state.scheduler.tick(), "diagnose")
    assert (result.result, result.reason) == ("ok", "baseline") and post.calls == []
    saved = state(tmp_path)
    assert saved["site"] == "site-1" and saved["active"] and result.findings > 0


def test_a_later_run_with_nothing_new_sends_nothing(app, clock, post):
    app.state.scheduler.tick()
    clock.advance(15 * 60)
    result = job(app.state.scheduler.tick(), "diagnose")
    assert (result.result, result.reason) == ("ok", "nothing") and post.calls == []


def forget_one(tmp_path):
    """Remove one remembered finding (not an event-based one, which a run without the event log does not look at)."""
    saved = state(tmp_path)
    forgotten = next(key for key in saved["active"] if key.startswith("device.offline|"))
    del saved["active"][forgotten]
    (tmp_path / STATE).write_text(json.dumps(saved))
    return forgotten


def test_a_new_finding_is_announced_once_to_the_destination_and_remembered(app, clock, tmp_path, post):
    app.state.scheduler.tick()
    forgotten = forget_one(tmp_path)
    clock.advance(15 * 60)
    result = job(app.state.scheduler.tick(), "diagnose")
    assert (result.result, result.reason) == ("ok", "sent") and result.destinations == "ntfy:sent"
    assert len(post.calls) == 1 and forgotten in state(tmp_path)["active"]
    clock.advance(15 * 60)
    app.state.scheduler.tick()
    assert len(post.calls) == 1                                             # it is not announced again


def test_the_command_line_and_the_scheduler_share_one_state(app, clock, tmp_path, post, monkeypatch, capsys):
    app.state.scheduler.tick()                                              # the scheduler records the baseline
    monkeypatch.chdir(tmp_path)
    client = app.state.service.client()
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, c: client))
    monkeypatch.setenv("UNIFI_URL", "https://controller.example")
    monkeypatch.setenv("UNIFI_API_KEY", "sekret-api-key-0123456789")
    monkeypatch.setenv("NOTIFY_NTFY_URL", NTFY)
    capsys.readouterr()
    cli.main(["diagnose", "--no-events", "--notify"])
    assert "nothing new, worse or fixed" in capsys.readouterr().err and post.calls == []
    forget_one(tmp_path)
    cli.main(["diagnose", "--no-events", "--notify"])                       # cron announces it ...
    assert len(post.calls) == 1
    clock.advance(15 * 60)
    assert job(app.state.scheduler.tick(), "diagnose").reason == "nothing"  # ... so the scheduler has nothing to say
    assert len(post.calls) == 1


def test_a_message_that_could_not_be_delivered_is_a_failed_run_and_is_tried_again(tmp_path, clock, monkeypatch):
    fake = FakePost(500)
    monkeypatch.setattr(notify_module.requests, "post", fake)
    app = make_app(tmp_path, clock)
    app.state.scheduler.tick()
    forgotten = forget_one(tmp_path)
    clock.advance(15 * 60)
    result = job(app.state.scheduler.tick(), "diagnose")
    assert (result.result, result.reason, result.destinations) == ("failed", "undelivered", "ntfy:failed")
    assert forgotten not in state(tmp_path)["active"]                       # not remembered: the next run tries again
    monkeypatch.setattr(notify_module.requests, "post", FakePost(200))
    clock.advance(15 * 60)
    assert job(app.state.scheduler.tick(), "diagnose").destinations == "ntfy:sent"


def test_without_a_destination_it_only_counts_the_findings_and_writes_no_state(tmp_path, clock, post):
    app = make_app(tmp_path, clock, destination=False)
    result = job(app.state.scheduler.tick(), "diagnose")
    assert (result.result, result.reason) == ("ok", "no_destination") and result.findings > 0
    assert not (tmp_path / STATE).exists() and post.calls == []


def test_a_partial_read_never_touches_the_state(app, clock, tmp_path, post, monkeypatch):
    app.state.scheduler.tick()
    before = (tmp_path / STATE).read_text()
    real = scheduler.diagnose_document

    def partial(*args, **kwargs):
        document = real(*args, **kwargs)
        document.meta["complete"] = False
        document.data["findings"] = []                                       # as if everything had cleared
        return document

    monkeypatch.setattr(scheduler, "diagnose_document", partial)
    clock.advance(15 * 60)
    result = job(app.state.scheduler.tick(), "diagnose")
    assert (result.result, result.reason) == ("skipped", "partial_data")
    assert (tmp_path / STATE).read_text() == before and post.calls == []


def test_the_settings_file_is_read_again_at_every_run(app, clock, tmp_path):
    first = job(app.state.scheduler.tick(), "diagnose")
    (tmp_path / "hlp.toml").write_text('[[ignore]]\nsubject = "*"\nreason = "all"\n')
    clock.advance(15 * 60)
    second = job(app.state.scheduler.tick(), "diagnose")
    assert first.findings > 0 and second.findings == 0


def test_a_controller_that_cannot_be_read_is_a_failed_run_and_the_next_one_recovers(app, clock, tmp_path, post):
    app.state.scheduler.tick()
    before = (tmp_path / STATE).read_text()
    app.state.fake.status = 503
    clock.advance(24 * 3600)
    failed = app.state.scheduler.tick()
    assert [(r.job, r.result) for r in failed] == [("diagnose", "failed"), ("snapshot", "failed")]
    assert failed[0].reason.startswith("controller_") and (tmp_path / STATE).read_text() == before
    app.state.fake.status = None
    clock.advance(24 * 3600)
    assert [r.result for r in app.state.scheduler.tick()] == ["ok", "ok"]


@pytest.mark.parametrize("error, reason", [
    (ConfigError("a message with /secret/path"), "config"), (OSError(28, "No space left"), "storage"),
    (RuntimeError("a secret token sekret-123"), "error"), (UniFiAPIError("x", kind="timeout"), "controller_timeout"),
])
def test_a_job_that_raises_is_a_failed_run_with_a_fixed_reason_and_never_ends_the_scheduler(
        app, clock, monkeypatch, error, reason):
    def boom(*args, **kwargs):
        raise error

    read = capture()
    monkeypatch.setattr(scheduler, "diagnose_document", boom)
    result = job(app.state.scheduler.tick(), "diagnose")
    assert (result.result, result.reason) == ("failed", reason)
    logged = json.dumps(read())
    assert "sekret-123" not in logged and "/secret/path" not in logged and "No space" not in logged
    clock.advance(15 * 60)
    monkeypatch.undo()
    assert job(app.state.scheduler.tick(), "diagnose").result == "ok"       # the next run works


# -- the snapshot job --------------------------------------------------------------------------------------------

def snapshots(tmp_path):
    return sorted((tmp_path / "snapshots" / "site-1").glob("snapshot-*.json"))


def test_a_snapshot_is_saved_in_the_directory_of_its_site_and_old_ones_are_pruned(tmp_path, clock, post):
    app = make_app(tmp_path, clock, scheduler_snapshot_keep=2, scheduler_snapshot_hours=1)
    for _ in range(4):
        result = job(app.state.scheduler.tick(), "snapshot")
        assert (result.result, result.reason) == ("ok", "saved")
        clock.advance(3600)
    assert len(snapshots(tmp_path)) == 2 and result.removed == 1
    assert oct(os.stat(snapshots(tmp_path)[0]).st_mode & 0o777) == "0o600"


def test_after_a_restart_the_first_snapshot_waits_until_the_newest_is_as_old_as_the_interval(tmp_path, clock, post):
    first = make_app(tmp_path, clock)
    first.state.scheduler.tick()
    saved = snapshots(tmp_path)[0]
    os.utime(saved, (clock(), clock() - 3600))                              # taken an hour ago, real time
    restarted = create_app(first.state.config, state_dir=tmp_path, hosts=["testserver"], scheduler=True,
                           auth=auth_for(tmp_path / "other"), service=ControllerService(
                               first.state.config, session=DemoSession(), ttl=0))
    restarted.state.scheduler._clock = clock
    assert [r.job for r in restarted.state.scheduler.tick()] == ["diagnose"]       # no snapshot yet: it is recent
    os.utime(saved, (clock(), clock() - 25 * 3600))                          # now it is older than 24 hours
    again = create_app(first.state.config, state_dir=tmp_path, hosts=["testserver"], scheduler=True,
                       auth=auth_for(tmp_path / "third"), service=ControllerService(
                           first.state.config, session=DemoSession(), ttl=0))
    again.state.scheduler._clock = clock
    assert "snapshot" in [r.job for r in again.state.scheduler.tick()]


def test_a_site_that_cannot_be_looked_up_makes_the_first_snapshot_due_at_once(app, clock):
    app.state.fake.status = 503
    assert [r.job for r in app.state.scheduler.tick()] == ["diagnose", "snapshot"]


def test_a_symbolic_link_is_not_followed_by_the_scheduler(app, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "snapshots").symlink_to(elsewhere)
    result = job(app.state.scheduler.tick(), "snapshot")
    assert (result.result, result.reason) == ("failed", "snapshots_unsafe") and list(elsewhere.iterdir()) == []


# -- what is logged ----------------------------------------------------------------------------------------------

def test_every_run_has_its_own_id_and_one_record_without_names_addresses_or_messages(app, clock, tmp_path):
    read = capture()
    app.state.scheduler.tick()
    clock.advance(15 * 60)
    app.state.scheduler.tick()
    records = [r for r in read() if r["event"] == "scheduler.run"]
    assert len(records) == 3
    ids = [r["run_id"] for r in records]
    assert len(set(ids)) == 3 and all(len(i) >= 8 for i in ids)
    assert {r["job"] for r in records} == {"diagnose", "snapshot"}
    text = json.dumps(records)
    for secret in ("ntfy.example", "topic-9f8e7d6c5b4a", "Gateway", "Garage", "site-1", str(tmp_path)):
        assert secret not in text, secret
    assert all(isinstance(r["duration_ms"], int) for r in records)


def test_a_failed_run_is_logged_as_a_warning(app, monkeypatch):
    read = capture()
    monkeypatch.setattr(scheduler, "snapshot_document", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    app.state.scheduler.tick()
    failed = [r for r in read() if r["event"] == "scheduler.run" and r["level"] == "WARNING"]
    assert len(failed) == 1 and failed[0]["reason"] == "storage"


# -- the thread and the command ---------------------------------------------------------------------------------

def test_the_thread_runs_with_the_server_and_stops_with_it(tmp_path, post):
    config = dataclasses.replace(CONFIG, notify_ntfy_url=NTFY)
    session = DemoSession()
    app = create_app(config, state_dir=tmp_path, hosts=["testserver"], auth=auth_for(tmp_path), scheduler=True,
                     service=ControllerService(config, session=session, ttl=0))
    app.state.scheduler.tick_seconds = 0.05
    ticks = []
    real_tick = app.state.scheduler.tick
    app.state.scheduler.tick = lambda: ticks.append(1) or real_tick()
    read = capture()
    with TestClient(app):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and ({"diagnose", "snapshot"} - set(app.state.scheduler.last)
                                               or len(ticks) < 3):
            time.sleep(0.05)
        assert {"diagnose", "snapshot"} <= set(app.state.scheduler.last) and len(ticks) >= 3
        thread = app.state.scheduler._thread
        assert thread is not None and thread.is_alive() and thread.daemon
    assert not thread.is_alive()
    started = [r for r in read() if r["event"] == "scheduler.start"]
    assert len(started) == 1 and started[0]["diagnose_minutes"] == 15 and started[0]["keep"] == 30


def test_an_app_without_the_scheduler_has_none(tmp_path):
    app = create_app(CONFIG, state_dir=tmp_path, hosts=["testserver"], auth=auth_for(tmp_path))
    assert app.state.scheduler is None
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200


def test_the_scheduler_writes_files_so_a_read_only_app_refuses_it(tmp_path):
    with pytest.raises(ValueError, match="read_only"):
        create_app(CONFIG, state_dir=tmp_path, hosts=["testserver"], read_only=True, scheduler=True)


def test_serve_starts_the_scheduler_only_when_asked_and_refuses_the_combinations(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(runner.uvicorn, "run", lambda app, **kwargs: calls.append(app))
    (tmp_path / ".env").write_text("UNIFI_URL=https://controller.example\nUNIFI_API_KEY=the-api-key-0123456789\n")
    auth_for(tmp_path)
    assert cli.main(["serve", "--data-dir", str(tmp_path)]) == 0
    assert cli.main(["serve", "--scheduler", "--data-dir", str(tmp_path)]) == 0
    assert [app.state.scheduler is not None for app in calls] == [False, True]
    for argv in (["serve", "--scheduler", "--read-only"], ["--demo", "serve", "--scheduler"]):
        with pytest.raises(SystemExit) as caught:
            cli.main(argv)
        assert caught.value.code == cli.EXIT_USAGE
    assert "cannot be combined" in capsys.readouterr().err


def test_stopping_a_scheduler_that_never_started_is_harmless(tmp_path, clock, post):
    make_app(tmp_path, clock).state.scheduler.stop()


@pytest.mark.parametrize("name, parse, good, bad", [
    ("SCHEDULER_DIAGNOSE_MINUTES", "parse_scheduler_diagnose_minutes", ("", "1", "1440"), ("0", "1441", "x", "1.5")),
    ("SCHEDULER_SNAPSHOT_HOURS", "parse_scheduler_snapshot_hours", ("", "0", "720"), ("-1", "721", "x")),
    ("SCHEDULER_SNAPSHOT_KEEP", "parse_scheduler_snapshot_keep", ("", "1", "10000"), ("0", "10001", "x")),
])
def test_the_settings_of_the_scheduler_are_validated_like_every_other_setting(name, parse, good, bad):
    from homelab_probe import config as config_module
    from homelab_probe.setup import validate_field

    parser = getattr(config_module, parse)
    assert [isinstance(parser(value), int) for value in good] == [True] * len(good)
    assert parser("") == {"SCHEDULER_DIAGNOSE_MINUTES": 15, "SCHEDULER_SNAPSHOT_HOURS": 24,
                          "SCHEDULER_SNAPSHOT_KEEP": 30}[name]
    for value in bad:
        with pytest.raises(ConfigError, match=name):
            parser(value)
        assert validate_field({name: value}, name)                             # the setup checks it too
    built = config_module.build_config({"UNIFI_URL": "https://c.example", "UNIFI_API_KEY": "k" * 20, name: good[1]})
    assert getattr(built, name.lower()) == int(good[1])


# -- stopping, and what the stop guarantees ------------------------------------------------------------------------

def test_a_job_that_is_running_when_the_server_stops_sends_and_writes_nothing_after_it(tmp_path, clock, post, monkeypatch):
    import threading

    app = make_app(tmp_path, clock)
    started, release = threading.Event(), threading.Event()
    real = scheduler.diagnose_document

    def slow(*args, **kwargs):
        started.set()
        assert release.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(scheduler, "diagnose_document", slow)
    monkeypatch.setattr(scheduler, "STOP_WAIT_SECONDS", 0.2)
    app.state.scheduler.tick_seconds = 0.05
    read = capture()
    app.state.scheduler.start()
    assert started.wait(10)
    app.state.scheduler.stop()                                      # gives up waiting after 0.2 s, and says so
    assert app.state.scheduler._thread.is_alive()
    release.set()
    app.state.scheduler._thread.join(10)
    assert not app.state.scheduler._thread.is_alive()
    assert post.calls == [] and not (tmp_path / STATE).exists()     # nothing was baselined, sent or saved
    assert app.state.scheduler.last["diagnose"].reason == "stopping"
    assert [r for r in read() if r["event"] == "warning" and "did not finish" in r["msg"]]


def test_the_snapshot_job_saves_nothing_once_the_stop_is_asked(tmp_path, clock, post, monkeypatch):
    app = make_app(tmp_path, clock)
    real = scheduler.snapshot_document

    def stopping(*args, **kwargs):
        app.state.scheduler._stop.set()
        return real(*args, **kwargs)

    monkeypatch.setattr(scheduler, "snapshot_document", stopping)
    result = job(app.state.scheduler.tick(), "snapshot")
    assert (result.result, result.reason) == ("skipped", "stopping") and snapshots(tmp_path) == []


def test_a_stop_asked_while_the_job_waits_for_the_state_ends_the_wait(tmp_path, clock, post, monkeypatch):
    import threading

    from homelab_probe.util import file_lock

    app = make_app(tmp_path, clock)
    lock = tmp_path / "snapshots" / "site-1" / "notify-state.json.lock"
    result = []
    holder_ready, release = threading.Event(), threading.Event()

    def hold():
        with file_lock(lock):
            holder_ready.set()
            release.wait(10)

    holder = threading.Thread(target=hold)
    holder.start()
    assert holder_ready.wait(10)
    worker = threading.Thread(target=lambda: result.append(job(app.state.scheduler.tick(), "diagnose")))
    worker.start()
    time.sleep(0.3)
    app.state.scheduler._stop.set()                                   # the wait for the lock ends at once
    worker.join(5)
    release.set()
    holder.join()
    assert not worker.is_alive() and (result[0].result, result[0].reason) == ("skipped", "stopping")
    assert post.calls == [] and not (tmp_path / STATE).exists()


def test_a_planned_message_is_not_sent_when_the_stop_comes_before_the_send(tmp_path, monkeypatch):
    from test_notify import finding
    from test_notify_lock import CONFIG, SITE

    from homelab_probe.diagnose import CRITICAL
    from homelab_probe.notify import process
    from homelab_probe.settings import DiagnoseSettings

    sent = FakePost(200)
    monkeypatch.setattr(notify_module.requests, "post", sent)
    asked = iter([False, True])                                        # not at the start, then yes before the send
    outcome = process([finding(CRITICAL, "Gateway", "device is offline")], config=CONFIG, settings=DiagnoseSettings(),
                      site=SITE, base=tmp_path, cancelled=lambda: next(asked))
    assert outcome.kind == "cancelled" and sent.calls == [] and not (tmp_path / "site-1" / "notify-state.json").exists()


def test_a_scheduler_stopped_before_it_runs_anything_runs_nothing(tmp_path, clock, post):
    app = make_app(tmp_path, clock)
    app.state.scheduler._stop.set()
    results = app.state.scheduler.tick()
    assert [(r.job, r.result) for r in results] == [("diagnose", "skipped"), ("snapshot", "skipped")]
    assert post.calls == [] and not (tmp_path / STATE).exists() and snapshots(tmp_path) == []
