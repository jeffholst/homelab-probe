"""The documentation bundle builder (tools/build_docs.py): the real docs, and crafted bad input in a temporary repository."""

import filecmp
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import build_docs
import pytest
from build_docs import BuildError, build_bundle, main, slugify, write_bundle
from docs_support import DOCS, README, ROOT, anchors, doc_paths, local_links

TAG = "v1.2.3"
VERSION = "1.2.3"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16
REAL_VERSION = build_docs.read_version(ROOT)
REAL_TAG = f"v{REAL_VERSION}"


# -- a throwaway repository ------------------------------------------------------------------------------------------------

def make_repo(tmp_path, readme="# Home\n", pages=None, files=None, version=VERSION):
    """A repository: a package version, README.md, docs/*.md (``pages``) and other files (``files``)."""
    root = tmp_path / "repo"
    (root / "homelab_probe").mkdir(parents=True)
    if version is not None:
        (root / "homelab_probe" / "__init__.py").write_text(f'__version__ = "{version}"\n')
    (root / "README.md").write_text(readme, encoding="utf-8")
    for name, content in (pages or {}).items():
        (root / "docs").mkdir(exist_ok=True)
        (root / "docs" / name).write_text(content, encoding="utf-8")
    for name, content in (files or {}).items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content if isinstance(content, bytes) else content.encode())
    return root


def problems_of(root, tag=TAG, **kwargs):
    with pytest.raises(BuildError) as raised:
        build_bundle(root, tag, **kwargs)
    return raised.value.problems


def bundle_of(root, tag=TAG):
    return build_bundle(root, tag).data


def page_of(data, page_id):
    return next(page for page in data["pages"] if page["id"] == page_id)


def readme_problems(tmp_path, text, **kwargs):
    return problems_of(make_repo(tmp_path, readme=text, **kwargs))


# -- the repository's own documentation ------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def real(tmp_path_factory):
    out = tmp_path_factory.mktemp("bundle")
    bundle = build_bundle(ROOT, REAL_TAG)
    write_bundle(bundle, out, ROOT)
    return bundle.data, out


def test_every_source_page_is_in_the_bundle_in_a_group(real):
    data, _ = real
    expected = {"readme"} | {path.stem for path in DOCS.glob("*.md")}
    assert {page["id"] for page in data["pages"]} == expected
    in_groups = [page for group in data["groups"] for page in group["pages"]]
    assert sorted(in_groups) == sorted(expected)
    groups = {group["id"]: group["pages"] for group in data["groups"]}
    assert all(page["id"] in groups[page["group"]] for page in data["pages"])
    assert [page["id"] for page in data["pages"]] == in_groups
    assert page_of(data, "readme")["source"] == "README.md"
    assert all(page["title"] and page["markdown"].startswith("# ") for page in data["pages"])


def test_the_groups_follow_the_readmes_documentation_paragraph_and_name_real_pages():
    paragraph = README.read_text(encoding="utf-8").split("\n## Documentation\n", 1)[1].split("\n## ", 1)[0]
    in_readme = [Path(name).stem for name in re.findall(r"\]\(docs/([\w.-]+\.md)\)", paragraph)]
    flat = [page for _, _, ids in build_docs.GROUPS for page in ids]
    assert [page for page in flat if page in in_readme] == in_readme
    assert set(flat) - {"readme"} <= {path.stem for path in DOCS.glob("*.md")}
    assert len(flat) == len(set(flat))


def test_a_page_that_is_in_no_group_goes_to_a_last_group(tmp_path):
    data = bundle_of(make_repo(tmp_path, pages={"extra.md": "# Extra\n", "configuration.md": "# Config\n"}))
    assert [(g["id"], g["pages"]) for g in data["groups"]] == [
        ("overview", ["readme"]), ("using", ["configuration"]), ("more", ["extra"])]
    assert [page["group"] for page in data["pages"]] == ["overview", "using", "more"]


def test_every_internal_link_names_a_page_and_an_anchor_that_exist(real):
    data, _ = real
    pages = {page["id"]: page for page in data["pages"]}
    seen = 0
    for page in data["pages"]:
        for link in page["links"]:
            if link["kind"] == "page":
                seen += 1
                assert link["page"] in pages
                assert not link["anchor"] or link["anchor"] in pages[link["page"]]["anchors"], (page["id"], link)
    assert seen > 40


