"""Diagnostics: the ``homelab_probe`` logger tree, its three output formats and the filter every record passes.

Command output is not logging (it goes through ``commands.say``); this module is for what the tool says *about*
itself: a degraded read, a request to the controller, the outcome of a notification. Library code takes a logger
with ``logging.getLogger(__name__)`` and logs through ``log_event`` so each record has a stable ``event`` name
(listed in ``EVENTS`` and in ``docs/logging.md``, never renamed or reused).

Everything goes to stderr, through a handler made here, so the redaction filter is always on it:

* ``cli``: what the command line has always printed: ``Warning: ...`` for a degraded read and, with ``--verbose``,
  ``[verbose] ...`` lines. This is the default of every command, so output does not change.
* ``text``: one human-readable line per record with a timestamp, the level, the event and ``key=value`` fields.
* ``json``: one JSON object per line (``ts``, ``level``, ``logger``, ``msg``, ``event``, ``request_id``, ``user``,
  ``site`` and the event's own fields), for a log collector.

A record is always one clean line: text goes through ``util.printable`` and a limit on its length, values that
are secrets (the registered ones, and the shape of a header, a password or a token) become ``[redacted]``, and an
exception contributes only its type and message. Personal data (client names, MACs, addresses) is logged only at
DEBUG; INFO and above carry counts, codes and fixed words.
"""

import contextvars
import json
import logging
import re
import sys
import threading
import uuid
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from typing import Any, Callable, ContextManager, Dict, Iterator, List, Optional, TextIO
from urllib.parse import quote, quote_plus

from .util import printable

ROOT = "homelab_probe"
MAX_LINE = 2000                     # characters in one rendered record
MIN_SECRET_LENGTH = 6               # a shorter registered secret would garble every line; real keys are longer
REDACTED = "[redacted]"
LEVEL_WORDS = ("DEBUG", "INFO", "WARNING", "ERROR")
FORMAT_WORDS = ("text", "json")

# The ``event`` of every record the tool emits: name -> what it means. ``tests/test_logs.py`` checks that the
# source uses exactly these names and that ``docs/logging.md`` lists each one.
EVENTS: Dict[str, str] = {
    "warning": "A message the command also prints as `Warning:` (an optional read failed, a setting is loose, "
               "a state file was damaged).",
    "http.request": "One attempt to read from the controller (DEBUG): method, path, outcome, milliseconds.",
    "http.retry": "A read is about to be retried after a pause (DEBUG).",
    "snapshot.read": "What one collection of the controller's data read (DEBUG): counts only.",
    "run.settings": "Which settings a run uses (DEBUG, with `--verbose`): the controller address, the site, limits.",
    "run.summary": "How many requests a run made and how long they took (DEBUG, with `--verbose`).",
    "notify.delivery": "The outcome of one notification destination (INFO delivered, WARNING failed): the kind, "
                       "a fixed reason, milliseconds; never the message, the URL or a recipient.",
    "audit.event": "A change to the web accounts was made (INFO): the action, who did it and which user; "
                   "never a password.",
    "server.start": "The web server started listening (INFO): the address, the port, whether it is a demo.",
    "server.cache": "One read of the controller by the web server (DEBUG): answered from the cache (hit), read (miss), "
                    "served stale because the read failed, or refused because it just failed (coalesced).",
    "server.request": "One request to the web server (INFO): the method, the route template (never the path), the "
                      "status and the milliseconds.",
    "scheduler.start": "The scheduler of the web server started (INFO): how often it runs `diagnose`, takes a "
                       "snapshot and how many snapshots it keeps.",
    "scheduler.run": "One run of a scheduled job ended (INFO ok or skipped, WARNING failed): the job, its run id, the "
                     "result, a fixed reason, the milliseconds and counts; never a finding, a name or a message.",
    "watch.pass": "One pass of `diagnose --watch` finished (INFO): how many findings, whether the read was complete.",
    "watch.unavailable": "A pass of `diagnose --watch` could not read the controller or got only part of its data "
                         "(WARNING).",
}

