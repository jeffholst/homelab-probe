"""The community files: SECURITY.md, CONTRIBUTING.md and the issue forms (issue #136).

They are documentation that can rot like any other (a command CI no longer runs, a link that moved, a template that
GitHub would refuse), and two of them tell people what to redact, so a real address in an example would be worse than
a typo. These tests keep them honest.
"""

import re
from urllib.parse import urlparse

import pytest
import yaml
from docs_support import ROOT, anchors, duplicate_headings, headings, local_links
from test_docs_layout import README_LINES

SECURITY = ROOT / "SECURITY.md"
CONTRIBUTING = ROOT / "CONTRIBUTING.md"
TEMPLATES = ROOT / ".github" / "ISSUE_TEMPLATE"
FORMS = sorted(TEMPLATES.glob("*.yml"))
COMMUNITY = [SECURITY, CONTRIBUTING, ROOT / ".github" / "pull_request_template.md"]
REPOSITORY = "https://github.com/jeffholst/homelab-probe"
UPSTREAM = "https://github.com/ericfitz/unifi-clients-export"
ADVISORIES = f"{REPOSITORY}/security/advisories/new"
REPOSITORY_LABELS = {"bug", "enhancement", "documentation"}          # the labels the repository has
FIELD_TYPES = {"markdown", "textarea", "input", "dropdown", "checkboxes"}


def read(path):
    return path.read_text(encoding="utf-8")


def rel(path):
    return path.relative_to(ROOT).as_posix()


def form(name):
    return yaml.safe_load(read(TEMPLATES / name))


# -- the files exist and are reachable -----------------------------------------------------------------------------

def test_the_files_exist():
    for path in (SECURITY, CONTRIBUTING, TEMPLATES / "config.yml", TEMPLATES / "bug_report.yml",
                 TEMPLATES / "feature_request.yml"):
        assert path.is_file(), rel(path)


def test_the_readme_points_to_both_documents():
    links = {target for _, _, target in local_links(ROOT / "README.md")}
    assert {"CONTRIBUTING.md", "SECURITY.md"} <= links


def test_each_document_links_to_the_other_two_places_a_reader_needs():
    security, contributing = read(SECURITY), read(CONTRIBUTING)
    assert "(SECURITY.md)" in contributing and "(CHANGELOG.md)" in security and "(README.md#requirements)" in security
    for needle in ("(CLAUDE.md)", "(docs/development.md#development)", "(.github/pull_request_template.md)",
                   "(LICENSE)"):
        assert needle in contributing, needle


@pytest.mark.parametrize("path", COMMUNITY, ids=rel)
def test_every_relative_link_resolves_to_a_file_and_an_anchor(path):
    broken = []
    for target, anchor, written in local_links(path):
        if not target.exists():
            broken.append(f"{written}: no such file")
        elif anchor and target.suffix == ".md" and anchor not in anchors(read(target)):
            broken.append(f"{written}: no heading makes the anchor #{anchor}")
    assert not broken, "; ".join(broken)


@pytest.mark.parametrize("path", COMMUNITY, ids=rel)
def test_no_two_headings_make_the_same_anchor(path):
    assert duplicate_headings(read(path)) == []


@pytest.mark.parametrize("path", [SECURITY, CONTRIBUTING], ids=rel)
def test_a_backticked_repository_path_exists(path):
    missing = [token for token in re.findall(r"`([\w./-]+\.(?:md|py|yml|json|toml|lock))`", read(path))
               if not (ROOT / token).exists()]
    assert not missing, missing


def test_the_github_links_name_this_repository_or_the_upstream_project_it_credits():
    for path in [SECURITY, CONTRIBUTING, *FORMS]:
        for url in re.findall(r"https://github\.com/[^\s)\"'>]+", read(path)):
            assert url.startswith((REPOSITORY, UPSTREAM)), f"{rel(path)}: {url}"


# -- SECURITY.md ---------------------------------------------------------------------------------------------------

def test_security_says_how_to_report_privately_and_what_not_to_do():
    text = read(SECURITY)
    assert f"({REPOSITORY}/security)" in text and "Report a vulnerability" in text
    assert "do not open a public issue" in text.lower() and "Security contact request" in text
    assert "(not even what the problem is)" in text        # the fallback must not leak the details either


def test_security_has_the_sections_a_reporter_looks_for():
    names = headings(read(SECURITY))
    for wanted in ("Security policy", "Reporting a vulnerability", "Supported versions", "What is in scope",
                   "What is not in scope"):
        assert wanted in names, wanted