def test_the_links_and_anchors_agree_with_the_regex_checker_the_other_tests_use(real):
    """docs_support finds relative links and anchors with regular expressions; the parser must find the same ones."""
    data, _ = real
    for path in doc_paths():
        page = next(p for p in data["pages"] if p["source"] == path.relative_to(ROOT).as_posix())
        regex_links = [written for _, _, written in local_links(path)]
        parsed = [link["target"] for link in page["links"] if link["kind"] != "external"]
        assert sorted(regex_links) == sorted(parsed), path.name
        assert set(page["anchors"]) == anchors(path.read_text(encoding="utf-8")), path.name


def test_a_known_finding_code_is_found_in_the_search_text(real):
    data, _ = real
    assert "device.offline" in page_of(data, "diagnose")["search"]
    assert "firewall.forward_duplicate" in page_of(data, "network")["search"]        # a table cell
    assert "uv run hlp.py diagnose" in page_of(data, "readme")["search"]              # a fenced block
    assert "**" not in page_of(data, "readme")["search"] and "](" not in page_of(data, "readme")["search"]


def test_the_output_is_the_same_every_time(tmp_path):
    first, second = tmp_path / "one", tmp_path / "two"
    assert main([str(first), "--tag", REAL_TAG]) == 0
    assert main([str(second), "--tag", REAL_TAG]) == 0
    assert main([str(second), "--tag", REAL_TAG]) == 0              # a rebuild in place
    comparison = filecmp.dircmp(first, second)
    assert not (comparison.left_only or comparison.right_only or comparison.diff_files or comparison.funny_files)
    assert (first / "docs.json").read_bytes() == (second / "docs.json").read_bytes()
    assert (first / "docs.json").read_text(encoding="utf-8").endswith("}\n")
    leftovers = [name for _, _, names in os.walk(second) for name in names if name.endswith(".tmp")]
    assert not leftovers


def test_it_records_the_version_the_tag_and_where_repository_links_point(real):
    data, _ = real
    assert data["version"] == REAL_VERSION and data["tag"] == REAL_TAG and data["format"] == build_docs.FORMAT
    assert list(data)[:2] == ["version", "format"]
    readme = page_of(data, "readme")
    changelog = next(link for link in readme["links"] if link["target"] == "CHANGELOG.md")
    assert changelog["kind"] == "repository"
    assert changelog["href"] == f"{build_docs.REPOSITORY}/blob/{REAL_TAG}/CHANGELOG.md"
    folder = next(link for link in page_of(data, "schemas")["links"] if link["target"] == "schemas/")
    assert folder["href"] == f"{build_docs.REPOSITORY}/tree/{REAL_TAG}/docs/schemas"


def test_images_and_schemas_are_copied_with_their_checksums(real):
    data, out = real
    assert [image["path"] for image in data["images"]] == ["docs/images/homelab-probe.png"]
    names = sorted(path.name for path in (ROOT / "docs" / "schemas").glob("*.json"))
    assert [schema["path"] for schema in data["schemas"]] == [f"docs/schemas/{name}" for name in names]
    for entry in [*data["images"], *data["schemas"]]:
        original = ROOT / entry["path"]
        assert (out / entry["path"]).read_bytes() == original.read_bytes()
        assert entry["size"] == original.stat().st_size and re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
    assert data["schemas"][0]["id"] == "audit.v1" and data["schemas"][0]["title"]
    assert page_of(data, "readme")["images"] == [{"alt": "Homelab Probe", "target": "docs/images/homelab-probe.png",
                                                  "line": 2, "path": "docs/images/homelab-probe.png"}]
    copied = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
    assert copied == sorted(["docs.json", *[e["path"] for e in data["images"] + data["schemas"]]])


def test_schema_links_become_bundled_files(real):
    data, _ = real
    links = [link for link in page_of(data, "schemas")["links"] if link["kind"] == "file"]
    assert len(links) >= 20 and all(link["path"].startswith("docs/schemas/") for link in links)


def test_the_bundled_markdown_has_no_html_and_keeps_the_old_readme_anchors(real):
    data, _ = real
    readme = page_of(data, "readme")
    assert "<a " not in readme["markdown"]
    assert {"unificlientscsv", "credits", "wi-fi"} <= set(readme["anchors"])
    for page in data["pages"]:
        text = re.sub(r"```.*?```", "", page["markdown"], flags=re.S)
        assert not re.search(r"<[A-Za-z/!]", re.sub(r"`[^`\n]*`", "", text)), page["id"]


def test_the_heading_tree_nests_by_level(real):
    data, _ = real
    (top,) = page_of(data, "readme")["headings"]
    assert (top["text"], top["anchor"], top["line"], top["level"]) == ("Homelab Probe", "homelab-probe", 1, 1)
    installation = next(h for h in top["children"] if h["text"] == "Installation")
    assert [(c["level"], c["anchor"]) for c in installation["children"]] == [
        (3, "configure"), (3, "install-dependencies"), (3, "docker")]
    assert next(h for h in top["children"] if h["text"] == "Usage")["children"][0]["text"] == "Exit codes"


