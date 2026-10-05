"""docs/scheduling.md (issue #137): the snippets are real configuration files, so they are tested like code.

The cron line is run through ``sh`` against a stub, the systemd units, the launchd plist and the Dockerfile are parsed
and compared with what the program really does (its exit codes, its default state file, its arguments). Building the
image and running the units needs Docker and systemd, so those were run by hand for the pull request (see its text);
everything that can be checked without them is checked here.
"""

import configparser
import json
import plistlib
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time

import pytest
from docs_support import README, ROOT, anchors, headings, local_links

from homelab_probe import cli, notify
from homelab_probe.commands import COMMANDS, EXIT_ERROR
from homelab_probe.diagnose.model import EXIT_CRITICAL, EXIT_WARNING

PAGE = ROOT / "docs" / "scheduling.md"
TEXT = PAGE.read_text(encoding="utf-8")
INSTALL_DIR = "/opt/homelab-probe"
INTERVAL_MINUTES = 15
FINDINGS = {EXIT_WARNING, EXIT_CRITICAL}


def blocks(language, text=TEXT):
    """The fenced code blocks of one language, in order."""
    return re.findall(rf"^```{language}\n(.*?)^```", text, flags=re.S | re.M)


def section(title, text=TEXT):
    """The text under one ``##`` heading."""
    return text.split(f"\n## {title}\n", 1)[1].split("\n## ", 1)[0]


def parse_cli(arguments):
    """The real parser accepts these arguments (a command the docs name must exist and so must its options)."""
    args = cli.build_parser().parse_args(arguments)
    assert args.command in {command.name for command in COMMANDS}
    return args


def unit(text):
    parser = configparser.ConfigParser(strict=True, interpolation=None)
    parser.optionxform = str                                   # systemd keys are case sensitive
    parser.read_string(text)
    return parser


# -- the page ------------------------------------------------------------------------------------------------------

def test_the_page_covers_each_scheduler_and_is_linked():
    names = headings(TEXT)
    for wanted in ("Running on a schedule", "Before you pick a scheduler", "cron", "systemd timer",
                   "launchd (macOS)", "Docker", "`--watch` or a schedule?"):
        assert wanted in names, wanted
    assert "(docs/scheduling.md)" in README.read_text(encoding="utf-8")
    for page in ("README.md", "docs/notifications.md"):
        assert "scheduling" in (ROOT / page).read_text(encoding="utf-8"), page


def test_every_code_block_has_a_language():
    opening, languages = True, []
    for line in TEXT.splitlines():
        if line.lstrip().startswith("```"):
            if opening:
                languages.append(line.lstrip()[3:])
            opening = not opening
    assert opening and languages and all(languages), languages        # fences pair up and each names a language


def test_the_links_on_the_page_resolve():
    for target, anchor, written in local_links(PAGE):
        assert target.exists(), written
        if anchor and target.suffix == ".md":
            assert anchor in anchors(target.read_text(encoding="utf-8")), written


