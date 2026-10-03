"""The diagnose package: one module per topic, a stable public surface, and the order of the checks."""

import ast
import importlib
from pathlib import Path

import pytest

import unifi_sentinel.diagnose as diagnose_package
import unifi_sentinel.diagnose.areas as areas_module
from unifi_sentinel.diagnose import diagnose
from unifi_sentinel.diagnose.model import Finding
from unifi_sentinel.settings import DiagnoseSettings
from unifi_sentinel.snapshot import Needs, collect_snapshot

PACKAGE = Path(diagnose_package.__file__).parent
MODULES = ["addresses", "areas", "devices", "event_checks", "health", "model", "output", "ports", "reserved", "wireless"]

PUBLIC = {
    "BANDS", "CODES", "CRITICAL", "DEVICE_SUBSYSTEMS", "EMOJI", "EXIT_CRITICAL", "EXIT_OK", "EXIT_WARNING",
    "GATEWAY_TYPES", "INFO", "JSON_VERSION", "LINK_LOCAL_PREFIX", "SEVERITY_ORDER", "WARNING", "Finding",
    "apply_ignores", "diagnose", "exit_code", "findings_json", "format_findings", "format_ignored",
    "stream_supports_emoji", "uplink_speeds"}

# The order in which diagnose() runs its checks. Findings with the same severity and subject keep this order.
CHECK_ORDER = [
    "_offline_device_findings", "_resource_findings", "_overheating_findings", "_health_findings", "_wan_findings", "_client_ip_findings",
    "_reservation_findings", "_pool_findings", "_offline_reservation_findings", "_private_mac_findings",
    "_duplicate_ip_findings", "_legacy_unavailable_findings", "_port_basic_findings", "_port_health_findings",
    "_uplink_speed_findings", "_wifi_findings", "_event_findings"]


def test_the_package_is_split_into_the_documented_modules():
    assert sorted(p.stem for p in PACKAGE.glob("*.py") if p.stem != "__init__") == MODULES
    for name in MODULES:
        assert importlib.import_module(f"unifi_sentinel.diagnose.{name}").__doc__, name


def test_the_public_names_are_unchanged():
    assert PUBLIC <= set(diagnose_package.__all__)
    for name in PUBLIC:
        assert hasattr(diagnose_package, name), name


def test_finding_is_one_class_whichever_way_it_is_imported():
    assert diagnose_package.Finding is Finding
    assert Finding("info", "x", "y").code == ""


def test_no_module_is_big_again():
    for path in PACKAGE.glob("*.py"):
        assert len(path.read_text(encoding="utf-8").splitlines()) < 250, f"{path.name} should be split further"


def test_modules_import_each_others_public_names_only():
    """Only the package's __init__ and areas.py (which assembles the checks) may pull in the underscore-named
    checks."""
    for path in PACKAGE.glob("*.py"):
        if path.name in ("__init__.py", "areas.py"):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.level >= 1:
                private = [a.name for a in node.names if a.name.startswith("_")]
                assert not private, f"{path.name} imports {private} from {node.module}"


def test_diagnose_runs_every_check_once_in_the_documented_order(fake_client, monkeypatch):
    calls = []

    def spy(name):
        def check(*args, **kwargs):
            calls.append(name)
            return []
        return check

    for name in CHECK_ORDER:
        monkeypatch.setattr(areas_module, name, spy(name))
    snap = collect_snapshot(fake_client, "default", Needs())
    assert diagnose(snap, DiagnoseSettings()) == [] and calls == CHECK_ORDER


def test_checks_are_listed_in_the_module_of_their_topic():
    expected = {
        "devices": {"_offline_device_findings", "_resource_findings", "_overheating_findings"},
        "health": {"_health_findings", "_wan_findings"},
        "addresses": {"_client_ip_findings", "_duplicate_ip_findings", "_private_mac_findings"},
        "reserved": {"_reservation_findings", "_pool_findings", "_offline_reservation_findings"},
        "ports": {"_legacy_unavailable_findings", "_port_basic_findings", "_port_health_findings",
                  "_uplink_speed_findings"},
        "wireless": {"_wifi_findings"},
        "event_checks": {"_event_findings"},
    }
    for module, names in expected.items():
        loaded = importlib.import_module(f"unifi_sentinel.diagnose.{module}")
        assert all(callable(getattr(loaded, n)) for n in names), module
    assert {n for names in expected.values() for n in names} == set(CHECK_ORDER)


@pytest.mark.parametrize("module", ["devices", "health", "addresses", "reserved", "ports", "wireless", "event_checks"])
def test_each_check_module_builds_findings(module):
    source = (PACKAGE / f"{module}.py").read_text(encoding="utf-8")
    assert "Finding(" in source, f"{module} has no checks"


def test_every_package_directory_is_included_when_the_project_is_installed():
    """An editable install finds any folder, a built wheel only what pyproject lists: a subpackage that is not
    matched would be missing from `pip install .` and from a release. (Found when diagnose.py became a package.)"""
    import sys

    root = Path(__file__).resolve().parent.parent
    if sys.version_info >= (3, 11):
        import tomllib
    else:  # pragma: no cover  (Python 3.10 only)
        import tomli as tomllib
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["setuptools"]
    packages = config.get("packages")
    patterns = packages.get("find", {}).get("include") if isinstance(packages, dict) else None
    assert patterns == ["unifi_sentinel*"], "use [tool.setuptools.packages.find] with include = [\"unifi_sentinel*\"]"
    from fnmatch import fnmatch

    on_disk = {
        p.parent.relative_to(root).as_posix().replace("/", ".")
        for p in (root / "unifi_sentinel").rglob("__init__.py")
    }
    assert "unifi_sentinel.diagnose" in on_disk
    assert all(any(fnmatch(name, pattern) for pattern in patterns) for name in on_disk), on_disk