def test_the_options_and_the_rules_are_documented_in_the_development_page():
    text = (DOCS / "development.md").read_text(encoding="utf-8")
    assert "tools/build_docs.py" in text
    for action in build_docs.build_parser()._actions:
        for option in action.option_strings:
            if option.startswith("--") and option != "--help":
                assert re.search(re.escape(option) + r"(?![\w-])", text), option
    assert "docs.json" in text


def test_the_tool_runs_as_a_script(tmp_path):
    script = str(ROOT / "tools" / "build_docs.py")
    done = subprocess.run([sys.executable, script, str(tmp_path / "o"), "--tag", REAL_TAG],
                          capture_output=True, text=True, check=False)
    assert done.returncode == 0 and "pages" in done.stdout and (tmp_path / "o" / "docs.json").is_file()
    failed = subprocess.run([sys.executable, script, str(tmp_path / "p"), "--tag", "v0.0.0"],
                            capture_output=True, text=True, check=False)
    assert failed.returncode == 1 and "v0.0.0" in failed.stderr and not (tmp_path / "p").exists()


# -- GitHub's anchors ----------------------------------------------------------------------------------------------------

def test_headings_get_the_anchors_github_gives_them():
    assert slugify("Wi-Fi quality") == "wi-fi-quality"
    text = ("# A\n## A\n## A\n### a-1\n## `Code` *and* **bold** [link](x.md) ![img](i.png)\n"
            "## unifi_clients.csv\n## _it_\n")
    assert build_docs.anchors_of(text) == {"a", "a-1", "a-2", "a-1-1", "code-and-bold-link-img",
                                           "unifi_clientscsv", "it"}


def test_repeated_headings_are_numbered_in_the_page(tmp_path):
    data = bundle_of(make_repo(tmp_path, "# Home\n## Usage\n## Usage\n## Usage\n[x](#usage-2)\n"))
    readme = page_of(data, "readme")
    assert [h["anchor"] for h in readme["headings"][0]["children"]] == ["usage", "usage-1", "usage-2"]
    assert readme["links"][0]["anchor"] == "usage-2"


def test_headings_in_code_and_closing_hashes_are_handled():
    text = "```\n# no\n```\n## Yes ##\n#no heading\n####### seven\n#\n"
    assert build_docs.heading_texts(text) == ["Yes", ""]


# -- what is rejected ----------------------------------------------------------------------------------------------------

def test_raw_html_fails_the_build_and_names_the_file_and_line(tmp_path):
    root = make_repo(tmp_path, pages={"a.md": '# A\n\nfine\n\ntext <div class="x">here</div>\n'})
    assert problems_of(root) == [
        "docs/a.md:5: raw HTML is not supported (</div>); use Markdown or a code span",
        'docs/a.md:5: raw HTML is not supported (<div class="x">); use Markdown or a code span']


@pytest.mark.parametrize("html", [
    "<br>", "<br/>", "</p>", "<details>", "<img src=x>", "<!DOCTYPE html>", "<?php", "<setting name>",
    '<a href="x">y</a>', '<a id="x" class="y"></a>', "<span\n  hidden>", "a <b>c</b>"])
def test_each_kind_of_raw_html_is_refused(tmp_path, html):
    found = [p for p in readme_problems(tmp_path, f"# Home\n\nsome {html} text\n") if "raw HTML" in p]
    assert found and re.match(r"README\.md:[34]:", found[0])


def test_html_in_code_and_a_plain_less_than_are_text(tmp_path):
    text = ("# Home\n\n`<div>` and ``a <b> c`` and 1 < 2 and a <- b and <3\n\n```html\n<div>\n```\n\n"
            '- item\n\n  ```xml\n  <plist version="1.0">\n  ```\n')
    page = page_of(bundle_of(make_repo(tmp_path, text)), "readme")
    assert "<div>" in page["search"] and "<plist" in page["search"] and "1 < 2" in page["search"]


def test_an_angle_bracket_link_is_refused(tmp_path):
    problems = readme_problems(tmp_path, "# Home\n\nsee <https://example.com/x>\n")
    assert problems == ["README.md:3: an angle-bracket link is not supported; write [text](url)"]


