"""The server stays outside the core: the command line never imports FastAPI (issue #183)."""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent.parent / "homelab_probe"
WEB = {"fastapi", "starlette", "uvicorn", "pydantic", "anyio", "h11", "httpx", "httpx2"}


def imports(path, *, top_level_only):
    """The modules a file imports, written as ``a.b`` (relative imports resolved against the package)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    nodes = tree.body if top_level_only else list(ast.walk(tree))
    found = []
    for node in nodes:
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = ("." * node.level) + (node.module or "")
            found += [base] + [f"{base}.{alias.name}".lstrip(".") for alias in node.names]
    return found


def core_files():
    return [p for p in PACKAGE.rglob("*.py") if "server" not in p.relative_to(PACKAGE).parts]


def test_importing_the_command_line_never_imports_the_web_stack():
    code = ("import sys, homelab_probe.cli, homelab_probe.commands, homelab_probe.documents, homelab_probe.accounts;"
            f"bad = sorted(m for m in sys.modules if m.split('.')[0] in {sorted(WEB)!r} "
            "or m.startswith('homelab_probe.server'));"
            "print(','.join(bad)); sys.exit(1 if bad else 0)")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout


def test_no_core_module_imports_the_web_stack_or_the_server_at_module_level():
    offenders = []
    for path in core_files():
        for name in imports(path, top_level_only=True):
            if name.split(".")[0] in WEB or "server" in name.split("."):
                offenders.append(f"{path.relative_to(PACKAGE)}: {name}")
    assert offenders == []


def test_the_only_core_import_of_the_server_is_inside_the_serve_command():
    inside = {}
    for path in core_files():
        for name in imports(path, top_level_only=False):
            if "server" in name.split(".") or name.split(".")[0] in WEB:
                inside.setdefault(path.name, set()).add(name)
    assert set(inside) == {"commands.py"} and all("server.runner" in n for n in inside["commands.py"])


def test_documents_and_accounts_do_not_know_the_server_exists():
    for name in ("documents.py", "accounts.py"):
        assert not [n for n in imports(PACKAGE / name, top_level_only=False) if "server" in n.split(".")], name


@pytest.mark.parametrize("path", sorted((PACKAGE / "server").glob("*.py")), ids=lambda p: p.name)
def test_the_server_makes_no_request_toward_the_controller_by_itself(path):
    """A request toward the controller can only be made by a ``UniFiClient`` (GET, and the one event-log POST): the
    server imports no HTTP library and calls no write method."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules = {alias.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for alias in n.names}
    modules |= {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and not n.level}
    assert not modules & {"requests", "urllib3", "httpx", "httpx2", "socket", "urllib"}
    called = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not called & {"post", "put", "patch", "delete", "request", "send"}
