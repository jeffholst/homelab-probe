"""The guided setup of the server: what a server that is not set up yet does, and the routes it offers.

A server with **nothing configured** (neither ``UNIFI_URL`` nor ``UNIFI_API_KEY``) is in the **setup mode**
(``SetupState.mode``); settings that exist but are broken still fail loudly at start, as they always did. In the setup
mode every endpoint answers 503 ``not_configured`` except the public ones and the setup routes below
(``auth.guard``), and nothing is read from a controller until the setup says so.

* **Who may use it.** While no administrator exists, whoever has the *setup token*: a random value printed once on the
  console of the server (or set in ``HLP_SETUP_TOKEN``), sent in the ``X-Setup-Token`` header. A wrong token is
  counted and slowed down like a wrong password (``LoginThrottle``), audited as ``setup.token_failed`` and never says
  how close it was. As soon as an administrator exists (the accounts file is looked at on every request), and once the
  server is set up (``mode`` is None), the same routes need an administrator's session instead: the token stops working.
* **The draft.** What is typed is kept **on the server**, in memory (``Draft``), never in the browser and never in a
  log: the API key is registered with the log redaction as soon as it arrives and is only ever reported as "set".
* **The certificate.** ``/certificate`` fetches the certificate the controller shows (not verified: see
  ``tlsprobe``) and tests that it would be accepted for that address; the owner then accepts it by sending its
  fingerprint back, or turns checking off by typing ``UNVERIFIED_PHRASE``. Both are explicit, separate requests.
* **The test.** ``/connection`` runs the checks of ``hlp doctor`` that need the controller on what was typed;
  ``/preview`` runs ``diagnose`` without the event log. Both send the API key to the controller and to nobody else, do
  not follow redirects (a redirect would carry the key to another host), use a short timeout, and answer with fixed
  text only.

* **Finishing.** ``/finish`` checks that the draft has passed the connection test, validates the first administrator,
  writes the files (``setup.apply``: ``.env``, ``hlp.toml``, ``snapshots/``, and ``certs/controller.pem`` for a pinned
  certificate), creates the administrator, reloads the configuration, starts the controller service and leaves the
  setup mode, all without a restart. When the files cannot be written (a read-only volume, a settings file named
  elsewhere, settings that the environment sets) nothing is written and the answer is the finished ``.env`` and a
  compose snippet to copy, with a placeholder where the API key goes: the key is never returned. A server that has
  settings but no administrator is in the **admin mode**: the token may create the first administrator, and only that.

Every step is an audit entry (``setup.*``) with the address of the controller and never a key or a token.
"""

import contextlib
import dataclasses
import json
import logging
import math
import os
import re
import secrets
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Literal, Optional, Tuple
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from .. import config as config_module
from .. import logs, tlsprobe
from .. import setup as setup_engine
from ..accounts import AccountError, AccountStore, check_password_policy, check_username
from ..client import UniFiAPIError, UniFiClient
from ..config import KNOWN_VARIABLES, Config, ConfigError
from ..diagnose.areas import AREA_NAMES
from ..doctor import check_controller, check_notifications, exit_failed
from ..documents import diagnose_document, info_document
from ..settings import DiagnoseSettings
from ..setup import validate_field
from ..util import printable
from .auth import address_of, admin, audit_event, setup_ok
from .errors import ApiError, from_controller
from .service import ControllerService

_log = logging.getLogger(__name__)
API = "/api/v1"
MODE_SETUP, MODE_ADMIN = "setup", "admin"
ADMIN_MODE_STEPS = frozenset({"status", "finish"})       # what the admin mode offers: the first administrator only
NOTIFY_NAMES = tuple(name for name in KNOWN_VARIABLES if name.startswith("NOTIFY_"))
CERT_DIR, CERT_FILE = "certs", "controller.pem"           # in the data directory: the pinned certificate
MAX_NOTIFY_VALUE = 1024
TOKEN_HEADER = "x-setup-token"
SETUP_ACTOR = "(setup)"
MIN_TOKEN_LENGTH = 16
UNVERIFIED_PHRASE = "send my API key without verifying the controller"
CONNECTION_TIMEOUT = 8.0
MAX_PREVIEW_FINDINGS = 25
PIN_FILE = "controller-certificate.pem"
_PINNING = re.compile(r"[^0-9A-Fa-f]")
# What is wrong with the address, as opposed to the network between here and the controller.
_ADDRESS_REASONS = frozenset({"bad_url", "unresolvable", "blocked_address", "public_address"})