def test_comments_and_anchor_tags_are_inert_and_removed(tmp_path):
    text = ('# Home\n\nA <!-- note --> B <a id="old-name"></a>C `<a id="kept"></a>` `<!-- c -->`\n\n'
            '<!--\nmulti\nline\n-->\n\nD\n<!-- one -->\n\n```text\n<!-- code -->\n<a id="code"></a>\n```\n[x](#old-name)\n')
    page = page_of(bundle_of(make_repo(tmp_path, text)), "readme")
    assert page["markdown"] == ('# Home\n\nA  B C `<a id="kept"></a>` `<!-- c -->`\n\n\n\n\n\n\nD\n\n\n'
                                '```text\n<!-- code -->\n<a id="code"></a>\n```\n[x](#old-name)\n')
    assert page["markdown"].count("\n") == text.count("\n")
    assert "old-name" in page["anchors"] and "kept" not in page["anchors"] and "code" not in page["anchors"]
    assert "note" not in page["search"] and page["links"][0]["anchor"] == "old-name"


@pytest.mark.parametrize("text, message", [
    ("# Home\n\nsome <!-- never closed\n", "comment that spans lines must start"),
    ("# Home\n\n<!-- never closed\nmore\n", "never closed"),
    ("# Home\n\n<!-- a\n--> trailing\n", "text after the end"),
])
def test_a_badly_formed_comment_is_refused(tmp_path, text, message):
    assert any(message in p for p in readme_problems(tmp_path, text))


@pytest.mark.parametrize("text, message, line", [
    ("# Home\n\nTitle\n=====\n", "setext heading", 4),
    ("# Home\n\nTitle\n-----\n", "setext heading", 4),
    ("# Home\n\n---\n", "horizontal rule", 3),
    ("# Home\n\n* * *\n", "horizontal rule", 3),
    ("# Home\n\ntext\n***\n", "horizontal rule", 4),
    ("# Home\n\n~~~\ncode\n~~~\n", "~~~ fence", 3),
    ("# Home\n\n    indented code\n", "indented code block", 3),
    ("# Home\n\n[a][b]\n", "reference link", 3),
    ("# Home\n\n[b]: https://example.com\n", "reference link definition", 3),
    ("# Home\n\n[a](<x y.md>)\n", "could not be read", 3),
    ("# Home\n\n[a](x.md\n", "could not be read", 3),
    ("# Home\n\n[a]()\n", "could not be read", 3),
    ('# Home\n\n[a](x "unclosed)\n', "could not be read", 3),
    ("# Home\n\n```\nnever closed\n", "never closed", 3),
    ("# Home\n\n- item\n\t- tab\n", "tab in the indentation", 4),
    ("# Home\n\n- item\ncontinued\n", "must be indented", 4),
    ("# Home\n\n> quote\ncontinued\n", "without >", 4),
    ("# Home\n\n#\n", "empty heading", 3),
    ("# Home\n\n[a [b](x.md)](y.md)\n", "link inside a link", 3),
    ("# Home\n\nzero\u200bwidth\n", "zero-width", 3),
    ("# Home\n\nbidi \u202e text\n", "direction-changing", 3),
    ("# Home\n\n| a | b |\n| - |\n", "columns", 3),
])
def test_constructs_outside_the_subset_fail_with_the_line(tmp_path, text, message, line):
    problems = readme_problems(tmp_path, text)
    found = [p for p in problems if message in p]
    assert found and found[0].startswith(f"README.md:{line}:"), problems


def test_a_page_without_a_title_and_a_non_utf8_page_fail(tmp_path):
    root = make_repo(tmp_path, pages={"a.md": "## Only a subheading\n"})
    (root / "docs" / "b.md").write_bytes(b"# B\n\xff\xfe\n")
    assert problems_of(root) == ["docs/a.md: the page has no '# Title' heading",
                                 "docs/b.md: cannot be read as UTF-8 text (UnicodeDecodeError)"]


def test_a_problem_that_stops_a_page_does_not_stop_the_other_pages(tmp_path):
    root = make_repo(tmp_path, readme="# Home\n\n~~~\n", pages={"a.md": "# A\n\n<div>\n"})
    assert [p.split(":")[0] for p in problems_of(root)] == ["README.md", "docs/a.md"]


def test_every_problem_is_reported_not_only_the_first(tmp_path):
    problems = readme_problems(tmp_path, "# Home\n\n<div>\n\n[a](#nope) ![x](missing.png) [b](gone.md)\n")
    assert len(problems) == 4