_log = logging.getLogger(__name__)

# -- context: who and what a record belongs to -------------------------------------------------------------

_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("homelab_probe_request_id", default="")
_user: contextvars.ContextVar[str] = contextvars.ContextVar("homelab_probe_user", default="")
_site: contextvars.ContextVar[str] = contextvars.ContextVar("homelab_probe_site", default="")
_sink: contextvars.ContextVar[Optional[List[str]]] = contextvars.ContextVar("homelab_probe_sink", default=None)
_quiet: contextvars.ContextVar[bool] = contextvars.ContextVar("homelab_probe_quiet", default=False)


def new_id() -> str:
    """A short random id for a run or a request (twelve hex digits)."""
    return uuid.uuid4().hex[:12]


@contextmanager
def bind(request_id: Optional[str] = None, user: Optional[str] = None, site: Optional[str] = None) -> Iterator[None]:
    """Every record logged inside the block (also by the threads of a parallel read, which run in a copy of the
    context) carries these values. A value that is ``None`` is left as it was."""
    tokens = []
    for var, value in ((_request_id, request_id), (_user, user), (_site, site)):
        if value is not None:
            tokens.append((var, var.set(value)))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def current_request_id() -> str:
    return _request_id.get()


# -- redaction ---------------------------------------------------------------------------------------------

_secrets: List[str] = []             # longest first, so a secret that contains another is replaced whole
_secrets_lock = threading.Lock()

_HEADER = re.compile(
    r"(?i)\b(authorization|proxy-authorization|cookie|set-cookie|x-api-key)([\"']?\s*[:=]\s*)(.*)")
_SCHEME = re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}")
_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@]+@")
_NAMES = (r"(?:pass(?:word|wd|phrase)?|secret|(?:api[_-]?|access[_-]?|auth[_-]?|setup[_-]?|session[_-]?|"
          r"refresh[_-]?|csrf[_-]?)?(?:key|token)|session(?:[_-]?id)?|sid|credentials?)")
_PAIR = re.compile(r"(?i)(\b(?:[\w.-]*[_.-])?" + _NAMES + r"[\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&\"'})]+)")
_SENSITIVE_FIELD = re.compile(
    r"(?i)(?:^|[_.-])(?:pass(?:word|wd|phrase)?|secret|token|api[_-]?key|authorization|cookie|set[_-]cookie|"
    r"session(?:[_-]?id)?|sid|csrf|credentials?)(?:$|[_.-])")


def register_secrets(*values: Any) -> None:
    """Values that must never appear in a log line (the API key, notification URLs and tokens, a password, a
    session id, the setup token). The value is also hidden in its URL-encoded and JSON-escaped spellings.
    Values shorter than ``MIN_SECRET_LENGTH`` are ignored, as are non-strings."""
    found: List[str] = []
    for value in values:
        if isinstance(value, str) and len(value.strip()) >= MIN_SECRET_LENGTH:
            raw = value.strip()
            found += [raw, quote(raw, safe=""), quote_plus(raw), json.dumps(raw)[1:-1]]
    with _secrets_lock:
        merged = set(_secrets) | {text for text in found if text}
        _secrets[:] = sorted(merged, key=lambda text: (-len(text), text))


def scrub(value: Any, limit: int = MAX_LINE) -> str:
    """``value`` as one safe line: control characters and line breaks gone, every registered secret, header
    value, ``Bearer`` token, ``user:password@`` and ``password=...`` style pair replaced by ``[redacted]``, cut to
    ``limit`` characters. Applying it twice gives the same text."""
    text = printable(value)
    with _secrets_lock:
        secrets = tuple(_secrets)
    for secret in secrets:
        text = text.replace(secret, REDACTED)
    text = _HEADER.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    text = _SCHEME.sub(REDACTED, text)
    text = _USERINFO.sub(lambda m: f"{m.group(1)}{REDACTED}@", text)
    text = _PAIR.sub(lambda m: f"{m.group(1)}{REDACTED}", text)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _field_value(name: str, value: Any) -> Any:
    """A record field made safe: a sensitive name hides its value; text is scrubbed; numbers and booleans stay;
    anything else (a list, a dict) becomes scrubbed JSON text."""
    if _SENSITIVE_FIELD.search(name):
        return REDACTED
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return scrub(value)
    return scrub(json.dumps(value, default=str, sort_keys=True))


