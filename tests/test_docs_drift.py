"""The documentation is the specification, so it is tested against the program. The README is the quickstart and
the detail is in docs/ (see tests/test_docs_layout.py); everything below looks at the README and every page there.

* every `hlp ...` example in them parses with the real argument parser;
* every command has a row in the README's Commands table, and every long option is mentioned somewhere;
* the sample output blocks equal what the commands print against the synthetic fixture, wherever they now live
  (compared through golden_support.normalise, so times and padding do not matter).

When a sample is out of date, regenerate it with

    UPDATE_README_SAMPLES=1 uv run pytest tests/test_docs_drift.py

and review the diff; update the golden file first if the change is intended
(UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py).
"""

import argparse
import os
import re
import shlex

import pytest
import record_fixture
from docs_support import README, all_docs_text, doc_paths
from golden_support import CASES, GOLDEN, normalise, run_command

from homelab_probe import cli

LAUNCHERS = re.compile(r"(?:uv run hlp\.py|hlp)\s+(.*)$")
STOP = {"||", "|", ">", ">>", "&&", ";", "2>&1"}

# README sample block -> golden case. A block that starts with a `uv run ...` line keeps that line.
SAMPLES = [
    ("Topology", "topology"),
    ("Wi-Fi", "wifi"),
    ("WAN", "wan"),
    ("Firewall", "firewall"),
    ("Audit", "audit"),
    ("Client view", "client_desktop"),
    ("diagnose", "diagnose"),
    ("new-clients", "new_clients"),
    ("Networks", "query_networks"),
    ("Wi-Fi networks", "query_wlans"),
    ("Event history", "events"),
]
CSV_SAMPLES = [
    ("unifi_clients.csv", "unifi_clients.csv"),
    ("switch_Office Switch.csv", "switch_Office Switch.csv"),
]
COMMAND_DOC_SECTIONS = {
    "export": ("Output files",),
    "query": ("Devices", "Switch ports", "DHCP reservations", "Randomized MAC addresses", "Networks", "Wi-Fi networks",
              "Clients on a network, SSID or access point"),
    "snapshot": ("Snapshots and diff",),
    "diff": ("Snapshots and diff",),
    "topology": ("Topology",),
    "wifi": ("Wi-Fi",),
    "wan": ("WAN",),
    "firewall": ("Firewall",),
    "audit": ("Audit",),
    "events": ("Event history",),
    "client": ("Client view", "Randomized MAC addresses"),
    "new-clients": ("New clients", "Randomized MAC addresses"),
    "diagnose": ("Diagnose", "Notifications"),
    "completion": ("Shell completion",),
    "doctor": ("Checking your setup: `doctor`",),
    "info": (),
}


def read():
    """The README (the Commands table and the usage examples are there)."""
    return README.read_text(encoding="utf-8")


def read_all():
    """The README and every page of docs/, for what may live in either."""
    return all_docs_text()


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
    if not (line.startswith("uv run hlp.py") or line.startswith("hlp ")):
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
        return None                                     # a placeholder such as `hlp <command>`, not an example
    return tokens


def examples():
    seen, found = set(), []
    for line in command_lines(read_all()):
        argv = example_argv(line)
        if argv is not None and tuple(argv) not in seen:
            seen.add(tuple(argv))
            found.append(argv)
    return found


def subcommands(parser):
    action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return action.choices


# -- the examples ---------------------------------------------------------------------------------

def test_the_documentation_has_a_useful_number_of_examples():
    assert len(examples()) >= 30


@pytest.mark.parametrize("argv", examples(), ids=lambda a: " ".join(a))
def test_every_example_parses_with_the_real_parser(argv):
    try:
        cli.build_parser().parse_args(argv)
    except SystemExit as e:
        pytest.fail(f"a documentation example does not parse (exit {e.code}): hlp {' '.join(argv)}")


def test_the_example_extractor_handles_the_shapes_the_readme_uses():
    assert example_argv("uv run hlp.py diagnose --json   # as JSON") == ["diagnose", "--json"]
    assert example_argv("$ hlp --verbose wan --json > wan.json") == ["--verbose", "wan", "--json"]
    assert example_argv("*/15 * * * * cd /path/to/x && uv run hlp.py diagnose --fail-on critical || notify-me"
                        ) == ["diagnose", "--fail-on", "critical"]
    assert example_argv("hlp --env-file lab.env diagnose") == ["--env-file", "lab.env", "diagnose"]
    assert example_argv("hlp <command>") is None
    assert example_argv("hlp.toml settings are read") is None
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


def sections_named(heading):
    """The text under ``heading`` in every page that has it (the README may keep a short stub of a section whose
    detail is in docs/), joined."""
    found = []
    for path in doc_paths():
        try:
            found.append(section_text(path.read_text(encoding="utf-8"), heading))
        except StopIteration:
            continue
    assert found, f"no page has a heading {heading!r}"
    return "\n".join(found)


def section_text(text, heading):
    text = re.sub(r"(?ms)^```[^\n]*\n.*?^```\s*$", "", text)
    lines = text.splitlines()
    start = next(
        i for i, line in enumerate(lines)
        if (match := re.match(r"^(#{1,4}) " + re.escape(heading) + r"$", line))
    )
    level = len(re.match(r"^(#{1,4}) ", lines[start]).group(1))
    end = next(
        (i for i in range(start + 1, len(lines))
         if (match := re.match(r"^(#{1,4}) ", lines[i])) and len(match.group(1)) <= level),
        len(lines),
    )
    return "\n".join(lines[start + 1:end])