def test_all_the_supported_constructs_are_accepted_together(tmp_path):
    text = ('# Home\n\n> A quote with **bold**, *italic*, `code` and [a link](https://example.com/a(b)?x=1 "title").\n\n'
            "1. one\n2. two\n   - nested\n     more\n\n   ```bash\n   run\n   ```\n3) other delimiter\n\n"
            "* star\n+ plus\n\n| A | B \\| C |\n| :-- | --: |\n| `x|y` | z |\n| tail |\n\n"
            "Text 2 * 3 and snake_case and \\*literal\\* and a lone ` tick and [brackets] and ![a](i.png).\n"
            "Hard  \nbreak.\n\n## Section ##\n\n####### not a heading\n\n[end](#section)\n")
    page = page_of(bundle_of(make_repo(tmp_path, text, files={"i.png": PNG})), "readme")
    assert [h["anchor"] for h in page["headings"][0]["children"]] == ["section"]
    assert "A | B | C" in page["search"] and "Text 2 * 3 and snake_case and *literal*" in page["search"]
    assert [link["kind"] for link in page["links"]] == ["external", "page"]
    assert page["links"][0]["href"] == "https://example.com/a(b)?x=1"
    assert page["images"][0]["path"] == "i.png" and "nested more" in page["search"]


def test_a_link_to_a_missing_file_or_anchor_fails(tmp_path):
    root = make_repo(
        tmp_path, readme="# Home\n\n[a](docs/gone.md) [b](docs/a.md#nope) [c](#nope) [d](#) [e](docs/a.md#)\n"
                         "[f](docs/a.md#fine) [g](CHANGELOG.md#nope)\n",
        pages={"a.md": "# A\n\n## Fine\n"}, files={"CHANGELOG.md": "# Changelog\n## [1.0] - x\n"})
    assert problems_of(root) == [
        "README.md:3: '#': an empty anchor",
        "README.md:3: '#nope': README.md has no heading or anchor #nope",
        "README.md:3: 'docs/a.md#': an empty anchor",
        "README.md:3: 'docs/a.md#nope': docs/a.md has no heading or anchor #nope",
        "README.md:3: 'docs/gone.md': no such file or directory",
        "README.md:4: 'CHANGELOG.md#nope': no heading makes the anchor #nope"]


def test_links_leaving_the_repository_or_using_other_schemes_fail(tmp_path):
    (tmp_path / "secret.md").write_text("# Secret\n")
    text = ("# Home\n\n[a](../secret.md) [b](../../etc/passwd) [c](/etc/passwd) [d](javascript:alert(1)) "
            "[e](data:text/html,x)\n[f](//evil.example/x) [g](a\\b.md) [h](a.md?x=1) [i](docs/%00x.md) "
            "[j](ftp://x.example/y)\n")
    problems = readme_problems(tmp_path, text)
    assert len(problems) == 10
    assert sum("outside the repository" in p for p in problems) == 2
    assert sum("only http, https and mailto" in p for p in problems) == 3
    assert sum("relative path with forward slashes" in p for p in problems) == 4
    assert sum("write the scheme" in p for p in problems) == 1