def new_token() -> str:
    return secrets.token_urlsafe(24)


@dataclass
class Draft:
    """What has been typed so far. ``verify`` is ``true`` (the system's certificate authorities), ``pin`` (only the
    certificate fetched and accepted here) or ``false`` (not checked, after the typed confirmation)."""

    url: str = ""
    site: str = config_module.DEFAULT_SITE
    api_key: str = field(default="", repr=False)
    verify: str = "true"
    certificate: Optional[tlsprobe.Certificate] = None      # the last one fetched
    problem: str = ""                                       # why it would not be accepted (a tlsprobe reason), or ""
    connection_ok: Optional[bool] = None                    # the last test of the connection, if any
    notify: Dict[str, str] = field(default_factory=dict, repr=False)      # NOTIFY_* settings (secrets)


class SetupState:
    """The setup of one server: its mode, token and draft. ``mode`` becomes None when the server is set up."""

    def __init__(self, mode: Optional[str], reason: str, token: Optional[str] = None, *,
                 site: str = config_module.DEFAULT_SITE, allow_public: bool = False,
                 resolver: tlsprobe.Resolver = tlsprobe.system_resolver,
                 client_factory: Optional[Callable[[Config], UniFiClient]] = None,
                 reload: Optional[Callable[[], Config]] = None,
                 service_factory: Optional[Callable[[Config], ControllerService]] = None,
                 env_named: bool = False) -> None:
        self.mode, self.reason = mode, reason
        self.reload = reload                  # the configuration as the server reads it (set by the runner)
        self.service_factory = service_factory
        self.env_named = env_named            # the settings file is named by --env-file or HLP_ENV
        self.token_shown = token is None                    # a token the operator chose is not repeated on the console
        self.token = token or new_token()
        self.allow_public = allow_public
        self.resolver = resolver
        self.client_factory = client_factory
        self.draft = Draft(site=site)
        self.generation = 0                          # counts the changes of the draft: a result is for one of them
        self.lock = threading.RLock()
        logs.register_secrets(self.token)

    # -- the draft ---------------------------------------------------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        """The draft as the browser may see it: everything but the key, which is only reported as set."""
        draft = self.draft
        certificate = None if draft.certificate is None else {
            "fingerprint": draft.certificate.fingerprint, "usable": not draft.problem, "problem": draft.problem,
            "problem_message": tlsprobe.REASONS.get(draft.problem, "")}
        return {"mode": self.mode, "reason": self.reason, "unverified_phrase": UNVERIFIED_PHRASE,
                "draft": {"url": draft.url, "site": draft.site, "api_key_set": bool(draft.api_key),
                          "verify": draft.verify, "certificate": certificate,
                          "connection_ok": draft.connection_ok, "notify": sorted(draft.notify)}}

    def update(self, body: "DraftBody") -> List[str]:
        """Apply the fields of ``body`` that are set, all or nothing. Returns the names of the settings that changed;
        an invalid value is a 422 with the reason, and nothing changes."""
        with self.lock:
            new = dataclasses.replace(self.draft)
            changed: List[str] = []
            if body.url is not None:
                url = self._valid("UNIFI_URL", body.url.strip())
                if url != new.url:
                    new.url, new.certificate, new.problem, new.connection_ok = url, None, "", None
                    new.verify = "true"              # a certificate or a confirmation is for one address only
                    changed.append("UNIFI_URL")
            if body.site is not None:
                site = self._valid("UNIFI_SITE_ID", body.site.strip())
                if site != new.site:
                    new.site, new.connection_ok = site, None
                    changed.append("UNIFI_SITE_ID")
            if body.api_key is not None:
                key = self._valid("UNIFI_API_KEY", body.api_key.strip())
                if key != new.api_key:
                    new.api_key, new.connection_ok = key, None
                    changed.append("UNIFI_API_KEY")
            if body.verify is not None:
                verify = self._verify(new, body)
                if verify != new.verify:
                    new.verify, new.connection_ok = verify, None
                    changed.append("UNIFI_VERIFY_SSL")
            if body.notify is not None:
                notify = self._notify(new.notify, body.notify)
                if notify != new.notify:
                    new.notify = notify
                    changed.append("NOTIFY_*")
            if new.api_key and new.api_key != self.draft.api_key:
                logs.register_secrets(new.api_key)
            logs.register_secrets(*new.notify.values())
            self.draft = new
            if changed:
                self.generation += 1
            return changed

    @staticmethod
    def _notify(current: Dict[str, str], given: Dict[str, Optional[str]]) -> Dict[str, str]:
        """``current`` with the NOTIFY_* settings of ``given`` applied (a blank value removes one), checked together
        with the rules of the commands. The message of a failed check never repeats a value."""
        merged = dict(current)
        for name, value in given.items():
            text = (value or "").strip()
            if name not in NOTIFY_NAMES or len(text) > MAX_NOTIFY_VALUE:
                raise ApiError(422, "invalid_setting", "That is not a notification setting, or its value is too long.",
                               setting="NOTIFY_*")
            if text:
                merged[name] = text
            else:
                merged.pop(name, None)
        problem = validate_field(dict(merged), "NOTIFY_*")
        if problem is not None:
            raise ApiError(422, "invalid_setting", problem, setting="NOTIFY_*")
        return merged

    @staticmethod
    def _valid(name: str, value: str) -> str:
        """``value`` for the setting ``name``, or a 422. The message of the check is used as it is (it never contains
        the API key); the address is refused here when it carries credentials, before any message could repeat it."""
        if name == "UNIFI_URL":
            try:
                parts = urlsplit(value)
            except ValueError:                      # such as https://[::1: let the check below word it
                parts = urlsplit("")
            if value and (parts.username is not None or parts.password is not None):
                raise ApiError(422, "invalid_setting", "The address must not contain a user name or password.",
                               setting=name)
        problem = validate_field({name: value}, name)           # an http:// address does not pass: it is not allowed
        if problem is not None or not value:
            raise ApiError(422, "invalid_setting", problem or f"{name} needs a value.", setting=name)
        return value.rstrip("/") if name == "UNIFI_URL" else value

    @staticmethod
    def _verify(draft: Draft, body: "DraftBody") -> str:
        if body.verify == "false":
            if (body.confirm or "").strip() != UNVERIFIED_PHRASE:
                raise ApiError(422, "confirmation_required",
                               "Turning certificate checking off needs the confirmation sentence, typed exactly.",
                               setting="UNIFI_VERIFY_SSL")
        elif body.verify == "pin":
            certificate = draft.certificate
            if certificate is None:
                raise ApiError(422, "certificate_required", "Fetch the controller's certificate first.",
                               setting="UNIFI_VERIFY_SSL")
            if _PINNING.sub("", body.fingerprint or "").lower() != certificate.fingerprint.replace(":", "").lower():
                raise ApiError(422, "fingerprint_mismatch",
                               "The fingerprint does not match the certificate that was fetched.",
                               setting="UNIFI_VERIFY_SSL")
            if draft.problem:
                raise ApiError(422, "certificate_unusable", tlsprobe.REASONS[draft.problem],
                               setting="UNIFI_VERIFY_SSL", reason=draft.problem)
        return str(body.verify)

    # -- using it ----------------------------------------------------------------------------------------------

    def require(self, *, key: bool = True) -> Draft:
        draft = self.draft
        if not draft.url or (key and not draft.api_key):
            raise ApiError(409, "draft_incomplete", "Enter the controller's address and an API key first.")
        return draft

    @contextlib.contextmanager
    def config(self) -> Iterator[Tuple[Config, int]]:
        """(a ``Config`` for the draft, the generation of the draft it was made from). With a pinned certificate the
        certificate is a private file for as long as the block lasts (``UNIFI_VERIFY_SSL`` names a file), removed when
        it ends. The draft is copied under the lock, so a change made while a test runs does not mix into it."""
        with self.lock:
            draft = dataclasses.replace(self.require())
            generation = self.generation
        with tempfile.TemporaryDirectory(prefix="hlp-setup-") as directory:
            verify = draft.verify
            if verify == "pin":
                assert draft.certificate is not None           # `update` only accepts a pin after a fetch
                path = Path(directory) / PIN_FILE
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "w") as handle:
                    handle.write(draft.certificate.pem)
                verify = str(path)
            try:
                config = config_module.build_config({
                    "UNIFI_URL": draft.url, "UNIFI_API_KEY": draft.api_key, "UNIFI_SITE_ID": draft.site,
                    "UNIFI_VERIFY_SSL": verify, "UNIFI_TIMEOUT": str(CONNECTION_TIMEOUT), **draft.notify})
            except ConfigError:
                raise ApiError(422, "invalid_setting", "The settings typed so far cannot be used together.") from None
            yield config, generation

    def client(self, config: Config) -> UniFiClient:
        """The client of a test: one that does not follow a redirect, because the API key would go with it."""
        client = (self.client_factory or UniFiClient.from_config)(config)
        client.session.max_redirects = 0
        return client

    def scrub(self, text: str) -> str:
        """``text`` without the draft's key or the controller's address (the checks are fixed words; a safety net)."""
        draft = self.draft
        if len(draft.api_key) >= 4:
            text = text.replace(draft.api_key, "***")
        parts = urlsplit(draft.url)
        for hidden in (draft.url, parts.netloc, parts.hostname or ""):
            if len(hidden) >= 3:
                text = text.replace(hidden, "<controller>")
        return text


