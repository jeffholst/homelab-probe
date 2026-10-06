"""Notifications: tell the owner when `diagnose` finds something new, worse or fixed.

This is the one place the tool sends data *off the machine* (never to the controller), so it is
deliberately small and opt-in:

* nothing is sent unless ``diagnose --notify`` is given and a destination is configured;
* the destination URLs, tokens and the mail account are secrets, read from the environment, and never appear in
  any message, error or log line (errors say only which destination and a fixed reason);
* only the finding identity, severity and text are sent, never the API key, the controller
  address or a site id, and ``--notify-redact`` replaces names, addresses and MACs with the
  generic description of the check;
* a message goes out only when something changed since the last notified run, remembered in an
  owner-only state file, so a persistent problem is not re-sent every run.

The planning (`plan`) and the wording (`render_text`, `render_payload`) are pure functions; the
network calls (one HTTPS POST per ntfy or webhook destination, one SMTP session for email) are in `send`.
Email uses verified STARTTLS or implicit TLS by default. Plain SMTP is available only with the lab opt-in,
and a password is never sent over an unencrypted connection.
"""

import contextlib
import json
import logging
import os
import smtplib
import ssl
import time
from collections import Counter
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from .config import Config, ConfigError, SmtpSettings
from .diagnose import CODES, CRITICAL, INFO, SEVERITY_ORDER, WARNING, Finding, area_of
from .logs import log_event
from .util import LockCancelled, LockTimeout, file_lock, printable, site_key

_log = logging.getLogger(__name__)

STATE_VERSION = 1
STATE_DIR = "snapshots"                                 # the git-ignored directory that holds saved inventories
STATE_FILE_NAME = "notify-state.json"
# Before there was one state per site the file was shared; it is still read (for the site "default" only) when the
# site has none of its own yet, and is never changed or removed.
LEGACY_SITE_REF = "default"
MAX_LINES = 20                                          # events spelled out in one message
KIND_ORDER = {"new": 0, "worsened": 1, "reminder": 2, "recovered": 3}
KIND_WORD = {"new": "NEW", "worsened": "WORSE", "reminder": "STILL", "recovered": "RECOVERED"}


@dataclass(frozen=True)
class Destination:
    kind: str                                  # "ntfy", "webhook" or "email"
    url: str = field(default="", repr=False)   # a secret: a topic name or a token is part of it
    token: str = field(default="", repr=False)
    smtp: Optional[SmtpSettings] = field(default=None, repr=False)     # for "email"


@dataclass(frozen=True)
class Event:
    kind: str                                  # new, worsened, reminder or recovered
    severity: str
    code: str
    subject: str
    message: str = ""


def destinations_from_config(config: Config) -> List[Destination]:
    found = []
    if config.notify_ntfy_url:
        found.append(Destination("ntfy", config.notify_ntfy_url, config.notify_ntfy_token))
    if config.notify_webhook_url:
        found.append(Destination("webhook", config.notify_webhook_url, config.notify_webhook_token))
    if config.notify_smtp is not None:
        found.append(Destination("email", smtp=config.notify_smtp))
    return found


# -- planning ------------------------------------------------------------------------------

def identity(code: str, subject: str) -> str:
    """The stable key of a finding: its check and its subject, never its wording (counts change)."""
    return f"{code or 'unknown'}|{subject}"


def split_identity(key: str) -> Tuple[str, str]:
    code, _, subject = key.partition("|")
    return code, subject


def empty_state() -> Dict[str, Any]:
    return {"version": STATE_VERSION, "active": {}}


def _ran(key: str, areas: Optional[Collection[str]]) -> bool:
    """Did the checks that could report ``key`` run? Always true for a full run and for a state entry whose
    code belongs to no area (nothing says it was skipped)."""
    area = area_of(split_identity(key)[0])
    return areas is None or area is None or area in areas


