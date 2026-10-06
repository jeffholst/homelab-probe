"""Two runs never both announce one finding: the notification step takes a lock on the state (issue #222)."""

import json
import os
import stat
import subprocess
import sys
import threading
import time

import pytest
from test_notify import FakePost, finding

from homelab_probe import notify as notify_module
from homelab_probe.config import Config, ConfigError
from homelab_probe.diagnose import CRITICAL, WARNING
from homelab_probe.notify import process
from homelab_probe.settings import DiagnoseSettings
from homelab_probe.util import LockTimeout, file_lock

SITE = {"id": "site-1", "name": "Default", "ref": "default"}
CONFIG = Config(controller_url="https://controller.example", api_key="k" * 20,
                notify_ntfy_url="https://ntfy.example/topic-1234567890")


@pytest.fixture
def post(monkeypatch):
    fake = FakePost(200)
    monkeypatch.setattr(notify_module.requests, "post", fake)
    return fake


def run(tmp_path, findings, **kwargs):
    return process(findings, config=CONFIG, settings=DiagnoseSettings(), site=SITE, base=tmp_path, **kwargs)


# -- the lock ---------------------------------------------------------------------------------------------------

def test_a_lock_is_exclusive_owner_only_and_released_at_the_end_of_the_block(tmp_path):
    path = tmp_path / "dir" / "x.lock"
    with file_lock(path):
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600 and stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
        with pytest.raises(LockTimeout):
            with file_lock(path, timeout=0.2):
                pass
    with file_lock(path, timeout=0.2):                              # free again
        pass


def test_a_waiting_run_gets_its_turn_when_the_other_finishes(tmp_path):
    path = tmp_path / "x.lock"
    order = []

    def holder():
        with file_lock(path):
            order.append("first in")
            time.sleep(0.3)
            order.append("first out")

    thread = threading.Thread(target=holder)
    thread.start()
    time.sleep(0.1)
    with file_lock(path, timeout=5):
        order.append("second in")
    thread.join()
    assert order == ["first in", "first out", "second in"]


def test_a_lock_without_a_time_limit_waits_as_long_as_it_takes(tmp_path):
    path = tmp_path / "x.lock"
    done = []

    def holder():
        with file_lock(path):
            time.sleep(0.3)

    thread = threading.Thread(target=holder)
    thread.start()
    time.sleep(0.1)
    with file_lock(path):
        done.append(True)
    thread.join()
    assert done == [True]


# -- the notification step ---------------------------------------------------------------------------------------

def test_two_runs_with_the_same_new_finding_announce_it_once(tmp_path, monkeypatch):
    sent = []

    def slow_send(destinations, events, redact, timeout):
        sent.append(len(events))
        time.sleep(0.4)                                             # long enough for the other run to be waiting
        return [("ntfy", True, "")]

    monkeypatch.setattr(notify_module, "send", slow_send)
    run(tmp_path, [], baseline_only=True)                           # an empty baseline: every finding is new
    outcomes = []
    findings = [finding(CRITICAL, "Gateway", "device is offline")]
    threads = [threading.Thread(target=lambda: outcomes.append(run(tmp_path, findings))) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sent == [1] and sorted(o.kind for o in outcomes) == ["nothing", "sent"]


def test_a_run_in_another_process_holds_the_state_and_the_other_gives_up_with_a_clear_error(tmp_path, monkeypatch):
    lock = tmp_path / "site-1" / "notify-state.json.lock"
    lock.parent.mkdir(parents=True)
    holder = subprocess.Popen([sys.executable, "-c", (
        "import sys, time; from pathlib import Path; from homelab_probe.util import file_lock\n"
        "with file_lock(Path(sys.argv[1])):\n    print('held', flush=True); time.sleep(3)"), str(lock)],
        stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        monkeypatch.setattr(notify_module, "LOCK_WAIT_SECONDS", 0.3)
        sent = FakePost(200)
        monkeypatch.setattr(notify_module.requests, "post", sent)
        with pytest.raises(ConfigError, match="in use by another run"):
            run(tmp_path, [finding(CRITICAL, "Gateway", "device is offline")])
        assert sent.calls == [] and not (tmp_path / "site-1" / "notify-state.json").exists()
        monkeypatch.setattr(notify_module, "LOCK_WAIT_SECONDS", 20)
        assert run(tmp_path, [finding(CRITICAL, "Gateway", "device is offline")]).kind == "sent"    # it got its turn
    finally:
        holder.kill()
        holder.wait()


def test_the_outcome_says_what_happened_without_a_message(tmp_path, post):
    first = run(tmp_path, [finding(WARNING, "Garage AP", "device is offline")], baseline_if_new=True)
    assert first.kind == "baseline" and post.calls == []
    second = run(tmp_path, [finding(WARNING, "Garage AP", "device is offline")], baseline_if_new=True)
    assert second.kind == "nothing"
    third = run(tmp_path, [finding(WARNING, "Garage AP", "device is offline"),
                           finding(CRITICAL, "Gateway", "device is offline")], baseline_if_new=True)
    assert (third.kind, third.events, third.undelivered, third.results) == ("sent", 1, False, [("ntfy", True, "HTTP 200")])
    dry = run(tmp_path, [finding(CRITICAL, "NAS", "device is offline")], dry_run=True)
    assert dry.kind == "dry_run" and dry.events == 3 and len(post.calls) == 1       # NAS is new; two others cleared


def test_the_state_names_its_site_and_the_legacy_state_is_read_from_the_base(tmp_path, post):
    legacy = tmp_path / "notify-state.json"
    legacy.write_text(json.dumps({"version": 1, "active": {}}))
    run(tmp_path, [finding(WARNING, "Garage AP", "device is offline")], baseline_if_new=True)
    assert len(post.calls) == 1                                    # the legacy file existed: nothing was baselined
    assert json.loads((tmp_path / "site-1" / "notify-state.json").read_text())["site"] == "site-1"
    assert json.loads(legacy.read_text()) == {"version": 1, "active": {}}      # never changed


def test_a_damaged_state_is_a_warning_and_the_run_starts_empty(tmp_path, post):
    (tmp_path / "site-1").mkdir()
    (tmp_path / "site-1" / "notify-state.json").write_text("not json")
    warned = []
    outcome = run(tmp_path, [finding(WARNING, "Garage AP", "device is offline")], warn=warned.append)
    assert len(warned) == 1 and "not valid JSON" in warned[0] and outcome.kind == "sent"


def test_a_lock_that_cannot_be_taken_for_another_reason_is_the_error_itself(tmp_path, monkeypatch):
    from homelab_probe import util

    def broken(fd, operation):
        raise OSError(37, "No locks available")

    monkeypatch.setattr(util.fcntl, "flock", broken)
    with pytest.raises(OSError, match="No locks available"):
        with file_lock(tmp_path / "x.lock"):
            pass


def test_a_step_that_is_cancelled_before_it_starts_reads_sends_and_writes_nothing(tmp_path, post):
    outcome = run(tmp_path, [finding(CRITICAL, "Gateway", "device is offline")], cancelled=lambda: True)
    assert outcome.kind == "cancelled" and post.calls == [] and not (tmp_path / "site-1" / "notify-state.json").exists()
