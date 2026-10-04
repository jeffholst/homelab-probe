"""Small accuracy items: one source for the version, wifi channels the span maths used to get wrong, and
claims in the README and the package metadata that must stay true."""

import ast
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from unifi_sentinel import __version__
from unifi_sentinel.wifi import CHANNEL_14_CENTRE_MHZ, overlaps, span_mhz

ROOT = Path(__file__).resolve().parent.parent

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover  (Python 3.10 only)
    import tomli as tomllib


def pyproject():
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


# -- one source for the version ------------------------------------------------------------------------------

def test_the_version_is_written_once_and_pyproject_reads_it():
    config = pyproject()
    assert "version" not in config["project"], "the version must not be repeated in pyproject.toml"
    assert "version" in config["project"]["dynamic"]
    assert config["tool"]["setuptools"]["dynamic"]["version"] == {"attr": "unifi_sentinel.__version__"}


def test_the_version_is_a_plain_literal_setuptools_can_read_without_importing_the_package():
    tree = ast.parse((ROOT / "unifi_sentinel" / "__init__.py").read_text(encoding="utf-8"))
    assigned = [n for n in tree.body if isinstance(n, ast.Assign) and any(
        isinstance(t, ast.Name) and t.id == "__version__" for t in n.targets)]
    assert len(assigned) == 1 and isinstance(assigned[0].value, ast.Constant)
    assert re.fullmatch(r"\d+\.\d+\.\d+([abc]\d+|rc\d+|\.dev\d+)?", assigned[0].value.value)


def test_no_other_source_file_repeats_the_version_string():
    version = re.escape(__version__)
    for path in (ROOT / "unifi_sentinel").rglob("*.py"):
        if path.name == "__init__.py" and path.parent.name == "unifi_sentinel":
            continue
        assert not re.search(rf'["\']{version}["\']', path.read_text(encoding="utf-8")), path


def test_the_command_line_reports_the_package_version(capsys):
    from unifi_sentinel import cli
    with pytest.raises(SystemExit) as caught:
        cli.main(["--version"])
    assert caught.value.code == 0 and capsys.readouterr().out.strip() == __version__


@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv to build a wheel")
def test_a_built_wheel_carries_the_same_version(tmp_path):
    # Built from a copy with only what a build reads: a stale build/ or *.egg-info next to the sources (git-ignored,
    # left by an earlier build) makes setuptools include files that pyproject.toml no longer asks for.
    source = tmp_path / "source"
    source.mkdir()
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        if (ROOT / name).exists():
            shutil.copy(ROOT / name, source / name)
    shutil.copytree(ROOT / "unifi_sentinel", source / "unifi_sentinel", ignore=shutil.ignore_patterns("__pycache__"))
    result = subprocess.run(["uv", "build", "--wheel", "--out-dir", str(tmp_path / "dist"), str(source)],
                            capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stderr
    (wheel,) = (tmp_path / "dist").glob("*.whl")
    assert wheel.name.startswith(f"unifi_sentinel-{__version__}-")
    metadata = zipfile.ZipFile(wheel).read(f"unifi_sentinel-{__version__}.dist-info/METADATA").decode("utf-8")
    assert f"Version: {__version__}" in metadata
    assert "Name: unifi-sentinel" in metadata
    names = zipfile.ZipFile(wheel).namelist()
    assert "unifi_sentinel/demo/controller.json" in names and "unifi_sentinel/demo/session.py" in names   # --demo


# -- wifi: channel 14 and U-NII-4 ---------------------------------------------------------------------------------

def test_channel_14_is_centered_on_2484_not_on_the_five_mhz_rule():
    assert CHANNEL_14_CENTRE_MHZ == 2484
    assert span_mhz("ng", 14, 20) == (2473, 2495)
    assert span_mhz("ng", 13, 20) == (2461, 2483) and span_mhz("ng", 1, 20) == (2401, 2423)      # unchanged


def test_channel_14_overlaps_13_but_not_11():
    assert overlaps(span_mhz("ng", 14, 20), span_mhz("ng", 13, 20))
    assert not overlaps(span_mhz("ng", 14, 20), span_mhz("ng", 11, 20))
    assert not overlaps(span_mhz("ng", 14, 20), span_mhz("ng", 6, 20))


def test_the_wide_2_4_ghz_paths_use_the_same_centre_rule():
    assert span_mhz("ng", 14, 40, None, 14) == (2464, 2504)                       # centre channel given
    assert span_mhz("ng", 13, 40, None, None, 14) == ((2472 + 2484) / 2 - 20, (2472 + 2484) / 2 + 20)   # secondary
    assert span_mhz("ng", 6, 40, None, 8) == (2427.0, 2467.0)                     # the old results are unchanged
    assert span_mhz("ng", 6, 40, None, None, "above") == (2427.0, 2467.0)


@pytest.mark.parametrize("channel, width, expected", [
    (165, 20, (5815, 5835)),                                   # a 20 MHz channel was always right
    (165, 40, (5815, 5855)), (169, 40, (5815, 5855)),
    (173, 40, (5855, 5895)), (177, 40, (5855, 5895)),
    (165, 80, (5815, 5895)), (169, 80, (5815, 5895)), (177, 80, (5815, 5895)),
    (149, 160, (5735, 5895)), (161, 160, (5735, 5895)), (177, 160, (5735, 5895)),
])
def test_unii_4_channels_belong_to_their_blocks(channel, width, expected):
    assert span_mhz("na", channel, width) == expected


def test_the_blocks_below_unii_4_are_unchanged_and_do_not_overlap_it():
    assert span_mhz("na", 149, 80) == (5735, 5815) and span_mhz("na", 161, 80) == (5735, 5815)
    assert span_mhz("na", 36, 80) == (5170, 5250) and span_mhz("na", 100, 160) == (5490, 5650)
    assert not overlaps(span_mhz("na", 161, 80), span_mhz("na", 165, 80))
    assert overlaps(span_mhz("na", 161, 80), span_mhz("na", 149, 160))
    assert overlaps(span_mhz("na", 169, 40), span_mhz("na", 165, 80))


# -- claims that must stay true --------------------------------------------------------------------------------------

def test_the_readme_says_what_was_actually_tested():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "9.5.21+ recommended" not in readme
    requirements = readme.split("## Requirements", 1)[1].split("\n## ", 1)[0]
    assert "Tested only against Network 10.6.106" in requirements and "9.5.21" in requirements


def test_the_package_metadata_describes_what_the_tool_is_now():
    project = pyproject()["project"]
    keywords = set(project["keywords"])
    assert not keywords & {"export", "csv"} and {"troubleshooting", "inventory", "unifi"} <= keywords
    assert "troubleshoot" in project["description"] and "inventory" in project["description"]
    assert "Topic :: System :: Networking :: Monitoring" in project["classifiers"]
