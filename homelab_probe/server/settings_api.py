"""The settings of ``diagnose`` over the API: ``GET`` and ``PUT /api/v1/settings``, the ``hlp.toml`` the command line
reads (thresholds and ignore rules).

* **One loader.** A change is applied to the text of the file, written to a temporary file and loaded with
  ``settings.load_settings``, the function every command uses; a value it refuses is a 422 with its own message and
  nothing is written. There is no second set of rules.
* **Comments stay.** The file is edited with ``tomlkit`` (the ``web`` extra), which keeps comments, blank lines and the
  order of keys; an ignore rule that did not change keeps its table and the comments in it.
* **A concurrent edit is detected, not overwritten.** ``GET`` gives a ``version`` (a hash of the file); ``PUT`` must
  send it back and is refused with 409 when the file is different now, also when it changed while this request worked.
* **Safe writes.** The file is replaced in one step, its permissions kept (a new one is owner-only), the old content
  kept as ``hlp.toml.bak``; a symbolic link is left alone. A request that changes nothing writes nothing.
* **Who.** Anyone logged in may read; only an administrator may change, with the CSRF token, and ``serve --read-only``
  refuses it. Each change is an audit entry (``settings.updated``) naming the thresholds that changed and the number
  of ignore rules, never their text.
"""

import dataclasses
import datetime
import hashlib
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import tomlkit
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt
from tomlkit.exceptions import TOMLKitError

from ..config import ConfigError
from ..settings import DEFAULT_FILENAME, DiagnoseSettings, IgnoreRule, known_codes, load_settings
from ..setup import SETTINGS_STUB
from ..util import printable
from .auth import admin, audit_event, local_write
from .errors import ApiError

API = "/api/v1"
IGNORE_FIELDS = ("code", "subject", "message", "reason")
MAX_RULES = 500
_LOCK = threading.Lock()          # one change at a time: the version check and the write are one step


def settings_file(request: Request) -> Path:
    """The ``hlp.toml`` of this server: the one named with ``--config``, else the one in the data directory."""
    named: Optional[Path] = request.app.state.settings_path
    return named if named is not None else Path(request.app.state.state_dir or ".") / DEFAULT_FILENAME


def load_effective(path: Path) -> DiagnoseSettings:
    """The settings as the commands see them: the file when there is one, else the defaults."""
    return load_settings(path) if path.is_file() else DiagnoseSettings()


def version_of(content: Optional[bytes]) -> str:
    """The token that says which content a client saw: a hash, ``absent`` for a file that is not there."""
    return "absent" if content is None else hashlib.sha256(content).hexdigest()


def _read(path: Path) -> Optional[bytes]:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ApiError(500, "settings_unreadable", "The settings file cannot be read; see the server log.") from error


# -- the document ---------------------------------------------------------------------------------------------

def rule_dict(rule: IgnoreRule, today: datetime.date) -> Dict[str, Any]:
    return {"code": rule.code, "subject": rule.subject, "message": rule.message, "reason": rule.reason,
            "until": rule.until.isoformat() if rule.until else None, "expired": rule.expired(today)}


def document(request: Request, content: Optional[bytes], settings: DiagnoseSettings) -> Dict[str, Any]:
    """What ``GET`` answers: the effective values with their defaults, which of them the file sets, the ignore rules,
    the version to send back, and whether notification destinations are configured (never their values)."""
    values = {k: v for k, v in dataclasses.asdict(settings).items() if k != "ignore"}
    defaults = {k: v for k, v in dataclasses.asdict(DiagnoseSettings()).items() if k != "ignore"}
    in_file: List[str] = []
    if content is not None:
        try:
            in_file = sorted(tomlkit.parse(content.decode("utf-8")).get("thresholds", {}))
        except (TOMLKitError, UnicodeDecodeError):      # pragma: no cover  (the loader refused such a file first)
            in_file = []
    config = request.app.state.config
    today = datetime.date.today()
    return {
        "version": version_of(content), "exists": content is not None, "file": settings_file(request).name,
        "read_only": bool(request.app.state.read_only),
        "thresholds": values, "defaults": defaults, "set_in_file": in_file,
        "ignore": [rule_dict(rule, today) for rule in settings.ignore],
        "codes": sorted(known_codes()),
        "notifications": {"ntfy": bool(config.notify_ntfy_url), "webhook": bool(config.notify_webhook_url),
                          "email": config.notify_smtp is not None},
    }


# -- the change -----------------------------------------------------------------------------------------------

Number = StrictInt | StrictFloat


class IgnoreBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(default="", max_length=200)
    subject: str = Field(default="", max_length=500)
    message: str = Field(default="", max_length=1000)
    reason: str = Field(default="", max_length=1000)
    until: Optional[str] = Field(default=None, max_length=10, description="The last day it applies: 2026-10-10")


class SettingsBody(BaseModel):
    """``thresholds`` sets the keys given (``null`` removes one, so the default applies again) and leaves the others;
    ``ignore``, when given, is the whole list of rules."""

    model_config = ConfigDict(extra="forbid")
    version: str = Field(max_length=100)
    thresholds: Optional[Dict[str, Optional[Number]]] = Field(default=None, max_length=100)
    ignore: Optional[List[IgnoreBody]] = Field(default=None, max_length=MAX_RULES)