def test_security_tells_the_reporter_to_redact_and_never_send_the_key():
    text = read(SECURITY)
    for needle in ("MAC addresses", "IP addresses", "SSIDs", "site IDs", "--verbose", "API key", ".env", "revoke"):
        assert needle in text, needle


def test_security_scope_names_the_guarantees_the_project_makes():
    text = read(SECURITY)
    for needle in ("read-only", "GET", "event-log", "control", "CSV", "redirect", "TLS", "UNIFI_VERIFY_SSL=false",
                   "ALLOW_INSECURE_HTTP=true"):
        assert needle in text, needle


@pytest.mark.parametrize("path", [SECURITY, CONTRIBUTING, *FORMS], ids=rel)
def test_no_personal_address_is_published(path):
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", read(path)), "an email address needs the owner's decision"


# -- the issue forms -----------------------------------------------------------------------------------------------

def problems(doc):
    """What GitHub's issue form schema (and this project) would refuse in one form."""
    found = []
    for key in ("name", "description", "body"):
        if not doc.get(key):
            found.append(f"no {key}")
    ids = []
    for index, item in enumerate(doc.get("body") or []):
        kind = item.get("type")
        if kind not in FIELD_TYPES:
            found.append(f"item {index}: unknown type {kind!r}")
            continue
        attributes = item.get("attributes") or {}
        if kind == "markdown":
            if not attributes.get("value"):
                found.append(f"item {index}: markdown without a value")
            continue
        if not attributes.get("label"):
            found.append(f"item {index} ({kind}): no label")
        if kind == "dropdown" and not attributes.get("options"):
            found.append(f"item {index}: dropdown without options")
        if kind == "checkboxes":
            options = attributes.get("options") or []
            if not options or not all(option.get("label") for option in options):
                found.append(f"item {index}: checkboxes need options with labels")
        ids.append(item.get("id"))
        if item.get("id") is not None and not re.fullmatch(r"[A-Za-z0-9_-]+", str(item["id"])):
            found.append(f"item {index}: bad id")
    named = [i for i in ids if i]
    if len(named) != len(set(named)):
        found.append("duplicate ids")
    if not set(doc.get("labels") or []) <= REPOSITORY_LABELS:
        found.append(f"labels the repository does not have: {sorted(set(doc.get('labels') or []) - REPOSITORY_LABELS)}")
    return found


@pytest.mark.parametrize("name", ["bug_report.yml", "feature_request.yml"])
def test_a_form_is_one_github_accepts(name):
    assert problems(form(name)) == []


def test_the_form_checker_can_fail():
    assert problems({"name": "x"}) == ["no description", "no body"]
    broken = {"name": "n", "description": "d", "labels": ["wontfix-ish"], "body": [
        {"type": "banner", "attributes": {}},
        {"type": "textarea", "id": "a", "attributes": {}},
        {"type": "dropdown", "id": "a", "attributes": {"label": "L"}},
        {"type": "checkboxes", "attributes": {"label": "L", "options": [{}]}},
        {"type": "markdown", "attributes": {}},
    ]}
    found = " | ".join(problems(broken))
    for needle in ("unknown type", "no label", "dropdown without options", "checkboxes need options",
                   "markdown without a value", "duplicate ids", "labels the repository"):
        assert needle in found, needle


def test_the_forms_label_what_they_are_and_the_labels_exist():
    assert form("bug_report.yml")["labels"] == ["bug"] and form("feature_request.yml")["labels"] == ["enhancement"]


def test_the_bug_form_asks_for_what_a_report_needs():
    fields = {item["id"]: item for item in form("bug_report.yml")["body"] if item["type"] != "markdown"}
    for needed in ("what-happened", "command", "version", "controller", "install"):
        assert fields[needed]["validations"]["required"] is True, needed
    assert "--version" in fields["version"]["attributes"]["description"]
    assert "10.6.106" in fields["controller"]["attributes"]["description"]       # the only tested version
    assert "--verbose" in fields["output"]["attributes"]["description"] and fields["output"]["attributes"]["render"] == "text"


@pytest.mark.parametrize("name", ["bug_report.yml", "feature_request.yml"])
def test_both_forms_make_redaction_a_required_checkbox_and_say_what_to_remove(name):
    doc = form(name)
    (boxes,) = [item for item in doc["body"] if item["type"] == "checkboxes"]
    redaction = [option for option in boxes["attributes"]["options"] if "removed" in option["label"]]
    assert redaction and all(option["required"] is True for option in redaction)
    intro = " ".join(item["attributes"]["value"] for item in doc["body"] if item["type"] == "markdown")
    for needle in ("MAC address", "IP address", "API key"):
        assert needle in intro, needle