# -- access ---------------------------------------------------------------------------------------------------

def administrator_exists(store: AccountStore) -> bool:
    """Is there an enabled administrator? (``AccountError`` for an accounts file that cannot be read.)"""
    return any(user.role == "admin" and not user.disabled for user in store.users())


def setup_access(request: Request) -> None:
    """The dependency of every setup route: the setup token while the server is in a setup mode and has no
    administrator, an administrator's session (with the CSRF token of an unsafe request) otherwise."""
    state: Optional[SetupState] = request.app.state.setup
    auth = request.app.state.auth
    if state is None or state.mode is None:
        admin(request)
        return
    try:
        has_administrator = administrator_exists(auth.accounts.store)
    except AccountError:
        raise ApiError(500, "accounts_unreadable", "The accounts file cannot be read; see the server log.") from None
    if has_administrator:
        admin(request)
        return
    address = address_of(request)
    wait = auth.throttle.wait(address, SETUP_ACTOR)
    if wait > 0:
        seconds = math.ceil(wait)
        raise ApiError(429, "too_many_attempts", f"Too many attempts. Try again in {seconds} seconds.",
                       headers={"Retry-After": str(seconds)}, retry_after=seconds)
    supplied = request.headers.get(TOKEN_HEADER, "")
    if not secrets.compare_digest(supplied.encode("utf-8"), state.token.encode("utf-8")):
        auth.throttle.failed(address, SETUP_ACTOR)
        audit_event(request, "setup.token_failed", SETUP_ACTOR)
        raise ApiError(401, "invalid_setup_token", "The setup token is missing or wrong.")
    auth.throttle.succeeded(address, SETUP_ACTOR)