def command_documentation(command):
    text = read()                                         # the usage examples are in the README
    usage = next(body for heading, language, body in fenced_blocks(text)
                 if heading == "Usage" and language == "bash")
    usage_examples = []
    parser = cli.build_parser()
    for line in usage.splitlines():
        argv = example_argv(line)
        if argv is not None and parser.parse_args(argv).command == command:
            usage_examples.append(line)
    sections = [sections_named(heading) for heading in COMMAND_DOC_SECTIONS[command]]
    return "\n".join([*usage_examples, *sections])


@pytest.mark.parametrize("command, option", all_long_options(), ids=lambda v: str(v))
def test_every_long_option_is_documented(command, option):
    documentation = read_all() if command is None else command_documentation(command)
    assert re.search(re.escape(option) + r"(?![\w-])", documentation), (
        f"{option} ({command or 'global'}) is not mentioned in the documentation")


def recorder_options():
    """The long options of tools/record_fixture.py, which the Development section of the documentation covers."""
    return {o for action in record_fixture.build_parser()._actions for o in action.option_strings
            if o.startswith("--")} - {"--help"}


@pytest.mark.parametrize("option", sorted(recorder_options()))
def test_every_recorder_option_is_documented(option):
    assert re.search(re.escape(option) + r"(?![\w-])", sections_named("Development")), option


def test_every_option_the_documentation_shows_exists():
    known = {o for _, o in all_long_options()} | {"--help", "--version"} | recorder_options()
    spans = [line for line in re.findall(r"`([^`\n]+)`", read_all()) if line.startswith("--")]
    shown = set(re.findall(r"(?<![\w-])(--[a-z][a-z-]*[a-z])(?![\w*-])", "\n".join(spans)))
    for argv in examples():
        shown |= {token.split("=")[0] for token in argv if token.startswith("--")}
    unknown = {o for o in shown if o not in known}
    assert not unknown, f"the documentation mentions options the program does not have: {sorted(unknown)}"


# -- sample output ------------------------------------------------------------------------------------

def sample_block(text, heading):
    for found_heading, language, body in fenced_blocks(text):
        if found_heading == heading and language == "text":
            return body
    raise AssertionError(f"no text block under the heading {heading!r}")


def csv_sample_block(text, heading):
    for found_heading, language, body in fenced_blocks(text):
        if found_heading == heading and language == "csv":
            return body
    raise AssertionError(f"no CSV block under the heading {heading!r}")


def find_sample(heading):
    """(the page, its text, the block) of the sample under ``heading``, wherever it lives."""
    for path in doc_paths():
        text = path.read_text(encoding="utf-8")
        try:
            return path, text, sample_block(text, heading)
        except AssertionError:
            continue
    raise AssertionError(f"no text block under the heading {heading!r} in the README or docs/")


def find_csv_sample(heading):
    for path in doc_paths():
        text = path.read_text(encoding="utf-8")
        try:
            return path, text, csv_sample_block(text, heading)
        except AssertionError:
            continue
    raise AssertionError(f"no CSV block under the heading {heading!r} in the README or docs/")


def without_command_line(body):
    lines = body.splitlines()
    return "\n".join(lines[1:]) if lines and lines[0].startswith("uv run ") else body


@pytest.mark.parametrize("heading, case", SAMPLES, ids=[case for _, case in SAMPLES])
def test_the_documented_sample_matches_the_real_output(fake_client, heading, case):
    path, text, body = find_sample(heading)
    actual = run_command(fake_client, CASES[case])[1]
    if os.environ.get("UPDATE_README_SAMPLES"):
        first = body.splitlines()[0] + "\n" if body.startswith("uv run ") else ""
        path.write_text(text.replace(body, (first + actual).rstrip("\n")), encoding="utf-8")
        return
    expected = normalise(actual)
    assert normalise(without_command_line(body)) == expected, (
        f"the sample under '{heading}' in {path.name} no longer matches `hlp {' '.join(CASES[case])}`. "
        "Regenerate it with UPDATE_README_SAMPLES=1 uv run pytest tests/test_docs_drift.py and review the diff.")


@pytest.mark.parametrize("heading, filename", CSV_SAMPLES, ids=[filename for _, filename in CSV_SAMPLES])
def test_documented_csv_samples_match_fixture_and_golden(fake_client, tmp_path, heading, filename):
    code, _, _ = run_command(fake_client, ["export", "--include-offline", "-o", str(tmp_path)])
    assert code == 0
    actual = (tmp_path / filename).read_text(encoding="utf-8").rstrip("\n")
    path, text, body = find_csv_sample(heading)
    golden = GOLDEN / filename
    if os.environ.get("UPDATE_README_SAMPLES"):
        path.write_text(text.replace(f"```csv\n{body}\n```", f"```csv\n{actual}\n```"), encoding="utf-8")
        golden.write_text(actual + "\n", encoding="utf-8")
        return
    expected = normalise(actual)
    assert normalise(body) == expected, (
        f"the CSV sample under '{heading}' in {path.name} no longer matches `export --include-offline`. "
        "Regenerate it with UPDATE_README_SAMPLES=1 uv run pytest tests/test_docs_drift.py and review the diff.")
    assert normalise(golden.read_text(encoding="utf-8")) == expected, f"{golden.name} no longer matches the fixture"


def test_every_sample_has_a_golden_file():
    for _, case in SAMPLES:
        assert (GOLDEN / f"{case}.txt").exists(), case
    for _, filename in CSV_SAMPLES:
        assert (GOLDEN / filename).exists(), filename