class RedactFilter(logging.Filter):
    """On every handler this module makes: the message, the fields and the exception of a record become safe
    text, and the run's context (request id, user, site) is attached. It runs in the thread that logged, so the
    context is the caller's."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except (TypeError, ValueError):          # a call with arguments that do not fit its format
            message = str(record.msg)
        record.msg, record.args = scrub(message), None
        fields: Dict[str, Any] = {}
        for name, value in (getattr(record, "fields", None) or {}).items():
            key = re.sub(r"[^\w.-]", "_", printable(name, 60)) or "_"
            fields[key] = _field_value(key, value)
        if record.exc_info and record.exc_info[0] is not None:
            fields["exc"] = scrub(f"{record.exc_info[0].__name__}: {record.exc_info[1]}")
        record.exc_info = record.exc_text = record.stack_info = None
        record.fields = fields
        record.event = scrub(getattr(record, "event", "") or "", 80)
        record.request_id = scrub(_request_id.get(), 80)
        record.user = scrub(_user.get(), 80)
        record.site = scrub(_site.get(), 120)
        return True


# -- formats -----------------------------------------------------------------------------------------------

_CLI_PREFIXES = {"warning": "Warning: ", "error": "ERROR: "}
_RESERVED = ("ts", "level", "logger", "msg", "event", "request_id", "user", "site")


def _timestamp(record: logging.LogRecord) -> str:
    moment = datetime.fromtimestamp(record.created, timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(record.msecs):03d}Z"


def _cli_prefix(record: logging.LogRecord) -> str:
    if getattr(record, "event", "") == "warning":
        return _CLI_PREFIXES["warning"]
    if record.levelno >= logging.ERROR:
        return _CLI_PREFIXES["error"]
    return "[verbose] "


def _text_value(value: Any) -> str:
    text = ("true" if value else "false") if isinstance(value, bool) else "" if value is None else str(value)
    return json.dumps(text, ensure_ascii=True) if not text or re.search(r"[\s=\"]", text) else text


class _Formatter(logging.Formatter):
    def __init__(self, mode: str) -> None:
        super().__init__()
        self.mode = mode

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if self.mode == "cli":
            return f"{_cli_prefix(record)}{message}"
        fields: Dict[str, Any] = getattr(record, "fields", {})
        context = {name: getattr(record, name, "") for name in ("request_id", "user", "site")}
        if self.mode == "json":
            document: Dict[str, Any] = {
                "ts": _timestamp(record), "level": record.levelname, "logger": record.name, "msg": message,
                "event": getattr(record, "event", "") or None, **{k: v or None for k, v in context.items()}}
            for name, value in fields.items():
                document[f"field_{name}" if name in _RESERVED else name] = value
            return json.dumps(document, ensure_ascii=True, default=str)
        parts = [_timestamp(record), f"{record.levelname:<7}", record.name, getattr(record, "event", "") or "-",
                 message]
        parts += [f"{name}={_text_value(value)}" for name, value in context.items() if value]
        parts += [f"{name}={_text_value(value)}" for name, value in fields.items()]
        return " ".join(parts)


# What is held around every write to stderr by this module: ``progress.Progress`` swaps in a context manager that erases
# its transient line first and keeps it from redrawing meanwhile, so a warning is never drawn on top of the spinner.
STDERR_GUARD: Callable[[], ContextManager[None]] = nullcontext


class _Handler(logging.Handler):
    """Writes each record as one line to the *current* ``sys.stderr`` (looked up per record, because a test or a
    caller may replace it after the handler was made) or to a stream given to it."""

    def __init__(self, mode: str, stream: Optional[TextIO], verbose: bool) -> None:
        super().__init__()
        self.mode = mode
        self.stream = stream
        self.verbose = verbose
        self.setFormatter(_Formatter(mode))
        self.addFilter(RedactFilter())

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if self.mode == "cli" and not self.verbose and _cli_prefix(record) == "[verbose] ":
                return                          # on the command line only warnings and errors show by default
            out = self.stream or sys.stderr
            with STDERR_GUARD():
                out.write(self.format(record) + "\n")
                out.flush()
        except Exception:                       # logging must never take the program down
            self.handleError(record)


def parse_level(text: Any) -> int:
    by_word = {level.lower(): level for level in LEVEL_WORDS}
    word = str(text).strip().lower()
    if word not in by_word:
        raise ValueError(f"log level must be one of {', '.join(LEVEL_WORDS)} (got {text!r})")
    return int(getattr(logging, by_word[word]))


def configure(fmt: str = "cli", level: Any = "WARNING", stream: Optional[TextIO] = None) -> None:
    """Set the format (``cli``, ``text`` or ``json``) and level of the ``homelab_probe`` logger tree, replacing
    what an earlier call set. Safe to call again. ``stream`` is for tests; the default is stderr."""
    if fmt not in ("cli", *FORMAT_WORDS):
        raise ValueError(f"log format must be one of cli, {', '.join(FORMAT_WORDS)} (got {fmt!r})")
    number = level if isinstance(level, int) else parse_level(level)
    logger = logging.getLogger(ROOT)
    for handler in [h for h in logger.handlers if isinstance(h, _Handler)]:
        logger.removeHandler(handler)
    logger.addHandler(_Handler(fmt, stream, verbose=number < logging.WARNING))
    logger.setLevel(number)
    logger.propagate = False        # one destination: the handler above, not whatever the root logger has


def reset() -> None:
    """Back to the state at import: command-line format, WARNING, no registered secrets, no context."""
    configure()
    with _secrets_lock:
        _secrets.clear()
    for var in (_request_id, _user, _site):
        var.set("")
    _sink.set(None)
    _quiet.set(False)


# -- emitting ----------------------------------------------------------------------------------------------

def log_event(logger: logging.Logger, level: int, event: str, message: str, /, **fields: Any) -> None:
    """Log ``message`` as ``event`` (a name from ``EVENTS``) with its own fields. The message is for a person;
    the fields are for a program, so put anything a collector would filter on in a field."""
    if logger.isEnabledFor(level):
        logger.log(level, message, extra={"event": event, "fields": fields})


def verbose(message: str, event: str = "run.settings") -> None:
    """A line of ``--verbose`` output: a DEBUG record, shown as ``[verbose] ...`` on the command line."""
    log_event(_log, logging.DEBUG, event, message)


def warn(message: str) -> None:
    """A degraded read or a loose setting: a WARNING record (shown as ``Warning: ...`` on the command line) and,
    when a ``collect_warnings`` block is active, one more entry in its list."""
    sink = _sink.get()
    if sink is not None:
        sink.append(printable(message))
    log_event(_log, logging.DEBUG if _quiet.get() else logging.WARNING, "warning", message)


@contextmanager
def collect_warnings(quiet: bool = False) -> Iterator[List[str]]:
    """The warnings of the block as data, in the order they were issued. By default they are still shown as
    before (the printed text is unchanged); with ``quiet=True`` they are only in the list and a DEBUG record, for
    a caller (an API) that returns them to its own client instead of printing them."""
    messages: List[str] = []
    sink_token, quiet_token = _sink.set(messages), _quiet.set(quiet)
    try:
        yield messages
    finally:
        _quiet.reset(quiet_token)
        _sink.reset(sink_token)


configure()