def _actor(request: Request) -> str:
    session = getattr(request.state, "session", None)
    return session.username if session is not None else SETUP_ACTOR


def _state(request: Request, step: str) -> SetupState:
    state: Optional[SetupState] = request.app.state.setup
    if state is None:
        raise ApiError(404, "no_setup", "This server has no guided setup.")
    if state.mode == MODE_ADMIN and step not in ADMIN_MODE_STEPS:
        raise ApiError(409, "step_unavailable", "This server has its settings: only the first administrator is "
                       "missing, and that is the one step available.")
    return state


# -- finishing ------------------------------------------------------------------------------------------------

def _credentials(store: AccountStore, body: "FinishBody", needs_admin: bool) -> Optional[Tuple[str, str]]:
    """The validated (username, password) of the first administrator, None when one exists already. Everything is
    checked before anything is written."""
    if not needs_admin:
        if body.username or body.password:
            raise ApiError(422, "admin_exists", "An administrator exists already.")
        return None
    if not body.username or not body.password:
        raise ApiError(422, "admin_required", "Choose a user name and a password for the first administrator.")
    try:
        name = check_username(body.username)
        check_password_policy(body.password)
        taken = store.get(name) is not None
    except AccountError as error:
        raise ApiError(422, "invalid_admin", printable(str(error))) from None
    if taken:
        raise ApiError(422, "invalid_admin", "That user name is taken.")
    return name, body.password


