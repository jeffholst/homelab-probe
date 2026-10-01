"""The README is the specification, so it is tested against the program.

* every `unifi-sentinel ...` example in it parses with the real argument parser;
* every command has a row in the Commands table, and every long option is mentioned somewhere;
* its sample output blocks equal what the commands print against the synthetic fixture (compared
  through golden_support.normalise, so times and padding do not matter).

When a sample is out of date, regenerate it with

    UPDATE_README_SAMPLES=1 uv run pytest tests/test_docs_drift.py

and review the diff; update the golden file first if the change is intended
(UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py).
"""

import argparse
import os
import re
import shlex
from pathlib import Path

import pytest
from golden_support import CASES, GOLDEN, normalise, run_command

from unifi_sentinel import cli

README = Path(__file__).resolve().parent.parent / "README.md"
LAUNCHERS = re.compile(r"(?:uv run unifi-sentinel\.py|unifi-sentinel)\s+(.*)$")
STOP = {"||", "|", ">", ">>", "&&", ";", "2>&1"}

# README sample block -> golden case. A block that starts with a `uv run ...` line keeps that line.
SAMPLES = [
    ("Topology", "topology"),
    ("Wi-Fi", "wifi"),
    ("WAN", "wan"),
    ("Client view", "client_desktop"),
    ("diagnose", "diagnose"),
    ("new-clients", "new_clients"),
    ("Event history", "events"),
]
# Deliberately not checked: the `diff` sample describes a hypothetical set of changes, and the CSV samples
# come from a different synthetic site.


def read():
    return README.read_text(encoding="utf-8")


def fenced_blocks(text):
    """[(heading, language, body)] for every fenced block, with the heading it sits under."""
    found, heading = [], ""
    for m in re.finditer(r"(?m)^#{2,4} ([^\n]+)\n|^```(\w*)\n(.*?)\n```", text, re.S):
        if m.group(1):
            heading = m.group(1)
        else:
            found.append((heading, m.group(2), m.group(3)))
    return found


def command_lines(text):
    """Candidate lines holding a command: lines of fenced blocks and inline code spans."""
    lines = []
    for _, _, body in fenced_blocks(text):
        lines += body.splitlines()
    lines += re.findall(r"`([^`\n]+)`", text)
    return lines


def example_argv(line):
    line = re.sub(r"^\s*(?:\$ |\*/\d+ \* \* \* \* cd \S+ && )", "", line.strip())
    if not (line.startswith("uv run unifi-sentinel.py") or line.startswith("unifi-sentinel ")):
        return None
    match = LAUNCHERS.match(line)
    if not match:
        return None
    tokens = shlex.split(match.group(1), comments=True)
    for i, token in enumerate(tokens):
        if token in STOP:
            tokens = tokens[:i]
            break
    if any(token.startswith("<") and token.endswith(">") for token in tokens):
        return None                                     # a placeholder such as `unifi-sentinel <command>`, not an example
    return tokens


def examples():
    seen, found = set(), []
    for line in command_lines(read()):
        argv = example_argv(line)
        if argv is not None and tuple(argv) not in seen:
            seen.add(tuple(argv))
            found.append(argv)
    return found


def subcommands(parser):
    action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return action.choices


# -- the examples ---------------------------------------------------------------------------------

def test_the_readme_has_a_useful_number_of_examples():
    assert len(examples()) >= 30


@pytest.mark.parametrize("argv", examples(), ids=lambda a: " ".join(a))
def test_every_example_parses_with_the_real_parser(argv):
    try:
        cli.build_parser().parse_args(argv)
    except SystemExit as e:
        pytest.fail(f"README example does not parse (exit {e.code}): unifi-sentinel {' '.join(argv)}")


def test_the_example_extractor_handles_the_shapes_the_readme_uses():
    assert example_argv("uv run unifi-sentinel.py diagnose --json   # as JSON") == ["diagnose", "--json"]
    assert example_argv("$ unifi-sentinel --verbose wan --json > wan.json") == ["--verbose", "wan", "--json"]
    assert example_argv("*/15 * * * * cd /path/to/x && uv run unifi-sentinel.py diagnose --fail-on critical || notify-me"
                        ) == ["diagnose", "--fail-on", "critical"]
    assert example_argv("unifi-sentinel --env-file lab.env diagnose") == ["--env-file", "lab.env", "diagnose"]
    assert example_argv("unifi-sentinel <command>") is None
    assert example_argv("unifi-sentinel.toml settings are read") is None
    assert example_argv("some other command") is None


# -- commands and flags -----------------------------------------------------------------------------

def test_every_command_has_a_row_in_the_commands_table():
    section = read().split("## Commands", 1)[1].split("\n## ", 1)[0]
    documented = set(re.findall(r"(?m)^\| `([a-z-]+)`", section))
    assert documented == set(subcommands(cli.build_parser())), "the Commands table and the parser disagree"


def all_long_options():
    parser = cli.build_parser()
    options = {(None, o) for a in parser._actions for o in a.option_strings if o.startswith("--")}
    for name, sub in subcommands(parser).items():
        options |= {(name, o) for a in sub._actions for o in a.option_strings if o.startswith("--")}
    return sorted((o for o in options if o[1] != "--help"), key=lambda o: (o[0] or "", o[1]))


@pytest.mark.parametrize("command, option", all_long_options(), ids=lambda v: str(v))
def test_every_long_option_is_documented(command, option):
    assert re.search(re.escape(option) + r"(?![\w-])", read()), (
        f"{option} ({command or 'global'}) is not mentioned in the README")


def test_every_option_the_readme_shows_exists():
    known = {o for _, o in all_long_options()} | {"--help", "--version"}
    spans = [line for line in re.findall(r"`([^`\n]+)`", read()) if line.startswith("--")]
    shown = set(re.findall(r"(?<![\w-])(--[a-z][a-z-]*[a-z])(?![\w*-])", "\n".join(spans)))
    for argv in examples():
        shown |= {token.split("=")[0] for token in argv if token.startswith("--")}
    unknown = {o for o in shown if o not in known}
    assert not unknown, f"the README mentions options the program does not have: {sorted(unknown)}"


# -- sample output ------------------------------------------------------------------------------------

def sample_block(text, heading):
    for found_heading, language, body in fenced_blocks(text):
        if found_heading == heading and language == "text":
            return body
    raise AssertionError(f"no text block under the heading {heading!r}")


def without_command_line(body):
    lines = body.splitlines()
    return "\n".join(lines[1:]) if lines and lines[0].startswith("uv run ") else body


@pytest.mark.parametrize("heading, case", SAMPLES, ids=[case for _, case in SAMPLES])
def test_the_readme_sample_matches_the_real_output(fake_client, heading, case):
    text = read()
    body = sample_block(text, heading)
    actual = run_command(fake_client, CASES[case])[1]
    if os.environ.get("UPDATE_README_SAMPLES"):
        first = body.splitlines()[0] + "\n" if body.startswith("uv run ") else ""
        README.write_text(text.replace(body, (first + actual).rstrip("\n")), encoding="utf-8")
        return
    expected = normalise(actual)
    assert normalise(without_command_line(body)) == expected, (
        f"the README sample under '{heading}' no longer matches `unifi-sentinel {' '.join(CASES[case])}`. "
        "Regenerate it with UPDATE_README_SAMPLES=1 uv run pytest tests/test_docs_drift.py and review the diff.")


def test_every_sample_has_a_golden_file():
    for _, case in SAMPLES:
        assert (GOLDEN / f"{case}.txt").exists(), case