def test_the_bug_form_sends_security_problems_elsewhere():
    doc = form("bug_report.yml")
    text = yaml.safe_dump(doc)
    assert "SECURITY.md" in text
    (boxes,) = [item for item in doc["body"] if item["type"] == "checkboxes"]
    assert any("not a security problem" in o["label"] and o["required"] for o in boxes["attributes"]["options"])


def test_the_feature_form_states_the_read_only_rule():
    doc = form("feature_request.yml")
    intro = " ".join(i["attributes"]["value"] for i in doc["body"] if i["type"] == "markdown")
    assert "read-only" in intro and "GET" in intro
    (boxes,) = [item for item in doc["body"] if item["type"] == "checkboxes"]
    assert any("read-only" in o["label"] and o["required"] for o in boxes["attributes"]["options"])


def test_the_chooser_config_links_the_private_advisory_form_and_the_troubleshooting_page():
    config = form("config.yml")
    assert config["blank_issues_enabled"] is True
    links = {link["name"]: link for link in config["contact_links"]}
    assert links["Report a security vulnerability"]["url"] == ADVISORIES
    assert "SECURITY.md" in links["Report a security vulnerability"]["about"]
    troubleshooting = urlparse(links["Troubleshooting"]["url"])
    assert troubleshooting.path == "/jeffholst/homelab-probe/blob/main/docs/configuration.md"
    assert troubleshooting.fragment in anchors(read(ROOT / "docs" / "configuration.md"))
    assert all(link["name"] and link["about"] for link in links.values())
    assert set(config) == {"blank_issues_enabled", "contact_links"}


# -- CONTRIBUTING.md stays true to the repository ------------------------------------------------------------------

def test_the_commands_contributing_names_are_the_ones_ci_runs():
    contributing, ci = read(CONTRIBUTING), read(ROOT / ".github" / "workflows" / "ci.yml")
    block = contributing.split("```bash", 1)[1].split("```", 1)[0]
    commands = [line.split("#", 1)[0].strip() for line in block.splitlines() if line.startswith("uv ")]
    assert commands == [
        "uv sync --locked --group dev --extra web --extra pretty",
        "uv run --extra web --extra pretty python -m pytest",
        "uv run --extra web --extra pretty ruff check .",
        "uv run --extra web --extra pretty python -m mypy",
        "uv lock --check",
    ]
    # CI installs the same extras first; local commands keep them explicit on every run.
    for command in ("uv sync --locked --extra web --extra pretty", "uv run pytest",
                    "uv run ruff check .", "uv run mypy", "uv lock --check"):
        assert command in ci, f"CI does not run {command!r}"


def test_the_numbers_and_rules_it_states_match_the_project():
    contributing = read(CONTRIBUTING)
    assert f"under {README_LINES} lines" in contributing
    assert "Python 3.10 or newer" in contributing and 'requires-python = ">=3.10"' in read(ROOT / "pyproject.toml")
    assert "3.10 to 3.13" in contributing
    for variable in ("UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py",
                     "UPDATE_README_SAMPLES=1 uv run pytest tests/test_docs_drift.py"):
        assert variable in contributing


def test_the_rules_it_repeats_are_the_rules_of_claude_md():
    claude, contributing = read(ROOT / "CLAUDE.md"), read(CONTRIBUTING)
    for rule in ("normalize_mac", "never rename or reuse", "homelab_probe/demo/controller.json"):
        assert rule in claude, f"CLAUDE.md no longer says {rule!r}"
    assert "normalize_mac" in contributing and "never renamed or reused" in contributing
    assert "homelab_probe/demo/controller.json" in contributing


# -- no real data in anything that tells people to remove it -----------------------------------------------------------

DOCUMENTATION_ADDRESSES = ("192.0.2.", "198.51.100.", "203.0.113.")


def real_looking(text):
    """MACs other than the made-up one, and IPv4 addresses outside the documentation ranges."""
    macs = {m.lower() for m in re.findall(r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b", text)} - {"aa:bb:cc:dd:ee:ff"}
    ips = [ip for ip in re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text) if not ip.startswith(DOCUMENTATION_ADDRESSES)]
    return sorted(macs), ips


@pytest.mark.parametrize("path", [SECURITY, CONTRIBUTING, *FORMS], ids=rel)
def test_examples_use_only_made_up_addresses(path):
    assert real_looking(read(path)) == ([], [])


def test_the_address_check_can_fail():
    text = "aa:bb:cc:dd:ee:ff 192.0.2.10 203.0.113.7 192.168.1.1 10.0.0.5 00:11:22:33:44:55"
    assert real_looking(text) == (["00:11:22:33:44:55"], ["192.168.1.1", "10.0.0.5"])