def plan(findings: List[Finding], state: Dict[str, Any], now: float, min_severity: str = WARNING,
         repeat_hours: float = 24, areas: Optional[Collection[str]] = None) -> Tuple[List[Event], Dict[str, Any]]:
    """What to tell the owner, and the state to save once it was delivered.

    A finding at or above ``min_severity`` that is not in the state is **new**; one that got worse
    than when it was last notified is **worsened**; a critical one still unresolved
    ``repeat_hours`` after it was last notified is a **reminder** (0 turns reminders off); and a
    notified one that is gone, or fell below ``min_severity``, is **recovered**. Everything else
    is quiet, so an unchanged situation sends nothing.

    ``areas`` are the areas of checks that ran (``diagnose --only/--skip``; None: all). A notified finding
    of an area that did not run is neither current nor gone: it is not **recovered**, and its entry is kept as it
    was, so a partial run never announces the recovery of problems it did not look for.
    """
    floor = SEVERITY_ORDER[min_severity]
    current: Dict[str, Finding] = {}
    for f in findings:
        if f.severity not in SEVERITY_ORDER or SEVERITY_ORDER[f.severity] > floor:
            continue
        key = identity(f.code, f.subject)
        if key not in current or SEVERITY_ORDER[f.severity] < SEVERITY_ORDER[current[key].severity]:
            current[key] = f                                   # the worst one speaks for the identity

    active = {}
    for key, entry in (state.get("active") or {}).items():
        if not isinstance(entry, dict) or entry.get("severity") not in SEVERITY_ORDER:
            continue
        try:
            normalized = {**entry, "last_notified": float(entry.get("last_notified") or 0)}
        except (TypeError, ValueError, OverflowError):
            continue
        active[key] = normalized
    events: List[Event] = []
    new_active: Dict[str, Any] = {}
    for key, f in current.items():
        previous = active.get(key)
        notified = {"severity": f.severity, "first_notified": now, "last_notified": now}
        if previous is None:
            events.append(Event("new", f.severity, f.code, f.subject, f.message))
            new_active[key] = notified
            continue
        last = float(previous.get("last_notified") or 0)
        entry = {"severity": f.severity, "first_notified": previous.get("first_notified", now), "last_notified": last}
        if SEVERITY_ORDER[f.severity] < SEVERITY_ORDER[previous["severity"]]:
            events.append(Event("worsened", f.severity, f.code, f.subject, f.message))
            entry["last_notified"] = now
        elif f.severity == CRITICAL and repeat_hours > 0 and now - last >= repeat_hours * 3600:
            events.append(Event("reminder", f.severity, f.code, f.subject, f.message))
            entry["last_notified"] = now
        new_active[key] = entry
    for key, previous in active.items():
        if not _ran(key, areas):
            new_active[key] = previous                         # not checked this time: left exactly as it is
        elif key not in current:
            code, subject = split_identity(key)
            events.append(Event("recovered", previous["severity"], code, subject))
    events.sort(key=lambda e: (KIND_ORDER[e.kind], SEVERITY_ORDER[e.severity], e.subject, e.code))
    return events, {"version": STATE_VERSION, "active": new_active}