def test_a_symbolic_link_out_of_the_repository_is_not_followed(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.png").write_bytes(PNG)
    (outside / "page.md").write_text("# Page\n")
    root = make_repo(tmp_path, readme="# Home\n\n![a](link/x.png) [b](link/page.md)\n")
    (root / "link").symlink_to(outside)
    (root / "docs").mkdir()
    (root / "docs" / "evil.md").symlink_to(outside / "page.md")
    assert problems_of(root) == ["README.md:3: 'link/page.md' points outside the repository",
                                 "README.md:3: 'link/x.png' points outside the repository",
                                 "docs/evil.md: the page is a link to a file outside the repository"]


def test_images_that_are_missing_remote_or_not_images_fail(tmp_path):
    text = ("# Home\n\n![a](gone.png) ![b](https://example.com/x.png) ![c](//example.com/x.png) ![d](../x.png) "
            "![e](notes.txt) ![f](pic.png#frag) ![g](pic.png) ![h](dir.png)\n")
    root = make_repo(tmp_path, text, files={"notes.txt": "x", "dir.png/keep": "x", "pic.png": PNG})
    problems = problems_of(root)
    assert len(problems) == 7
    assert sum("no such file" in p for p in problems) == 1
    assert sum("remote image" in p for p in problems) == 2
    assert sum("outside the repository" in p for p in problems) == 1
    assert sum("an image must be" in p for p in problems) == 3      # notes.txt, pic.png#frag, the directory dir.png


def test_a_page_in_docs_finds_images_and_pages_relative_to_itself(tmp_path):
    root = make_repo(tmp_path, pages={"a.md": "# A\n\n![x](images/p.png) [y](images/p.png) [z](../README.md#home)\n"},
                     files={"docs/images/p.png": PNG})
    page = page_of(bundle_of(root), "a")
    assert page["images"][0]["path"] == "docs/images/p.png"
    assert page["links"][0]["kind"] == "file" and page["links"][0]["path"] == "docs/images/p.png"
    assert page["links"][1] == {"text": "z", "target": "../README.md#home", "line": 3, "kind": "page",
                                "page": "readme", "anchor": "home"}
    root = make_repo(tmp_path / "again", pages={"a.md": "# A\n\n[y](images/p.png#frag)\n"},
                     files={"docs/images/p.png": PNG})
    assert "a file has no anchors" in problems_of(root)[0]


def test_the_page_ids_must_be_unique_and_well_formed(tmp_path):
    root = make_repo(tmp_path, pages={"Readme.md": "# Other\n", "bad name.md": "# Bad\n"})
    assert problems_of(root) == ["docs/Readme.md: the page id 'readme' is already used by README.md",
                                 "docs/bad name.md: the page id 'bad name' is not lower case letters, digits, - and _"]


def test_a_schema_that_is_not_json_and_a_missing_readme_fail(tmp_path):
    root = make_repo(tmp_path, files={"docs/schemas/a.v1.schema.json": "{not json", "docs/schemas/b.json": "[]",
                                      "docs/schemas/c.v1.schema.json": '{"title": 5}'})
    assert problems_of(root) == ["docs/schemas/a.v1.schema.json: the schema is not a JSON object",
                                 "docs/schemas/b.json: the schema is not a JSON object"]
    (root / "docs" / "schemas" / "a.v1.schema.json").write_text('{"title": "A"}')
    (root / "docs" / "schemas" / "b.json").write_text('{"title": "B"}')
    schemas = bundle_of(root)["schemas"]
    assert [(s["id"], s["title"]) for s in schemas] == [("a.v1", "A"), ("b", "B"), ("c.v1", "")]
    (root / "README.md").unlink()
    assert problems_of(root) == ["README.md: the page does not exist"]


def test_a_repository_without_docs_has_only_the_readme(tmp_path):
    assert [p["id"] for p in bundle_of(make_repo(tmp_path))["pages"]] == ["readme"]


# -- tag, version, repository, output -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("tag", ["v1.2.4", "v2.0.0", "v1.2.3-rc1", "", "-x", "a b", "v1/../x", "x" * 65, "a..b",
                                 "v1.2.3\n"])
def test_the_tag_must_be_the_package_version_or_a_plain_name(tmp_path, tag):
    assert len(problems_of(make_repo(tmp_path), tag=tag)) == 1


@pytest.mark.parametrize("tag", ["v1.2.3", "main", "release-1.2", "feature_x"])
def test_a_release_tag_or_a_branch_name_is_accepted(tmp_path, tag):
    assert bundle_of(make_repo(tmp_path), tag=tag)["tag"] == tag


def test_the_version_comes_from_the_package(tmp_path):
    root = make_repo(tmp_path, version=None)
    assert problems_of(root) == [f"{root}: no homelab_probe/__init__.py to read the version from"]
    (root / "homelab_probe" / "__init__.py").write_text("version = 1\n")
    assert problems_of(root) == ["homelab_probe/__init__.py: no __version__"]


def test_repository_links_use_the_given_repository_and_quote_the_path(tmp_path):
    root = make_repo(
        tmp_path, "# Home\n\n[a](my%20file.md#top) [b](.) [c](docs/) [d](docs/a%20b/)\n[e](https://example.com/x.md)\n",
        files={"my file.md": "# Top\n", "docs/a b/x": "y"})
    links = build_bundle(root, "main", repository="https://example.org/me/proj").data["pages"][0]["links"]
    assert [link["href"] for link in links] == [
        "https://example.org/me/proj/blob/main/my%20file.md#top", "https://example.org/me/proj/tree/main",
        "https://example.org/me/proj/tree/main/docs", "https://example.org/me/proj/tree/main/docs/a%20b",
        "https://example.com/x.md"]
    assert problems_of(root, repository="http://example.org/me/proj") == [
        "--repository 'http://example.org/me/proj': expected https://HOST/OWNER/NAME"]


def test_mailto_links_are_external(tmp_path):
    link = page_of(bundle_of(make_repo(tmp_path, "# Home\n\n[m](mailto:a@example.com)\n")), "readme")["links"][0]
    assert link["kind"] == "external" and link["href"] == "mailto:a@example.com"