def _compose_snippet(values: Dict[str, str]) -> str:
    """The settings as the ``environment`` of a compose service (``$`` doubled: compose would expand it)."""
    lines = ["services:", "  hlp:", "    environment:"]
    lines += [f"      {name}: {json.dumps(value.replace('$', '$$'))}" for name, value in values.items()]
    return "\n".join(lines) + "\n"


def _fallback(state: SetupState, reason: str, values: Dict[str, str], detail: str = "",
              names: Optional[List[str]] = None) -> Dict[str, Any]:
    """What to do by hand when the settings cannot be saved here: the finished ``.env`` and a compose snippet, with a
    placeholder where the API key goes (the key is never sent back) and, for a pinned certificate, the certificate to
    save and a path to put in its place."""
    shown = {**values, "UNIFI_API_KEY": setup_engine.PLACEHOLDER_KEY}
    pinned = state.draft.verify == "pin" and state.draft.certificate is not None
    if pinned:
        shown["UNIFI_VERIFY_SSL"] = "/path/to/controller.pem"
    return {"finished": False, "written": False, "reason": reason, "detail": detail,
            "environment_names": names or [], "env": setup_engine.render_env(shown),
            "compose": _compose_snippet(shown),
            "certificate": state.draft.certificate.pem if pinned and state.draft.certificate else None}


def _write_settings(state: SetupState, directory: Path, written: List[Dict[str, str]]) -> Optional[Dict[str, Any]]:
    """Save the draft in ``directory``; the fallback (see ``_fallback``) instead of an answer when it cannot be
    saved there. Nothing is written when the environment or a named settings file would win over the saved one."""
    draft = state.draft
    certificate = (Path(directory) / CERT_DIR / CERT_FILE).resolve()
    values: Dict[str, str] = {
        "UNIFI_URL": draft.url, "UNIFI_API_KEY": draft.api_key, "UNIFI_SITE_ID": draft.site,
        "UNIFI_VERIFY_SSL": str(certificate) if draft.verify == "pin" else draft.verify, **draft.notify}
    overridden = sorted(name for name, value in values.items()
                        if os.environ.get(name, "").strip() not in ("", value))
    if overridden:
        return _fallback(state, "environment", values, names=overridden)
    if state.env_named:
        return _fallback(state, "env_file_named", values)
    try:
        steps = []
        if draft.verify == "pin" and draft.certificate is not None:
            steps.append(setup_engine.ensure_private_dir("setup.certificates", certificate.parent))
            steps.append(setup_engine.write_private_file("setup.certificate", certificate, draft.certificate.pem))
        steps += setup_engine.apply(values, directory)
    except setup_engine.SetupError as error:
        return _fallback(state, "not_writable", values, detail=printable(str(error)))
    except OSError as error:
        return _fallback(state, "not_writable", values, detail=printable(error.strerror or type(error).__name__))
    written.extend({"id": step.id, "status": step.status, "message": step.message} for step in steps)
    return None


def _reload_default(directory: Path) -> Config:
    """The configuration as the server reads it when nothing else was arranged: the ``.env`` of the data directory
    and the environment."""
    path = Path(directory) / config_module.DEFAULT_ENV_FILE
    return config_module.build_config(config_module.layered_values(path if path.is_file() else None), path)


