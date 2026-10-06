"""The container image's files (issue #246), checked without Docker.

Building and running the image needs a daemon, so that is `tools/docker_smoke.sh` (run by the `docker` job of CI and by
hand for the pull request). What can be proved from the files is proved here: the default command is one the program
accepts (a wildcard bind needs an allowed host), the user is not root, nothing in the build context or the image can
hold a secret, compose hardens the container and has no setup token, the workflow builds without publishing, and
every `docker run` the documentation shows names a real command.
"""

import json
import os
import re
import shlex
import shutil
import subprocess

import pytest
import yaml
from docs_support import README, ROOT, local_links

from homelab_probe import __version__, cli
from homelab_probe.config import parse_log_format
from homelab_probe.util import check_bind, parse_allowed_host, parse_forwarded_ips

DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
COMPOSE_TEXT = (ROOT / "compose.yaml").read_text(encoding="utf-8")
COMPOSE = yaml.safe_load(COMPOSE_TEXT)
DOCKERIGNORE = (ROOT / ".dockerignore").read_text(encoding="utf-8")
DOCKER_PAGE = ROOT / "docs" / "docker.md"
DOCS = DOCKER_PAGE.read_text(encoding="utf-8")
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
SMOKE = ROOT / "tools" / "docker_smoke.sh"


def instructions(text=DOCKERFILE):
    """[(INSTRUCTION, arguments)] with continuation lines joined and comments removed."""
    joined = re.sub(r"\\\n\s*", " ", "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#")))
    return [tuple(line.strip().split(None, 1)) for line in joined.splitlines() if line.strip()]


def stages():
    """[[(INSTRUCTION, arguments)]] one list per FROM."""
    result = []
    for step in instructions():
        if step[0] == "FROM":
            result.append([])
        result[-1].append(step)
    return result


def final_stage():
    return dict((name, arguments) for name, arguments in stages()[-1])


def json_form(arguments):

    return json.loads(arguments)


def parse_cli(arguments):
    args = cli.build_parser().parse_args(arguments)
    return args


# -- the Dockerfile ------------------------------------------------------------------------------------------------

def test_the_first_stage_is_a_marked_placeholder_that_leaves_its_output_in_out():
    first = stages()[0]
    assert first[0] == ("FROM", "scratch AS web") and ("WORKDIR", "/out") in first
    assert "PLACEHOLDER" in DOCKERFILE and "only this stage is replaced" in DOCKERFILE
    build = stages()[1]
    assert ("COPY", "--from=web /out /web-out") in build         # the only place that reads the web stage
    copy_if_any = [arguments for name, arguments in build if name == "RUN" and "/web-out" in arguments]
    assert copy_if_any and "ls -A /web-out" in copy_if_any[0] and "homelab_probe/web" in copy_if_any[0]


def test_the_base_image_is_a_python_the_project_supports_in_both_python_stages():
    bases = [step[1].split()[0] for stage in stages()[1:] for step in stage[:1]]
    assert len(bases) == 2 and bases[0] == bases[1]
    version = tuple(int(n) for n in re.fullmatch(r"python:(\d+)\.(\d+)-slim", bases[0]).groups())
    assert version >= (3, 10)
    assert 'requires-python = ">=3.10"' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def test_only_named_files_are_copied_and_each_exists():
    for stage in stages():
        for name, arguments in stage:
            assert name != "ADD"
            if name != "COPY":
                continue
            words = arguments.split()
            if words[0].startswith("--from="):
                continue
            *sources, _target = words
            assert sources not in (["."], ["./"]), "the whole build context must never be copied"
            for source in sources:
                assert (ROOT / source).exists(), source


def test_the_image_installs_from_the_wheels_of_the_build_stage_with_the_web_extra_and_no_index():
    build = stages()[1]
    assert any(n == "RUN" and "pip wheel" in a and '".[web]"' in a for n, a in build)
    last = [a for n, a in stages()[-1] if n == "RUN" and "pip install" in a]
    assert len(last) == 1
    assert "--no-index" in last[0] and "--no-cache-dir" in last[0] and '"homelab-probe[web]"' in last[0]
    assert "from=build" in last[0]                                # the wheels are mounted, not kept in a layer


def test_the_process_is_not_root_and_data_is_where_it_keeps_its_files():
    steps = final_stage()
    stage = stages()[-1]
    assert [n for n, _ in stage].count("USER") == 1
    assert steps["USER"] not in ("root", "0")
    run = " ".join(a for n, a in stage if n == "RUN" and "useradd" in a)
    assert re.search(rf"useradd .*--uid 10001 .*\b{steps['USER']}\b", run) and "chown hlp /data" in run
    assert [n for n, _ in stage].index("USER") < [n for n, _ in stage].index("ENTRYPOINT")
    assert steps["WORKDIR"] == "/data" and steps["VOLUME"] == "/data"
    assert "uid 10001" in DOCS