def test_the_output_directory_may_not_be_the_repository_or_hold_something_else(tmp_path):
    root = make_repo(tmp_path)
    bundle = build_bundle(root, TAG)
    for bad in (root, tmp_path):
        with pytest.raises(BuildError, match="may not be the repository"):
            write_bundle(bundle, bad, root)
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "mine.txt").write_text("keep me")
    with pytest.raises(BuildError, match="not a previous docs bundle"):
        write_bundle(bundle, busy, root)
    assert (busy / "mine.txt").read_text() == "keep me"
    a_file = tmp_path / "a_file"
    a_file.write_text("x")
    with pytest.raises(BuildError, match="not a directory"):
        write_bundle(bundle, a_file, root)


def test_a_rebuild_replaces_the_old_bundle_and_nothing_else(tmp_path):
    root = make_repo(tmp_path, "# Home\n\n![a](a.png)\n", files={"a.png": PNG})
    out = tmp_path / "out"
    write_bundle(build_bundle(root, TAG), out, root)
    assert (out / "a.png").is_file()
    (out / "other.txt").write_text("mine")
    (root / "README.md").write_text("# Home\n")
    write_bundle(build_bundle(root, TAG), out, root)
    assert sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()) == [
        "a.png", "docs.json", "other.txt"]
    (out / "docs").symlink_to(tmp_path)
    with pytest.raises(BuildError, match="symbolic link"):
        write_bundle(build_bundle(root, TAG), out, root)
    assert (tmp_path / "repo").exists()


def test_the_command_line_prints_every_problem_and_returns_one(tmp_path, capsys):
    root = make_repo(tmp_path, "# Home\n\n<div>\n\n[a](#nope)\n")
    assert main([str(tmp_path / "out"), "--tag", TAG, "--root", str(root)]) == 1
    err = capsys.readouterr().err
    assert "README.md:3: raw HTML" in err and "README.md:5: '#nope'" in err and not (tmp_path / "out").exists()
    assert main([str(tmp_path / "out"), "--tag", TAG, "--root", str(make_repo(tmp_path / "ok"))]) == 0
    assert "1 pages, 0 schemas, 0 images (version 1.2.3, tag v1.2.3)" in capsys.readouterr().out


def test_an_unwritable_output_is_an_error_not_a_traceback(tmp_path, capsys):
    root = make_repo(tmp_path)
    blocked = tmp_path / "blocked"
    blocked.write_text("x")
    assert main([str(blocked / "sub"), "--tag", TAG, "--root", str(root)]) == 1
    assert "build_docs:" in capsys.readouterr().err


# -- the smaller rules of the parser ---------------------------------------------------------------------------------------

def test_a_paragraph_ends_where_another_block_starts(tmp_path):
    text = ("# Home\nintro\n## Next ##\nmore text\n```\ncode\n```\nafter\n> quote\n\n1. first\n2. second\n\n"
            "intro\ntable | head\n--- | ---\nrow | x\nend 2. not a list\n- bullet\n")
    page = page_of(bundle_of(make_repo(tmp_path, text)), "readme")
    assert [h["text"] for h in page["headings"][0]["children"]] == ["Next"]
    assert "table | head" in page["search"] and "end 2. not a list" in page["search"]


def test_lists_of_another_kind_start_a_new_list_and_loose_lists_continue(tmp_path):
    problems = readme_problems(tmp_path, "# Home\n\n- a\n\n- b\n* c\n1. one\n2) two\n\n-     code\n")
    assert problems == ["README.md:10: an indented code block is not supported; use a fenced block"]
    text = "# Home\n\n- a\n\n- b\n* c\n1. one\n2) two\n-\n  empty first line\n"
    page = page_of(bundle_of(make_repo(tmp_path / "ok", text)), "readme")
    assert "empty first line" in page["search"] and "two" in page["search"]


def test_small_inline_and_table_forms(tmp_path):
    text = ("# Home\n\n`` a `` and `` `x` `` [a\\]b](docs/a.md) [unclosed and [ok]( docs/a.md ) [t](docs/a.md 'T' )\n\n"
            "a | b\n- | -\nc | d\n\n[x](docs/a.md#top) [y](docs/a.md#top)")
    page = page_of(bundle_of(make_repo(tmp_path, text, pages={"a.md": "# Top\n"})), "readme")
    assert [link["kind"] for link in page["links"]] == ["page"] * 5
    assert "a and `x` a]b [unclosed and ok t" in page["search"] and "c | d" in page["search"]