def _rule_table(rule: IgnoreBody, existing: List[Any]) -> Any:
    """A TOML table for ``rule``: the table that is already in the file when it says the same (its comments stay), else
    a new one holding only the fields that are set."""
    wanted: Dict[str, Any] = {name: getattr(rule, name) for name in IGNORE_FIELDS if getattr(rule, name)}
    if rule.until is not None:
        try:
            wanted["until"] = datetime.date.fromisoformat(rule.until)
        except ValueError:
            raise ApiError(422, "invalid_settings", "[[ignore]]: until must be a date like 2026-10-10") from None
    for table in existing:
        if table.unwrap() == wanted:
            return table
    table = tomlkit.table()
    for name, value in wanted.items():
        table[name] = value
    return table


def apply_change(text: Optional[str], body: SettingsBody) -> str:
    """The new text of the file: ``text`` (or the commented stub, for a file that is not there yet) with ``body``
    applied."""
    try:
        doc = tomlkit.parse(SETTINGS_STUB if text is None else text)
    except TOMLKitError:
        raise ApiError(409, "settings_file_invalid", "The settings file is not valid TOML, so it cannot be edited "
                       "here: fix it by hand first.") from None
    if body.thresholds:
        table = doc.get("thresholds")
        if table is None:
            table = tomlkit.table()
            doc["thresholds"] = table
        for name, value in body.thresholds.items():
            if value is None:
                table.pop(name, None)
            else:
                table[name] = value
    if body.ignore is not None:
        existing = list(doc.get("ignore", []))
        rules = [_rule_table(rule, existing) for rule in body.ignore]
        if rules:
            array = tomlkit.aot()
            for table in rules:
                array.append(table)
            doc["ignore"] = array
        elif "ignore" in doc:
            del doc["ignore"]
    return tomlkit.dumps(doc)


def validate(text: str) -> DiagnoseSettings:
    """Load ``text`` with the loader every command uses (through a temporary file named like the real one, so its
    messages read the same). A refusal is a 422 with the loader's own message."""
    with tempfile.TemporaryDirectory(prefix="hlp-settings-") as directory:
        path = Path(directory) / DEFAULT_FILENAME
        path.write_text(text, encoding="utf-8")
        try:
            return load_settings(path)
        except ConfigError as error:
            raise ApiError(422, "invalid_settings", printable(str(error)).replace(f"{path}: ", "")) from None


def write_file(path: Path, text: str, previous: Optional[bytes]) -> None:
    """Replace ``path`` in one step, keeping its permissions (a new file is owner-only) and the old content as
    ``<name>.bak``. A symbolic link is refused."""
    if path.is_symlink():
        raise ApiError(409, "settings_is_link", "The settings file is a symbolic link; it is left alone.")
    mode = os.stat(path).st_mode & 0o777 if previous is not None else 0o600
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if previous is not None:
            _replace(path.with_name(path.name + ".bak"), previous, mode)
        _replace(path, text.encode("utf-8"), mode)
    except OSError as error:
        raise ApiError(500, "settings_not_written", "The settings file could not be written; see the server log.") \
            from error


def _replace(path: Path, content: bytes, mode: int) -> None:
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:                                          # pragma: no cover  (the temporary file is gone)
            pass
        raise


def _changes(old: Optional[str], new: str) -> Tuple[List[str], bool]:
    """(the thresholds that are set differently in ``new`` than in ``old``, a removed one included, and whether the
    ignore rules differ), from the two texts."""
    def read(text: Optional[str]) -> Dict[str, Any]:
        return tomlkit.parse(SETTINGS_STUB if text is None else text).unwrap()

    before, after = read(old), read(new)
    one, two = before.get("thresholds", {}), after.get("thresholds", {})
    return (sorted(name for name in {**one, **two} if one.get(name) != two.get(name)),
            before.get("ignore", []) != after.get("ignore", []))


# -- the routes -----------------------------------------------------------------------------------------------

def router() -> APIRouter:
    api = APIRouter(prefix=f"{API}/settings", tags=["settings"])

    @api.get("", summary="The diagnose settings: thresholds and ignore rules")
    def get_settings(request: Request) -> Dict[str, Any]:
        path = settings_file(request)
        content = _read(path)
        try:
            settings = load_effective(path)
        except ConfigError as error:
            raise ApiError(500, "settings_invalid", "The settings file cannot be used; see the server log "
                           "(`hlp doctor` or `diagnose` shows the reason).") from error
        return document(request, content, settings)

    @api.put("", dependencies=[Depends(admin)], summary="Change the diagnose settings")
    @local_write
    def put_settings(request: Request, body: SettingsBody) -> Dict[str, Any]:
        path = settings_file(request)
        with _LOCK:
            content = _read(path)
            if version_of(content) != body.version:
                raise ApiError(409, "settings_changed", "The settings file changed since you loaded it: load it again.")
            try:
                load_effective(path)
            except ConfigError:
                raise ApiError(409, "settings_file_invalid", "The settings file cannot be used as it is, so it "
                               "cannot be edited here: fix it by hand first.") from None
            old = None if content is None else content.decode("utf-8")
            text = apply_change(old, body)
            after = validate(text)
            if text == (SETTINGS_STUB if old is None else old):
                return document(request, content, after)             # nothing to change: nothing is written
            if version_of(_read(path)) != body.version:               # it changed while this request worked
                raise ApiError(409, "settings_changed", "The settings file changed since you loaded it: load it again.")
            thresholds, rules = _changes(old, text)
            write_file(path, text, content)
            written = text.encode("utf-8")
        audit_event(request, "settings.updated", request.state.session.username, thresholds=",".join(thresholds),
                    ignore_rules=f"{len(after.ignore)} rule(s)" if rules else "unchanged")
        return document(request, written, after)

    return api