def test_the_environment_logs_json_to_stderr_and_writes_no_bytecode():
    env = dict(re.findall(r"(\w+)=(\S+)", final_stage()["ENV"]))
    assert env["LOG_FORMAT"] == "json" and parse_log_format("json") == "json"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1" and env["PYTHONUNBUFFERED"] == "1"
    names = {n for n, _ in stages()[-1]}
    assert "ARG" not in names                                      # nothing a build argument could leak
    assert not re.search(r"(?i)(key|token|password|secret)=", " ".join(a for n, a in stages()[-1] if n in ("ENV", "RUN")))


def test_the_entry_point_is_hlp_and_the_default_command_is_one_the_server_accepts():
    steps = final_stage()
    assert json_form(steps["ENTRYPOINT"]) == ["hlp"]
    assert 'hlp = "homelab_probe.cli:main"' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    command = json_form(steps["CMD"])
    args = parse_cli(command)
    assert args.command == "serve" and args.host == "0.0.0.0" and args.scheduler is True
    assert args.allowed_host == ["localhost"]                      # a wildcard bind needs one (util.check_bind)
    assert check_bind(args.host, args.allowed_host) == "0.0.0.0"
    for name in args.allowed_host:
        parse_allowed_host(name)
    with pytest.raises(ValueError):
        check_bind(args.host, [])                                  # and the rule that makes it necessary is real
    assert args.forwarded_allow_ips is None and args.read_only is False


def test_the_health_check_asks_the_servers_default_port_with_python_and_no_curl():
    steps = final_stage()
    assert "curl" not in DOCKERFILE.replace("# ", "").split("HEALTHCHECK", 1)[1].split("\n\n")[0]
    health = steps["HEALTHCHECK"]
    port = parse_cli(["serve"]).port
    assert f"http://127.0.0.1:{port}/healthz" in health and '"python"' in health
    assert steps["EXPOSE"] == str(port)
    assert "/healthz" in (ROOT / "homelab_probe" / "server" / "app.py").read_text(encoding="utf-8")


def test_the_image_carries_the_licence_and_source_labels():
    labels = final_stage()["LABEL"]
    assert "org.opencontainers.image.licenses=\"Apache-2.0\"" in labels
    assert "https://github.com/jeffholst/homelab-probe" in labels


# -- .dockerignore -------------------------------------------------------------------------------------------------

def ignore_lines():
    return [line.strip() for line in DOCKERIGNORE.splitlines() if line.strip() and not line.startswith("#")]


def test_the_build_context_excludes_everything_then_lets_back_in_only_what_is_copied():
    lines = ignore_lines()
    assert lines[0] == "*"
    allowed = [line[1:] for line in lines if line.startswith("!")]
    for stage in stages():
        for name, arguments in stage:
            words = arguments.split()
            if name == "COPY" and not words[0].startswith("--from="):
                for source in words[:-1]:
                    assert any(source == a or source.startswith(a.rstrip("/") + "/") for a in allowed), source
    assert set(allowed) == {"pyproject.toml", "README.md", "homelab_probe", "docs/schemas"}


def test_secrets_and_state_are_excluded_wherever_they_are():
    lines = set(ignore_lines())
    for pattern in ("**/.env", "**/.env.*", "**/*.env", "**/hlp.toml", "**/users.json*", "**/audit.log*",
                    "**/snapshots", "**/certs", "**/.git", "**/.venv", "**/node_modules", "**/__pycache__"):
        assert pattern in lines, pattern
    positions = {line: i for i, line in enumerate(ignore_lines())}
    assert positions["**/.env"] > max(positions[line] for line in lines if line.startswith("!"))   # the last match wins
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for ignored in ("snapshots/", "users.json", "audit.log*", "hlp.toml", "*.env"):
        assert ignored in gitignore                                # what git keeps out is what the image keeps out


# -- compose.yaml --------------------------------------------------------------------------------------------------

def service():
    return COMPOSE["services"]["hlp"]


def test_compose_runs_the_image_hardened_with_the_port_on_loopback_and_one_data_volume():
    s = service()
    assert s["read_only"] is True and s["tmpfs"] == ["/tmp"]
    assert s["cap_drop"] == ["ALL"] and "cap_add" not in s
    assert s["security_opt"] == ["no-new-privileges:true"]
    assert s["ports"] == ["127.0.0.1:8787:8787"]
    assert s["volumes"] == ["hlp-data:/data"] and "hlp-data" in COMPOSE["volumes"]
    assert s["build"] == "." and "privileged" not in s and "network_mode" not in s
    assert s["logging"]["options"] == {"max-size": "10m", "max-file": "5"}


def test_compose_has_no_setup_token_key_or_env_file():
    assert "HLP_SETUP_TOKEN" not in COMPOSE_TEXT
    s = service()
    assert "env_file" not in s
    assert not re.search(r"(?i)api_key|unifi_url|password", " ".join(f"{k}={v}" for k, v in s["environment"].items()))
    assert parse_log_format(s["environment"]["LOG_FORMAT"]) == "json"