def test_nothing_secret_or_real_is_in_the_examples():
    assert not re.search(r"(?i)api_key\s*=\s*\S|token\s*=\s*\S", TEXT)
    addresses = [ip for ip in re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", TEXT) if ip != "127.0.0.1"]
    assert not addresses and not re.search(r"\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b", TEXT, flags=re.I)


def test_the_commands_the_page_gives_parse_with_the_real_parser():
    seen = []
    names = "|".join(c.name for c in COMMANDS)
    for line in TEXT.splitlines():
        match = re.search(rf"(?:venv/bin/|\s)hlp ((?:{names})\b[^;&|>#`]*)", line)
        if match:
            seen.append(shlex.split(match.group(1)))
    assert {"--notify", "--notify-baseline", "--fail-on"} <= {word for words in seen for word in words}
    for arguments in seen:
        parse_cli(arguments)


def test_the_installation_creates_a_user_writable_directory_before_cloning():
    instructions = section("Before you pick a scheduler")
    create_directory = 'sudo install -d -o "$(id -un)" /opt/homelab-probe'
    clone = "git clone https://github.com/jeffholst/homelab-probe /opt/homelab-probe"
    assert instructions.index(create_directory) < instructions.index(clone)


# -- cron ----------------------------------------------------------------------------------------------------------

def crontab_line():
    (block,) = blocks("text", section("cron"))
    (line,) = block.strip().splitlines()
    fields = line.split(None, 5)
    assert fields[:5] == ["*/15", "*", "*", "*", "*"]
    return fields[5]


def test_the_cron_line_runs_every_fifteen_minutes_like_the_other_schedulers():
    assert INTERVAL_MINUTES == 15
    assert unit(blocks("ini")[1]).get("Timer", "OnCalendar") == f"*:0/{INTERVAL_MINUTES}"
    assert plistlib.loads(blocks("xml")[0].encode())["StartInterval"] == INTERVAL_MINUTES * 60


LOCK = "/tmp/homelab-probe.lock"
FLOCK_STAND_IN = """#!/bin/sh
# util-linux `flock -n LOCKFILE COMMAND...` without the lock, so the rest of the line runs where flock is missing (macOS)
[ "$1" = "-n" ] || exit 64
shift 2
exec "$@"
"""


def cron_command(project, tmp_path):
    """The documented line, pointed at a temporary project and lock file."""
    return crontab_line().replace(INSTALL_DIR, str(project)).replace(LOCK, str(tmp_path / "lock"))


def cron_path(tmp_path, real_flock=False):
    """A cron-like PATH. By default ``flock`` is a stand-in that only runs the command, so the shell body of the line is
    tested on every platform (macOS has no flock); ``real_flock`` leaves the system's, for the test of the lock."""
    if real_flock:
        return "/usr/bin:/bin"
    stand_in = tmp_path / "bin" / "flock"
    stand_in.parent.mkdir(exist_ok=True)
    stand_in.write_text(FLOCK_STAND_IN)
    stand_in.chmod(stand_in.stat().st_mode | stat.S_IXUSR)
    return f"{stand_in.parent}:/usr/bin:/bin"


def run_cron(command, path):
    return subprocess.run(["/bin/sh", "-c", command], capture_output=True, text=True, env={"PATH": path})


@pytest.mark.skipif(sys.platform == "win32", reason="cron and sh")
@pytest.mark.parametrize("code", [0, 1, 2, 3, 4, 64, 127])
def test_the_cron_line_prints_only_when_the_tool_could_not_run(tmp_path, code):
    """The line is run as written, with a stub in place of the tool: findings stay silent, an error is one line."""
    stub = tmp_path / "venv" / "bin" / "hlp"
    stub.parent.mkdir(parents=True)
    (tmp_path / "snapshots").mkdir()
    stub.write_text(f"#!/bin/sh\necho findings\necho 'Notification: nothing new' >&2\nexit {code}\n")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    done = run_cron(cron_command(tmp_path, tmp_path), cron_path(tmp_path))
    log = (tmp_path / "snapshots" / "homelab-probe.log").read_text()
    assert log == "Notification: nothing new\n" and "findings" not in done.stdout + done.stderr
    if code in (0, *FINDINGS):
        assert done.stdout == "" and done.stderr == ""
    else:
        assert done.stdout == f"hlp failed (exit {code}), see snapshots/homelab-probe.log\n"


@pytest.mark.skipif(sys.platform == "win32", reason="cron and sh")
@pytest.mark.parametrize("failure", ["working directory", "snapshots directory", "log path"])
def test_the_cron_line_reports_shell_setup_failures(tmp_path, failure):
    project = tmp_path / "project"
    if failure != "working directory":
        project.mkdir()
    if failure == "log path":
        snapshots = project / "snapshots"
        snapshots.mkdir()
        (snapshots / "homelab-probe.log").mkdir()
        message = "hlp failed: snapshots/homelab-probe.log is not writable\n"
    elif failure == "snapshots directory":
        message = "hlp failed: snapshots/homelab-probe.log is not writable\n"
    else:
        message = f"hlp failed: cannot enter {project}\n"
    done = run_cron(cron_command(project, tmp_path), cron_path(tmp_path))
    assert done.returncode == EXIT_ERROR
    assert done.stdout == message
    assert done.stderr == ""


def test_the_cron_line_is_wrapped_in_the_lock_the_page_describes():
    assert crontab_line().startswith(f"flock -n {LOCK} /bin/sh -c '")
    assert "macOS has no `flock`" in section("cron")             # why the tests below do not rely on the system's


@pytest.mark.skipif(shutil.which("flock") is None, reason="needs util-linux flock (not on macOS)")
def test_a_second_run_that_finds_the_lock_held_exits_at_once_and_runs_nothing(tmp_path):
    """The one test of the real lock: the first run is still going when the second starts."""
    started, ran = tmp_path / "started", tmp_path / "ran"
    stub = tmp_path / "venv" / "bin" / "hlp"
    stub.parent.mkdir(parents=True)
    (tmp_path / "snapshots").mkdir()
    stub.write_text(f"#!/bin/sh\necho x >> {ran}\n: > {started}\nsleep 3\nexit 0\n")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    command = cron_command(tmp_path, tmp_path)
    path = cron_path(tmp_path, real_flock=True)
    first = subprocess.Popen(["/bin/sh", "-c", command], env={"PATH": path}, stdout=subprocess.PIPE, text=True)
    try:
        for _ in range(100):
            if started.exists():
                break
            time.sleep(0.05)
        assert started.exists(), "the first run never started"
        second = run_cron(command, path)
        assert second.returncode == 1 and second.stdout == "" and second.stderr == ""
    finally:
        first.communicate(timeout=30)
    assert first.returncode == 0 and ran.read_text() == "x\n"            # the second never reached the tool


def test_the_cron_line_threshold_is_the_first_exit_code_that_is_an_error():
    assert f'[ "$rc" -lt {EXIT_ERROR} ]' in crontab_line()
    assert max(FINDINGS) < EXIT_ERROR and EXIT_ERROR not in FINDINGS


def test_the_cron_line_runs_the_documented_command_with_notifications():
    args = parse_cli(shlex.split(re.search(r"hlp (diagnose[^>;]*)", crontab_line()).group(1)))
    assert args.command == "diagnose" and args.notify is True


# -- systemd -------------------------------------------------------------------------------------------------------

def test_the_service_runs_the_installed_command_from_the_project_directory():
    service = unit(blocks("ini")[0])
    assert service.get("Service", "Type") == "oneshot"
    directory = service.get("Service", "WorkingDirectory")
    command = shlex.split(service.get("Service", "ExecStart"))
    assert directory == INSTALL_DIR and command[0] == f"{directory}/venv/bin/hlp"
    assert parse_cli(command[1:]).notify is True
    assert service.get("Unit", "After") == "network-online.target" == service.get("Unit", "Wants")


def test_the_service_treats_exactly_the_finding_exit_codes_as_success():
    service = unit(blocks("ini")[0])
    success = {int(code) for code in service.get("Service", "SuccessExitStatus").split()}
    assert success == FINDINGS                                   # an error (3), a usage error (64) must still fail it
    assert not success & {0, EXIT_ERROR, 4, 64}


def test_the_timer_is_installed_into_timers_target_and_names_its_interval():
    timer = unit(blocks("ini")[1])
    assert timer.get("Install", "WantedBy") == "timers.target"
    assert f"{INTERVAL_MINUTES} minutes" in timer.get("Unit", "Description")
    assert timer.get("Timer", "Persistent") == "true"
    assert {"OnCalendar", "RandomizedDelaySec", "Persistent"} <= set(timer["Timer"])


def test_the_systemd_commands_name_the_files_the_page_saves():
    commands = blocks("bash", section("systemd timer"))[0]
    for name in ("homelab-probe.service", "homelab-probe.timer"):
        assert name in commands and f"`{name}`" in section("systemd timer")


def test_the_docker_run_in_the_unit_example_matches_the_docker_section():
    docker = section("Docker")
    unit_line = re.search(r"ExecStart=(/usr/bin/docker run [^`]*)", docker).group(1)
    run_lines = [line for line in blocks("bash", docker)[0].splitlines() if line.startswith("docker run")]
    assert unit_line.split()[:3] == ["/usr/bin/docker", "run", "--rm"]
    assert "-v homelab-probe-state:/data/snapshots" in unit_line
    assert all("-v homelab-probe-state:/data/snapshots" in line for line in run_lines)


# -- launchd -------------------------------------------------------------------------------------------------------

def test_the_plist_is_valid_and_runs_the_documented_command():
    plist = plistlib.loads(blocks("xml")[0].encode())
    program, *arguments = plist["ProgramArguments"]
    assert program.endswith("/venv/bin/hlp") and parse_cli(arguments).notify is True
    assert plist["WorkingDirectory"] == program[: -len("/venv/bin/hlp")]
    assert plist["StandardErrorPath"].startswith(plist["WorkingDirectory"] + "/snapshots/")
    assert plist["StandardOutPath"] == "/dev/null"
    assert set(plist) == {"Label", "ProgramArguments", "WorkingDirectory", "StartInterval", "StandardOutPath",
                          "StandardErrorPath", "ProcessType"}


def test_the_launchctl_commands_use_the_plists_label_and_file_name():
    plist = plistlib.loads(blocks("xml")[0].encode())
    commands = blocks("bash", section("launchd (macOS)"))[0]
    assert f"{plist['Label']}.plist" in commands
    labels = re.findall(r"gui/\$\(id -u\)/(\S+)", commands)
    assert labels and set(labels) == {plist["Label"]}


# -- Docker --------------------------------------------------------------------------------------------------------

def instructions():
    (block,) = blocks("dockerfile")
    joined = re.sub(r"\\\n\s*", " ", "\n".join(line for line in block.splitlines() if not line.startswith("#")))
    return [tuple(line.strip().split(None, 1)) for line in joined.splitlines() if line.strip()]


def test_the_dockerfile_installs_only_what_the_package_needs_and_runs_as_a_normal_user():
    steps = instructions()
    names = [name for name, _ in steps]
    assert names[0] == "FROM" and names.count("USER") == 1 and names.index("USER") < names.index("ENTRYPOINT")
    copied = [arguments.split() for name, arguments in steps if name == "COPY"]
    for *sources, _target in copied:
        for source in sources:
            assert (ROOT / source).exists(), source
    assert [c[:-1] for c in copied] == [["pyproject.toml", "README.md"], ["homelab_probe"]]
    user = dict(steps)["USER"]
    assert user not in ("root", "0")
    assert re.search(rf"useradd .*--uid 10001 .*\b{user}\b", " ".join(a for n, a in steps if n == "RUN"))
    assert "uid 10001" in section("Docker")
    assert "COPY . " not in TEXT                                  # the key and the saved inventories stay out


def test_the_image_uses_a_python_the_project_supports():
    base = dict(instructions())["FROM"]
    version = tuple(int(n) for n in re.fullmatch(r"python:(\d+)\.(\d+)-slim", base).groups())
    assert version >= (3, 10)
    assert 'requires-python = ">=3.10"' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def test_the_entry_point_and_default_command_are_real():
    steps = dict(instructions())
    assert json.loads(steps["ENTRYPOINT"]) == ["hlp"]
    assert 'hlp = "homelab_probe.cli:main"' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    args = parse_cli(json.loads(steps["CMD"]))
    assert args.command == "diagnose" and args.notify is True and args.fail_on == "critical"


def test_the_volume_is_where_the_program_keeps_its_notification_state():
    """The image's last WORKDIR plus the program's relative default is the directory the volume must cover."""
    steps = instructions()
    workdir = [arguments for name, arguments in steps if name == "WORKDIR"][-1]
    state = f"{workdir}/{notify.state_path_for({'id': 'site-1'})}"
    mount = re.search(r"-v homelab-probe-state:(\S+)", section("Docker")).group(1)
    assert state.startswith(mount + "/")
    assert re.search(rf"mkdir -p {re.escape(mount)}\b", " ".join(a for n, a in steps if n == "RUN"))


def test_the_dockerignore_advice_names_what_must_stay_out():
    docker = section("Docker")
    for name in (".env", "snapshots/", ".venv/"):
        assert f"`{name}`" in docker
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "snapshots/" in gitignore and ".venv/" in gitignore and "*.env" in gitignore


def test_the_first_run_and_the_scheduled_run_are_different_commands():
    runs = [line for line in blocks("bash", section("Docker"))[0].splitlines() if line.startswith("docker run")]
    assert len(runs) == 2 and "--notify-baseline" in runs[0] and "--notify-baseline" not in runs[1]