def baseline(findings: List[Finding], now: float, min_severity: str = WARNING,
             areas: Optional[Collection[str]] = None, state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The state in which every current finding counts as already reported (nothing is sent). After a run of
    some areas only (``areas``), what ``state`` holds for the other areas is kept."""
    saved = plan(findings, empty_state(), now, min_severity)[1]
    for key, entry in ((state or {}).get("active") or {}).items():
        if isinstance(entry, dict) and not _ran(key, areas):
            saved["active"][key] = entry
    return saved


# -- wording -----------------------------------------------------------------------------------

def _describe(e: Event) -> str:
    return CODES.get(e.code, e.code or "a problem")


def render_text(events: List[Event], redact: bool = False) -> Tuple[str, str]:
    """(title, body) for a plain-text channel. The title is ASCII (it travels as an HTTP header);
    the body has every name cleaned with ``printable``. With ``redact`` the body holds only the
    generic description of each check and a count, never a name, an address or a MAC."""
    problems = sum(e.kind != "recovered" for e in events)
    recovered = len(events) - problems
    bits = ([f"{problems} problem(s)"] if problems else []) + ([f"{recovered} recovered"] if recovered else [])
    title = "hlp: " + ", ".join(bits)

    lines: List[str] = []
    if redact:
        counts = Counter((e.kind, e.severity, e.code) for e in events)
        for (kind, severity, code), n in sorted(counts.items(), key=lambda i: (
                KIND_ORDER[i[0][0]], SEVERITY_ORDER[i[0][1]], i[0][2])):
            what = CODES.get(code, code or "a problem")
            lines.append(f"[{severity.upper()}] {KIND_WORD[kind]}  {what}" + (f" (x{n})" if n > 1 else ""))
    else:
        for e in events:
            subject = printable(e.subject)
            if e.kind == "recovered":
                lines.append(f"[OK] {KIND_WORD[e.kind]}  {subject} ({e.code})")
            else:
                tail = " (reminder: still unresolved)" if e.kind == "reminder" else ""
                lines.append(f"[{e.severity.upper()}] {KIND_WORD[e.kind]}  {subject}: {printable(e.message)}{tail}")
    if len(lines) > MAX_LINES:
        lines = lines[:MAX_LINES] + [f"... and {len(lines) - MAX_LINES} more"]
    return title, "\n".join(lines)


def render_payload(events: List[Event], redact: bool = False) -> Dict[str, Any]:
    """The JSON a generic webhook receives. ``text`` is the same message as the plain-text channels."""
    title, body = render_text(events, redact)
    items = []
    for e in events:
        item: Dict[str, Any] = {"event": e.kind, "severity": e.severity, "code": e.code,
                                "description": _describe(e)}
        if not redact:
            item.update(subject=e.subject, message=e.message)
        items.append(item)
    return {"source": "homelab-probe", "version": 1, "redacted": redact, "title": title,
            "text": f"{title}\n{body}", "events": items}


def _priority(events: List[Event]) -> Tuple[str, str]:
    """(ntfy priority, ntfy tag) for the worst thing in the message."""
    worst = min((SEVERITY_ORDER[e.severity] for e in events if e.kind != "recovered"), default=None)
    if worst == SEVERITY_ORDER[CRITICAL]:
        return "5", "rotating_light"
    if worst == SEVERITY_ORDER[WARNING]:
        return "4", "warning"
    if worst == SEVERITY_ORDER[INFO]:
        return "3", "information_source"
    return "3", "white_check_mark"                      # only recoveries


# -- sending -----------------------------------------------------------------------------------

def _email_failure(error: BaseException) -> str:
    """A fixed phrase for an SMTP failure. The library's own text is never used: it can hold the host, the user
    name or a line the server sent back. The order matters, because ``SMTPException`` and ``ssl.SSLError`` are
    both ``OSError`` subclasses, as are timeouts."""
    if isinstance(error, smtplib.SMTPAuthenticationError):
        return "authentication failed"
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        return "recipient refused"
    if isinstance(error, smtplib.SMTPSenderRefused):
        return "sender refused"
    if isinstance(error, smtplib.SMTPNotSupportedError):
        return "the server does not offer the required security or login"
    if isinstance(error, ssl.SSLError):
        return "TLS error"
    if isinstance(error, TimeoutError):
        return "timed out"
    if isinstance(error, smtplib.SMTPServerDisconnected):
        return "server disconnected"
    if isinstance(error, smtplib.SMTPException):
        return "server rejected the message"
    if isinstance(error, ValueError):
        return "the message could not be built (an invalid address)"
    return "connection error"


def _email_message(smtp: SmtpSettings, title: str, body: str) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = title.encode("ascii", "replace").decode("ascii").replace("\r", " ").replace("\n", " ")
    message["From"] = smtp.sender
    message["To"] = ", ".join(smtp.recipients)
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=smtp.sender.rsplit("@", 1)[1])   # not this machine's host name
    message.set_content(body)
    return message


def _send_email(smtp: SmtpSettings, title: str, body: str, timeout: float,
                plain: Callable[..., Any], implicit_tls: Callable[..., Any]) -> Tuple[bool, str]:
    """One plain-text message to every recipient over one connection: ``(delivered, reason)``.

    The connection is verified TLS (``ssl.create_default_context`` checks the certificate and the host name), a
    login is only attempted over it, and a password is refused over anything else even if the settings were
    built by hand. Delivered means at least one recipient accepted it; the reason says when some did not."""
    if smtp.password and smtp.security == "none":
        return False, "a password is never sent without encryption"
    context = ssl.create_default_context()
    hello = smtp.sender.rsplit("@", 1)[1]                  # the name we give the server; not this machine's host name
    try:
        connection = (implicit_tls(smtp.host, smtp.port, local_hostname=hello, timeout=timeout, context=context)
                      if smtp.security == "ssl" else
                      plain(smtp.host, smtp.port, local_hostname=hello, timeout=timeout))
        with connection:
            if smtp.security == "starttls":
                connection.starttls(context=context)
            if smtp.user:
                connection.login(smtp.user, smtp.password)
            refused = connection.send_message(_email_message(smtp, title, body), from_addr=smtp.sender,
                                              to_addrs=list(smtp.recipients))
    except (OSError, smtplib.SMTPException, ValueError) as error:    # SMTPException is an OSError; listed for clarity
        return False, _email_failure(error)
    if refused:
        accepted = len(smtp.recipients) - len(refused)
        return True, f"sent to {accepted} of {len(smtp.recipients)} recipients (the others were refused)"
    return True, "sent"


def _report(kind: str, outcome: Tuple[bool, str], started: float, trace: Optional[Callable[[str], None]]) -> None:
    """Log how one destination went: the kind, a fixed reason and the time, never the URL, a recipient or the
    message. ``trace`` (when given) gets the same line as text."""
    delivered, reason = outcome
    millis = (time.perf_counter() - started) * 1000
    line = f"notify {kind} -> {reason} ({millis:.0f} ms)"
    log_event(_log, logging.INFO if delivered else logging.WARNING, "notify.delivery", line,
              destination=kind, delivered=delivered, reason=reason, duration_ms=round(millis))
    if trace is not None:
        trace(line)


TEST_TITLE = "hlp: test notification"
TEST_BODY = "This is a test message from Homelab Probe. If you can read it, delivery to this destination works."


def send(destinations: List[Destination], events: List[Event], redact: bool, timeout: float,
         post: Optional[Callable[..., Any]] = None,
         trace: Optional[Callable[[str], None]] = None,
         smtp_plain: Optional[Callable[..., Any]] = None,
         smtp_ssl: Optional[Callable[..., Any]] = None, test: bool = False) -> List[Tuple[str, bool, str]]:
    """Send the message to every destination: ``[(kind, delivered, reason)]``.

    ``reason`` is a fixed phrase (``HTTP 403``, ``timed out``, ...), never the library's text,
    because that text contains the URL, which is a secret. Redirects are not followed (a
    redirect could carry the token to another host) and TLS is always verified. Email goes out as one
    plain-text message over one SMTP session (``smtp_plain`` and ``smtp_ssl`` stand in for ``smtplib.SMTP`` and
    ``smtplib.SMTP_SSL`` in tests). With ``test`` the message is the fixed test message (no events; a webhook gets
    ``"test": true`` and an empty ``events`` list), which an administrator asks for to check a destination.
    """
    post = post or requests.post
    if test:
        title, body, priority, tag = TEST_TITLE, TEST_BODY, "3", "white_check_mark"
    else:
        title, body = render_text(events, redact)
        priority, tag = _priority(events)
    results = []
    for dest in destinations:
        started = time.perf_counter()
        if dest.kind == "email" and dest.smtp is not None:
            outcome = _send_email(dest.smtp, title, body, timeout, smtp_plain or smtplib.SMTP,
                                  smtp_ssl or smtplib.SMTP_SSL)
            results.append((dest.kind, *outcome))
            _report(dest.kind, outcome, started, trace)
            continue
        headers: Dict[str, str] = {}
        if dest.token:
            headers["Authorization"] = f"Bearer {dest.token}"
        if dest.kind == "ntfy":
            headers.update({"Title": title.encode("ascii", "replace").decode("ascii"),
                            "Priority": priority, "Tags": tag})
            kwargs: Dict[str, Any] = {"data": body.encode("utf-8")}
        else:
            kwargs = {"json": {"source": "homelab-probe", "version": 1, "redacted": False, "title": title,
                               "text": f"{title}\n{body}", "events": [], "test": True}
                      if test else render_payload(events, redact)}
        try:
            response = post(dest.url, headers=headers, timeout=timeout, allow_redirects=False, verify=True, **kwargs)
        except requests.exceptions.SSLError:
            outcome = (False, "TLS certificate verification failed")
        except requests.exceptions.Timeout:
            outcome = (False, "timed out")
        except requests.exceptions.ConnectionError:
            outcome = (False, "connection error")
        except requests.exceptions.RequestException:
            outcome = (False, "request error")
        else:
            status = getattr(response, "status_code", 0)
            outcome = (200 <= status < 300, f"HTTP {status}")
        results.append((dest.kind, *outcome))
        _report(dest.kind, outcome, started, trace)
    return results


# -- the state file ------------------------------------------------------------------------------

def state_path_for(site: Dict[str, Any], base: Path = Path(STATE_DIR)) -> Path:
    """The default state file of ``site`` (a dict with its ``id``): ``snapshots/<site id>/notify-state.json``."""
    return base / site_key(site.get("id")) / STATE_FILE_NAME


def check_state_site(state: Dict[str, Any], site: Dict[str, Any], path: Path) -> None:
    """Refuse a state that says it is from another site: its findings would look fixed in this one (and the other way
    round). A state that does not say (made before sites were recorded) is accepted and stamped when it is saved."""
    recorded = state.get("site")
    if recorded is not None and recorded != str(site.get("id") or ""):
        raise ConfigError(f"{path} remembers the findings of another site; give this site a state file of its own "
                          f"(the default is {state_path_for(site)})")


def load_state(path: Path) -> Tuple[Dict[str, Any], str]:
    """(state, warning). A missing file is an empty state; an unreadable or damaged one is too, with a
    warning (the next save replaces it), so a broken file never stops the checks."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return empty_state(), ""
    except OSError as e:
        return empty_state(), f"cannot read the notification state {path}: {e.strerror or e}; starting empty"
    try:
        state = json.loads(text)
    except ValueError:
        return empty_state(), f"{path} is not valid JSON; starting with an empty notification state"
    if not isinstance(state, dict) or state.get("version") != STATE_VERSION or not isinstance(
            state.get("active", {}), dict):
        return empty_state(), f"{path} is not a notification state this version understands; starting empty"
    return state, ""


def save_state(path: Path, state: Dict[str, Any]) -> None:
    """Write the state owner-only, replacing the old file in one step."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        os.fchmod(f.fileno(), 0o600)
        json.dump(state, f, indent=2)
        f.write("\n")
    os.replace(temporary, path)


# -- one notification step, for the command line and the scheduler -----------------------------------------------

LOCK_WAIT_SECONDS = 120.0       # how long a run waits for another run's turn with the state


@dataclass
class Outcome:
    """What one notification step did. ``kind`` is ``baseline`` (the current findings were recorded as already
    reported), ``nothing`` (nothing new, worse or fixed), ``dry_run``, ``sent`` or ``cancelled`` (asked to stop first);
    ``results`` is one (destination kind, delivered, reason) per destination that was tried; ``undelivered`` is true
    when a message had to be sent and every destination failed."""

    kind: str
    results: List[Tuple[str, bool, str]] = field(default_factory=list)
    undelivered: bool = False
    events: int = 0


def load_site_state(explicit: Optional[Path], site: Dict[str, str],
                    base: Path = Path(STATE_DIR)) -> Tuple[Path, Dict[str, Any], str, bool]:
    """(where the state is kept, the state, a warning, whether there was a state at all) for ``site``. The default file
    is the site's own; a state that belongs to another site is refused. The shared file of earlier versions is read,
    for the site ``default`` only, while that site has no file of its own (it is never changed: the next save writes
    the new file). ``base`` is the directory that holds the states (``snapshots/`` of the working directory for the
    command line, that of the data directory for the server)."""
    path = explicit or state_path_for(site, base)
    legacy = base / STATE_FILE_NAME
    if explicit is None and not path.exists() and site.get("ref") == LEGACY_SITE_REF and legacy.exists():
        found = legacy
    else:
        found = path
    existed = found.exists()
    state, problem = load_state(found)
    check_state_site(state, site, path)
    return path, state, problem, existed


def process(findings: List[Finding], *, config: Config, settings: Any, site: Dict[str, str],
            state_file: Optional[Path] = None, base: Path = Path(STATE_DIR), minimum: str = WARNING,
            areas: Optional[List[str]] = None,
            redact: bool = False, dry_run: bool = False, baseline_only: bool = False, baseline_if_new: bool = False,
            report: Callable[[str], None] = lambda message: None,
            warn: Callable[[str], None] = lambda message: None,
            cancelled: Callable[[], bool] = lambda: False) -> Outcome:
    """The notification step: what is new, worse or fixed since the last run is sent to every destination and
    remembered. Everything from reading the state to saving it happens under one lock on the state, so a scheduler
    and a cron job (or two of either) never both announce the same finding. ``baseline_only`` records the current
    findings as already reported; ``baseline_if_new`` does so when there was no state yet (a first run would otherwise
    announce everything). ``report`` receives the lines a person is told, ``warn`` a damaged-state warning. Raises
    ``ConfigError`` for a state that cannot be saved, or one that stays locked by another run. ``cancelled`` is asked
    while waiting for the lock and again before anything is sent or written: when it says yes the step ends as
    ``cancelled`` with nothing sent and the state untouched (the scheduler uses it to stop)."""
    path = state_file or state_path_for(site, base)
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(file_lock(path.with_name(path.name + ".lock"), LOCK_WAIT_SECONDS, cancelled))
        except LockCancelled:
            return Outcome("cancelled")
        except LockTimeout:
            raise ConfigError(f"the notification state {path} is in use by another run that did not finish in "
                              f"{LOCK_WAIT_SECONDS:g} seconds; nothing was sent") from None
        except OSError as e:
            raise ConfigError(f"the notification state in {path.parent} cannot be locked: {e.strerror or e}") from e
        return _locked_step(findings, config, settings, site, state_file, base, minimum, areas, redact, dry_run,
                            baseline_only, baseline_if_new, report, warn, cancelled)


def _locked_step(findings: List[Finding], config: Config, settings: Any, site: Dict[str, str],
                 state_file: Optional[Path], base: Path, minimum: str, areas: Optional[List[str]], redact: bool,
                 dry_run: bool,
                 baseline_only: bool, baseline_if_new: bool, report: Callable[[str], None],
                 warn: Callable[[str], None], cancelled: Callable[[], bool]) -> Outcome:
    if cancelled():
        return Outcome("cancelled")
    state_path, state, problem, existed = load_site_state(state_file, site, base)
    if problem:
        warn(problem)
    now = time.time()
    if baseline_only or (baseline_if_new and not existed):
        saved = {**baseline(findings, now, minimum, areas, state), "site": site["id"]}
        try:
            save_state(state_path, saved)
        except OSError as e:
            raise ConfigError(
                f"the notification baseline could not be saved to {state_path}: {e.strerror or e}") from e
        report(f"Notification baseline saved: {len(saved['active'])} current finding(s) count as already reported")
        return Outcome("baseline")
    events, new_state = plan(findings, state, now, minimum, settings.notify_repeat_hours, areas)
    new_state = {**new_state, "site": site["id"]}
    if not events:
        if new_state != state:
            try:
                save_state(state_path, new_state)          # e.g. a finding improved but is still reported
            except OSError as e:
                raise ConfigError(
                    f"the notification state could not be saved to {state_path}: {e.strerror or e}") from e
        report("Notification: nothing new, worse or fixed since the last notified run")
        return Outcome("nothing")
    if dry_run:
        title, body = render_text(events, redact)
        report(f"Notification dry run (nothing sent, state unchanged): {title}\n{body}")
        return Outcome("dry_run", events=len(events))
    if cancelled():
        return Outcome("cancelled")                                 # asked to stop while planning: send nothing
    results = send(destinations_from_config(config), events, redact, config.timeout)
    for kind, delivered, reason in results:
        report(f"Notification to {kind}: " + ("sent" if delivered else f"FAILED ({reason})"))
    delivered_somewhere = any(ok for _, ok, _ in results)
    if delivered_somewhere:
        try:
            save_state(state_path, new_state)
        except OSError as e:
            raise ConfigError(f"the notification was sent but its state could not be saved to {state_path}: "
                              f"{e.strerror or e}; it will be sent again next run") from e
    return Outcome("sent", results, not delivered_somewhere, len(events))