def test_compose_command_is_a_complete_command_the_server_accepts():
    args = parse_cli([str(word) for word in service()["command"]])
    assert args.command == "serve" and args.host == "0.0.0.0" and args.scheduler is True
    assert "localhost" in args.allowed_host and check_bind(args.host, args.allowed_host)
    assert args.read_only is False
    published = service()["ports"][0].split(":")
    assert published[0] == "127.0.0.1" and int(published[2]) == args.port


# -- the CI job and the smoke test ---------------------------------------------------------------------------------

def test_ci_builds_and_smoke_tests_the_image_and_publishes_nothing():
    job = CI.split("\n  docker:\n", 1)[1]
    assert "docker build" in job and "tools/docker_smoke.sh" in job
    for forbidden in ("docker push", "docker login", "login-action", "build-push-action", "packages: write", "ghcr.io",
                      "secrets."):
        assert forbidden not in CI, forbidden
    assert "permissions:\n  contents: read" in CI
    assert "pull_request:" in CI                                   # built on pull requests, which is the point


def test_the_smoke_script_is_executable_valid_bash_and_makes_every_check_of_the_issue():
    assert os.access(SMOKE, os.X_OK)
    text = SMOKE.read_text(encoding="utf-8")
    for needle in ("--version", "/healthz", "Health.Status", "--demo query devices", "--demo serve", "needs_setup",
                   "--read-only", "--cap-drop ALL", "no-new-privileges", "setup", "--entrypoint id", "usr/.probe"):
        assert needle in text, needle
    assert "docker push" not in text and "docker login" not in text
    bash = shutil.which("bash")
    assert bash
    subprocess.run([bash, "-n", str(SMOKE)], check=True)


def test_the_version_the_smoke_test_expects_is_where_it_reads_it():
    init = (ROOT / "homelab_probe" / "__init__.py").read_text(encoding="utf-8")
    assert re.search(r'^__version__ = "' + re.escape(__version__) + '"$', init, flags=re.M)
    assert "sed -n 's/^__version__" in SMOKE.read_text(encoding="utf-8")


# -- docs/docker.md ------------------------------------------------------------------------------------------------

def blocks(language):
    return re.findall(rf"^```{language}\n(.*?)^```", DOCS, flags=re.S | re.M)


def docker_runs():
    """Each `docker run ...` line of the page, continuation lines joined."""
    for block in blocks("bash"):
        for line in re.sub(r"\\\n\s*", " ", block).splitlines():
            if line.startswith("docker run"):
                yield line


def test_the_page_is_linked_from_the_readme_and_the_scheduling_page_and_its_links_resolve():
    assert "(docs/docker.md)" in README.read_text(encoding="utf-8")
    assert "(docker.md)" in (ROOT / "docs" / "scheduling.md").read_text(encoding="utf-8")
    for target, anchor, written in local_links(DOCKER_PAGE):
        assert target.exists(), written
        if anchor and target.suffix == ".md":
            from docs_support import anchors
            assert anchor in anchors(target.read_text(encoding="utf-8")), written


def test_every_docker_run_of_the_page_names_a_command_the_program_has():
    seen = 0
    for line in docker_runs():
        if "--entrypoint" in line:                                  # the one-off tar and chown of the volume backup
            continue
        words = shlex.split(line, comments=True)
        if "homelab-probe" not in words or words[-1] == "homelab-probe":
            continue                                                # a plain `docker run ... homelab-probe`: the default
        arguments = words[words.index("homelab-probe") + 1:]
        if arguments != ["--version"]:                              # argparse exits at --version, by design
            assert parse_cli(arguments).command, line
        seen += 1
    assert seen >= 5                                                # --version, --demo diagnose, --demo query, diagnose, serve


def test_the_demo_option_comes_before_the_command_in_every_example():
    for line in docker_runs():
        if " --demo " in line:
            assert re.search(r"homelab-probe --demo \w", line), line


def test_the_documented_server_commands_are_accepted_with_their_allowed_hosts_and_proxies():
    for block in blocks("yaml"):
        data = yaml.safe_load(block)
        command = data.get("command")
        if command:
            args = parse_cli([str(word) for word in command])
            assert check_bind(args.host, args.allowed_host)
            if args.forwarded_allow_ips:
                assert parse_forwarded_ips(args.forwarded_allow_ips) == args.forwarded_allow_ips
    run = next(line for line in docker_runs() if "hlp.example.lan" in line)
    args = parse_cli(shlex.split(run)[shlex.split(run).index("homelab-probe") + 1:])
    assert args.allowed_host == ["localhost", "hlp.example.lan"]


def test_the_page_says_what_the_default_command_is_and_that_the_image_is_not_published():
    default = " ".join(json_form(final_stage()["CMD"])[1:])
    assert f"`serve {default}`" in DOCS
    assert "not published yet" in DOCS
    assert "docker_smoke.sh" in DOCS and "HLP_SETUP_TOKEN" in DOCS


def test_the_page_holds_no_secret_or_real_address():
    assert not re.search(r"(?i)api_key\s*=\s*\S|token\s*=\s*\S", DOCS)
    addresses = set(re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", DOCS))
    assert addresses <= {"127.0.0.1", "0.0.0.0", "172.18.0.0", "172.16.0.0"}, addresses