# -- the routes -----------------------------------------------------------------------------------------------

class DraftBody(BaseModel):
    """The fields of the draft that change; the ones left out stay as they are."""

    model_config = ConfigDict(extra="forbid")
    url: Optional[str] = Field(default=None, max_length=2048)
    site: Optional[str] = Field(default=None, max_length=128)
    api_key: Optional[str] = Field(default=None, max_length=512)
    verify: Optional[Literal["true", "pin", "false"]] = None
    fingerprint: Optional[str] = Field(default=None, max_length=200, description="To accept the fetched certificate")
    confirm: Optional[str] = Field(default=None, max_length=200, description="The sentence that turns checking off")
    notify: Optional[Dict[str, Optional[str]]] = Field(
        default=None, max_length=32, description="NOTIFY_* settings; a blank value removes one")


class FinishBody(BaseModel):
    """The first administrator, needed while there is none."""

    model_config = ConfigDict(extra="forbid")
    username: Optional[str] = Field(default=None, max_length=256)
    password: Optional[str] = Field(default=None, max_length=1024)


def _refusal(error: tlsprobe.ProbeError) -> ApiError:
    status = 422 if error.reason in _ADDRESS_REASONS else 502
    return ApiError(status, "controller_unavailable", str(error), reason=error.reason)


def _sites(state: SetupState, config: Config) -> List[Dict[str, Any]]:
    """The controller's sites for the picker (name, internal reference, id), or none when they cannot be read: the
    connection itself was just proven by the checks, and a site can still be typed."""
    try:
        sites = info_document(state.client(config)).data["sites"]
    except UniFiAPIError:
        return []
    return [{key: printable(str(site.get(key) or "")) for key in ("name", "ref", "id")} for site in sites]


