"""The documentation layout: a short README (the quickstart) and the detail in docs/.

The README keeps what a new user needs: what the tool is, install, configure, the Commands table, one example per
command and the exit codes. Everything else lives in ``docs/`` and the README points to it. These tests keep the move
honest: the README stays short, every link resolves, every old anchor still exists somewhere, and no page is orphaned.
"""

import re

import pytest
from docs_support import DOCS, README, ROOT, anchors, doc_paths, duplicate_headings, headings, local_links

from unifi_sentinel.commands import COMMANDS

README_LINES = 250          # "well under 250" was the target of the issue; this is the ceiling

# Every anchor the README had before the split (GitHub's slugs). Issues, pull requests and bookmarks may use them, so
# each one must still exist at its original README URL.
OLD_ANCHORS = (
    "unifi-sentinel",
    "credits",
    "commands",
    "features",
    "requirements",
    "installation",
    "configure",
    "seeing-what-the-tool-does---verbose",
    "getting-an-api-key",
    "install-dependencies",
    "usage",
    "diagnose",
    "controller-health",
    "port-health",
    "recent-events",
    "wi-fi-quality",
    "configuration-thresholds-and-ignore-list",
    "json-output-and-finding-codes",
    "notifications",
    "exit-codes",
    "devices",
    "snapshots-and-diff",
    "topology",
    "wi-fi",
    "wan",
    "audit",
    "firewall",
    "event-history",
    "the-one-post-and-why-it-is-safe",
    "client-view",
    "new-clients",
    "randomized-mac-addresses",
    "switch-ports",
    "dhcp-reservations",
    "output-files",
    "names-in-exports-and-output",
    "example-output",
    "unificlientscsv",
    "switchswitch---dencsv",
    "api-documentation",
    "troubleshooting",
    "development",
    "license",
    "contributing",
    "acknowledgments",
)


def read(path):
    return path.read_text(encoding="utf-8")


def test_the_readme_stays_a_quickstart():
    assert len(read(README).splitlines()) <= README_LINES


def test_the_detail_is_in_pages_under_docs_and_each_starts_with_a_title():
    pages = sorted(DOCS.glob("*.md"))
    assert {p.name for p in pages} >= {"configuration.md", "diagnose.md", "inventory.md", "network.md",
                                        "notifications.md", "development.md"}
    for page in pages:
        assert read(page).startswith("# "), page.name


def test_every_page_is_linked_from_the_readme():
    readme = read(README)
    for page in sorted(DOCS.glob("*.md")):
        assert f"docs/{page.name}" in readme, f"{page.name} is not linked from the README"


def test_every_command_row_links_to_a_page_that_exists():
    table = read(README).split("## Commands", 1)[1].split("\n## ", 1)[0]
    for command in COMMANDS:
        row = next((line for line in table.splitlines() if line.startswith(f"| `{command.name}`")), "")
        assert row, command.name
        if command.name != "info":
            link = re.search(r"\]\((docs/[\w.-]+\.md)(?:#[\w-]+)?\)", row)
            assert link and (ROOT / link.group(1)).is_file(), f"the {command.name} row has no link to a docs page"


def test_every_command_has_an_example_in_the_readme_itself():
    readme = read(README)
    for command in COMMANDS:
        assert re.search(rf"unifi-sentinel\.py (?:--[\w-]+(?: \S+)? )*{re.escape(command.name)}\b", readme), command.name


# -- links and anchors -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("path", doc_paths(), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_every_relative_link_resolves_to_a_file_and_an_anchor(path):
    broken = []
    for target, anchor, written in local_links(path):
        if not target.exists():
            broken.append(f"{written}: no such file")
        elif anchor and target.suffix == ".md" and anchor not in anchors(read(target)):
            broken.append(f"{written}: no heading makes the anchor #{anchor}")
    assert not broken, f"{path.name}: " + "; ".join(broken)


def test_the_link_checker_sees_a_broken_link_and_ignores_code_and_external_links(tmp_path):
    page = tmp_path / "p.md"
    page.write_text("# T\n\n[ok](#t) [gone](#nope) [file](missing.md) [web](https://example.com/#x)\n"
                    "`[code](#nope2)`\n\n```\n[block](#nope3)\n```\n")
    assert [written for _, _, written in local_links(page)] == ["#t", "#nope", "missing.md"]


def test_every_anchor_the_readme_used_to_have_still_exists_in_the_readme():
    available = anchors(read(README))
    lost = [anchor for anchor in OLD_ANCHORS if anchor not in available]
    assert not lost, f"these README anchors were not preserved at their original URLs: {lost}"


@pytest.mark.parametrize("path", doc_paths(), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_no_page_has_two_headings_with_the_same_anchor(path):
    assert duplicate_headings(read(path)) == []


def test_the_heading_helpers_follow_githubs_rules():
    assert headings("# A\n```\n# not a heading\n```\n## B `c`\n") == ["A", "B `c`"]
    from docs_support import slug
    assert slug("Seeing what the tool does: `--verbose`") == "seeing-what-the-tool-does---verbose"
    assert slug("Wi-Fi") == "wi-fi" and slug("DHCP reservations") == "dhcp-reservations"
    assert anchors("# A\n## A\n## A\n") == {"a", "a-1", "a-2"}