def test_headings_inside_a_comment_make_no_anchor(tmp_path):
    root = make_repo(tmp_path, "# Home\n\n[a](MORE.md#shown) [b](MORE.md#hidden) [c](MORE.md#ghost)\n",
                     files={"MORE.md": "# More\n## Shown\n<!--\n## Hidden\n-->\n<!-- ## Inline -->\n"
                                       "`<!-- x -->` ## not a heading\n## Shown\n<!-- <a id=\"ghost\"></a> -->\n"})
    assert problems_of(root) == ["README.md:3: 'MORE.md#ghost': no heading makes the anchor #ghost",
                                 "README.md:3: 'MORE.md#hidden': no heading makes the anchor #hidden"]
    assert build_docs.anchors_of("# A\n<!--\n# B\n-->\n## C\n") == {"a", "c"}
    assert build_docs.heading_texts("# A\n<!-- # B -->\n## C\n") == ["A", "C"]


def test_a_package_file_that_is_a_link_out_of_the_repository_is_not_read(tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text('__version__ = "1.2.3"\n')
    root = make_repo(tmp_path)
    (root / "homelab_probe" / "__init__.py").unlink()
    (root / "homelab_probe" / "__init__.py").symlink_to(outside)
    assert problems_of(root) == ["homelab_probe/__init__.py: points outside the repository"]


def test_a_schema_or_schema_folder_that_leaves_the_repository_fails_the_build(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.v1.schema.json").write_text('{"title": "secret"}')
    root = make_repo(tmp_path, files={"docs/schemas/ok.v1.schema.json": '{"title": "ok"}',
                                      "docs/schemas/folder.json/keep": "a folder named like a schema is no schema"})
    (root / "docs" / "schemas" / "evil.v1.schema.json").symlink_to(outside / "x.v1.schema.json")
    assert problems_of(root) == [
        "docs/schemas/evil.v1.schema.json: the schema is a link to a file outside the repository"]
    (root / "docs" / "schemas" / "evil.v1.schema.json").unlink()
    assert [s["id"] for s in bundle_of(root)["schemas"]] == ["ok.v1"]
    shutil.rmtree(root / "docs" / "schemas")
    (root / "docs" / "schemas").symlink_to(outside)
    assert problems_of(root) == ["docs/schemas: the folder is a link to a place outside the repository"]


def test_a_rebuild_never_writes_through_a_link_left_in_the_output_directory(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "target.txt").write_text("keep")
    (outside / "dir").mkdir()
    root = make_repo(tmp_path, "# Home\n\n![a](a.png) ![b](sub/b.png)\n", files={"a.png": PNG, "sub/b.png": PNG})
    out = tmp_path / "out"
    write_bundle(build_bundle(root, TAG), out, root)
    (out / "a.png").unlink()
    (out / "a.png").symlink_to(outside / "target.txt")
    with pytest.raises(BuildError, match="a.png: a symbolic link in the output directory"):
        write_bundle(build_bundle(root, TAG), out, root)
    assert (out / "docs.json").read_text().startswith("{")          # a refusal removed nothing
    (out / "a.png").unlink()
    (out / "sub").rename(tmp_path / "moved")
    (out / "sub").symlink_to(outside / "dir")
    with pytest.raises(BuildError, match="sub: a symbolic link in the output directory"):
        write_bundle(build_bundle(root, TAG), out, root)
    assert (outside / "target.txt").read_text() == "keep" and not list((outside / "dir").iterdir())
    (out / "sub").unlink()
    (out / "docs.json").unlink()
    (out / "docs.json").symlink_to(outside / "target.txt")
    with pytest.raises(BuildError, match="docs.json: a symbolic link in the output directory"):
        write_bundle(build_bundle(root, TAG), out, root)
    assert (outside / "target.txt").read_text() == "keep" and (out / "docs.json").is_symlink()


def test_the_destination_check_looks_at_every_component_of_the_path(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "real").mkdir()
    (out / "link").symlink_to(out / "real")
    assert build_docs.unlinked_destination(out, "real/x.png") == out / "real" / "x.png"
    with pytest.raises(BuildError, match="link: a symbolic link"):
        build_docs.unlinked_destination(out, "link/x.png")


def test_anchors_in_a_file_outside_the_bundle_are_checked_once_per_file(tmp_path):
    root = make_repo(tmp_path, "# Home\n\n[a](MORE.md#one) [b](MORE.md#one) [c](MORE.md#two)\n",
                     files={"MORE.md": "# More\n## One\n"})
    assert problems_of(root) == ["README.md:3: 'MORE.md#two': no heading makes the anchor #two"]


def test_an_empty_closing_heading_is_empty(tmp_path):
    assert readme_problems(tmp_path, "# Home\n\n## ##\n") == ["README.md:3: an empty heading"]


def test_windows_line_endings_are_read(tmp_path):
    root = make_repo(tmp_path)
    (root / "README.md").write_bytes(b"# Home\r\n\r\ntext\r\n")
    assert bundle_of(root)["pages"][0]["markdown"] == "# Home\n\ntext\n"