def router() -> APIRouter:
    api = APIRouter(prefix=f"{API}/setup", tags=["setup"], dependencies=[Depends(setup_access)])

    @api.get("/status", summary="The state of the setup")
    @setup_ok
    def setup_status(request: Request) -> Dict[str, Any]:
        return _state(request, "status").snapshot()

    @api.post("/draft", summary="Change the draft of the settings")
    @setup_ok
    def setup_draft(request: Request, body: DraftBody) -> Dict[str, Any]:
        state = _state(request, "draft")
        changed = state.update(body)
        if changed:
            audit_event(request, "setup.draft_changed", _actor(request), settings=",".join(changed),
                        url=state.draft.url)
        return {**state.snapshot(), "changed": changed}

    @api.post("/certificate", summary="Fetch the certificate the controller shows")
    @setup_ok
    def setup_certificate(request: Request) -> Dict[str, Any]:
        state = _state(request, "certificate")
        with state.lock:
            url = state.require(key=False).url
        try:
            found = tlsprobe.fetch_certificate(url, state.allow_public, resolver=state.resolver)
        except tlsprobe.ProbeError as error:
            audit_event(request, "setup.certificate_fetched", _actor(request), url=url, outcome=error.reason)
            raise _refusal(error) from None
        problem = ""
        try:
            tlsprobe.check_pin(url, found.pem, state.allow_public, resolver=state.resolver)
        except tlsprobe.ProbeError as error:
            problem = error.reason
        with state.lock:
            if state.draft.url == url:                   # the address may have changed while the controller answered
                state.draft.certificate, state.draft.problem = found, problem
        audit_event(request, "setup.certificate_fetched", _actor(request), url=url, outcome="fetched",
                    fingerprint=found.fingerprint, usable=not problem)
        return state.snapshot()

    @api.post("/connection", summary="Test the connection with the draft")
    @setup_ok
    def setup_connection(request: Request) -> Dict[str, Any]:
        state = _state(request, "connection")
        with state.config() as (config, generation):
            try:
                tlsprobe.resolve_target(config.controller_url, state.allow_public, state.resolver)
            except tlsprobe.ProbeError as error:
                raise _refusal(error) from None
            checks = check_controller(config, state.client)
            ok = not exit_failed(checks)
            sites = _sites(state, config) if ok else []
        with state.lock:
            if state.generation == generation:              # a draft changed meanwhile was not the one tested
                state.draft.connection_ok = ok
        audit_event(request, "setup.connection_tested", _actor(request), url=config.controller_url, ok=ok)
        return {"ok": ok, "sites": sites,
                "checks": [{**c.to_dict(), "message": state.scrub(c.message), "fix": state.scrub(c.fix)}
                           for c in checks]}

    @api.post("/preview", summary="Run the health checks on the draft")
    @setup_ok
    def setup_preview(request: Request) -> Dict[str, Any]:
        state = _state(request, "preview")
        areas = [name for name in AREA_NAMES if name != "events"]
        with state.config() as (config, _):
            try:
                tlsprobe.resolve_target(config.controller_url, state.allow_public, state.resolver)
                document = diagnose_document(state.client(config), config.site, DiagnoseSettings(), areas,
                                             echo=False)
            except tlsprobe.ProbeError as error:
                raise _refusal(error) from None
            except UniFiAPIError as error:
                raise from_controller(error) from None
        data = document.data
        audit_event(request, "setup.preview_run", _actor(request), url=config.controller_url)
        return {"areas": data["areas"], "summary": data["summary"],
                "findings": data["findings"][:MAX_PREVIEW_FINDINGS], "total": len(data["findings"]),
                "warnings": [state.scrub(w) for w in document.warnings]}

    @api.post("/notifications", summary="Dry run of the notification destinations of the draft")
    @setup_ok
    def setup_notifications(request: Request) -> Dict[str, Any]:
        state = _state(request, "notifications")
        with state.config() as (config, _):
            checks = check_notifications(config)
        audit_event(request, "setup.notifications_checked", _actor(request), url=config.controller_url)
        return {"checks": [{**c.to_dict(), "message": state.scrub(c.message), "fix": state.scrub(c.fix)}
                           for c in checks]}

    @api.post("/finish", summary="Save the settings, create the administrator and leave the setup mode")
    @setup_ok
    def setup_finish(request: Request, body: FinishBody) -> Dict[str, Any]:
        state = _state(request, "finish")
        auth = request.app.state.auth
        directory = Path(request.app.state.state_dir or ".")
        with state.lock:
            if state.mode is None:
                raise ApiError(409, "already_set_up", "This server is set up already.")
            try:
                needs_admin = not administrator_exists(auth.accounts.store)
                credentials = _credentials(auth.accounts.store, body, needs_admin)
            except AccountError:
                raise ApiError(500, "accounts_unreadable", "The accounts file cannot be read; see the server log.") \
                    from None
            written: List[Dict[str, str]] = []
            if state.mode == MODE_SETUP:
                if state.require().connection_ok is not True:
                    raise ApiError(409, "not_tested", "Test the connection with these settings first.")
                fallback = _write_settings(state, directory, written)
                if fallback is not None:
                    audit_event(request, "setup.finish_fallback", _actor(request), reason=fallback["reason"])
                    return fallback
            if credentials is not None:
                def record(user: Any) -> None:
                    auth.audit.write("user.added", _actor(request), user=user.username, role=user.role,
                                     address=address_of(request))

                try:
                    auth.accounts.store.add(credentials[0], "admin", credentials[1], on_change=record)
                except AccountError:
                    raise ApiError(500, "admin_not_created", "The settings are saved but the administrator could not "
                                   "be created: run `hlp web-user add NAME --role admin` and restart.") from None
            try:
                config = (state.reload or (lambda: _reload_default(directory)))()
            except ConfigError:
                raise ApiError(500, "reload_failed", "The settings are saved but could not be loaded: restart the "
                               "server.") from None
            request.app.state.service = (state.service_factory or ControllerService)(config)
            request.app.state.config = config
            logs.register_secrets(*config.secret_values())
            state.mode, state.draft = None, Draft(site=state.draft.site)
            state.generation += 1
        audit_event(request, "setup.finished", _actor(request), url=config.controller_url,
                    admin_created=credentials is not None)
        return {"finished": True, "written": written, "admin_created": credentials is not None}

    return api
