"""Authenticated terminal API: bounded input, fixed dispatch and worker-owned execution slots."""

import asyncio
import contextvars
import json
import math
import re
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Any, Callable, Dict, Iterable, Literal, TypeVar

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .. import __version__, logs
from ..cli import StrictParseError, build_parser
from ..client import UniFiAPIError
from ..client_view import candidate_rows
from ..completion import spec
from ..util import printable
from . import terminal_completion
from . import terminal_policy as policy
from . import terminal_reports as reports
from .auth import ROLE_RANK
from .errors import ApiError, error_responses, from_controller

API = "/api/v1/terminal"
_SENSITIVE = re.compile(r"(?i)(password|passphrase|secret|token|api.?key|cookie|authorization|session.?id|credentials)")


class ExecuteBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    argv: Annotated[list[Annotated[str, Field(min_length=1, max_length=policy.TOKEN_CHARS)]],
                    Field(min_length=1, max_length=policy.TOKEN_COUNT)]
    requestId: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")]


class ExecuteResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    correlationId: str
    requestId: str
    operation: str
    status: Literal["completed"]
    output: str
    warnings: list[str]
    truncated: bool


class CompleteBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    argv: Annotated[list[Annotated[str, Field(max_length=policy.TOKEN_CHARS)]],
                    Field(min_length=1, max_length=policy.TOKEN_COUNT)]
    tokenIndex: Annotated[int, Field(ge=0, lt=policy.TOKEN_COUNT)]
    cursor: Annotated[int, Field(ge=0, le=policy.TOKEN_CHARS)]
    requestId: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")]

    @model_validator(mode="after")
    def check_cursor(self) -> "CompleteBody":
        if self.tokenIndex >= len(self.argv) or self.cursor > len(self.argv[self.tokenIndex]):
            raise ValueError("Invalid cursor.")
        return self


class CompletionCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: Annotated[str, Field(max_length=policy.TOKEN_CHARS)]
    description: Annotated[str, Field(max_length=policy.COMPLETION_DESCRIPTION_CHARS)]
    kind: Literal["command", "option", "choice"]


class CompleteResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    correlationId: str
    requestId: str
    tokenIndex: int
    candidates: Annotated[list[CompletionCandidate], Field(max_length=policy.COMPLETION_COUNT)]
    truncated: bool


class TerminalError(BaseModel):
    error: str
    message: str
    correlationId: str | None = None
    executionStatus: Literal["did_not_run", "failed", "outcome_unknown"] | None = None
    candidates: list[dict[str, str]] | None = None
    retry_after: int | None = None


class OptionMetadata(BaseModel):
    flags: list[str]
    description: str
    takesValue: bool
    repeatable: bool
    commaList: bool
    status: Literal["supported", "restricted", "unavailable"]
    reason: str
    choices: list[str]


class CommandMetadata(BaseModel):
    name: str
    description: str
    status: Literal["supported", "restricted", "unavailable"]
    reason: str
    choices: list[str]
    options: list[OptionMetadata]


class TerminalLimits(BaseModel):
    bodyBytes: int
    tokenCount: int
    tokenChars: int
    outputBytes: int
    perUserConcurrency: int
    perUserPerMinute: int
    globalConcurrency: int
    eventRows: int
    eventWindowSeconds: int
    wanDays: int
    completionCandidates: int
    completionDescriptionChars: int


class CapabilitiesResult(BaseModel):
    version: Literal[1]
    commands: list[CommandMetadata]
    globalOptions: list[OptionMetadata]
    staticOperations: list[str]
    limits: TerminalLimits
    cancellation: str


class TerminalState:
    """Admission is atomic. Only the worker releases an admitted slot, including after HTTP timeout/cancellation."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.lock = threading.Lock()
        self.active: set[str] = set()
        self.rates: dict[str, deque[float]] = {}
        self.denial_at = -math.inf
        self.pool = ThreadPoolExecutor(max_workers=policy.GLOBAL_CONCURRENCY, thread_name_prefix="terminal")

    def acquire(self, user: str) -> None:
        with self.lock:
            now = self.clock()
            self.rates = {name: times for name, times in self.rates.items() if times and times[-1] > now - 60}
            times = self.rates.setdefault(user, deque())
            while times and times[0] <= now - 60:
                times.popleft()
            wait = math.ceil(times[0] + 60 - now) if len(times) >= policy.USER_RATE else 0
            if wait or user in self.active or len(self.active) >= policy.GLOBAL_CONCURRENCY:
                raise ApiError(429, "terminal_busy", "The terminal is busy; try again later.",
                               headers={"Retry-After": str(max(1, wait))}, retry_after=max(1, wait))
            times.append(now)
            self.active.add(user)

    def release(self, user: str) -> None:
        with self.lock:
            self.active.remove(user)

    def audit(self, request: Request, operation: str, outcome: str, started: float, *, denial: bool = False,
              resources: tuple[str, ...] = ()) -> None:
        if denial:
            with self.lock:
                now = self.clock()
                if now - self.denial_at < 1:
                    return
                self.denial_at = now
        session = getattr(request.state, "session", None)
        try:
            request.app.state.auth.audit.write(
                "terminal.denied" if denial else "terminal.executed", session.username if session else "(anonymous)",
                operation=operation, resources=resources, outcome=outcome, correlation_id=logs.current_request_id(),
                duration_ms=round((self.clock() - started) * 1000))
        except OSError:
            logs.warn("the audit log could not be written (terminal)")


class TerminalBoundary:
    """No-store and correlation ids on every terminal response, including authentication/origin denials."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(API + "/"):
            await self.app(scope, receive, send)
            return
        started = scope["app"].state.terminal.clock()

        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"]
                message = {**message, "headers": [*headers, (b"cache-control", b"no-store")]}
                if message["status"] in (401, 403, 413, 415, 422, 429, 503):
                    request = Request(scope)
                    request.app.state.terminal.audit(request, "", str(message["status"]), started, denial=True)
            await send(message)

        await self.app(scope, receive, wrapped)


def cleaner(request: Request) -> Callable[[str], str]:
    """Request-local secrets do not enter the global registry (and cannot grow it without bound)."""
    state = request.app.state
    secrets = [*state.config.secret_values(), request.state.session.csrf, *request.cookies.values(),
               state.config.controller_url, *[
                   str(Path(path).resolve()) for path in (state.config.env_file, state.settings_path, state.state_dir)
                   if path is not None and str(Path(path).resolve()) != "/"]]
    secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def clean(text: str) -> str:
        for secret in secrets:
            text = text.replace(secret, "[redacted]")
        prefix = text[:len(text) - len(text.lstrip(" "))]
        return prefix + logs.scrub(text, limit=max(1, len(text) + 1))

    return clean


def clean_data(value: Any, clean: Callable[[str], str]) -> Any:
    if isinstance(value, str):
        return clean(value)
    if isinstance(value, dict):
        return {clean(str(k)): "[redacted]" if _SENSITIVE.search(str(k)) else clean_data(v, clean)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean_data(v, clean) for v in value]
    return value


def bounded(chunks: Iterable[str]) -> tuple[str, bool]:
    output = bytearray()
    for chunk in chunks:
        encoded = chunk.encode("utf-8")
        remaining = policy.OUTPUT_BYTES - len(output)
        output.extend(encoded[:remaining])
        if len(encoded) > remaining:
            return output.decode("utf-8", errors="ignore"), True
    return output.decode("utf-8"), False


def capabilities(role: str) -> Dict[str, Any]:
    grammar = spec(build_parser(strict=True))
    if policy.compatibility_errors(grammar):
        raise ApiError(503, "terminal_unreviewed", "The CLI grammar needs review before terminal use.")
    commands = []
    for command in grammar.commands:
        capability = policy.CAPABILITIES[command.name]
        if ROLE_RANK.get(role, 0) < ROLE_RANK[capability.minimum_role]:
            continue
        options = []
        for option in command.options:
            classification = capability.options[option.flags[0]]
            options.append({"flags": list(option.flags), "description": option.help,
                            "takesValue": option.takes_value, "repeatable": option.repeatable,
                            "commaList": option.comma_list, "status": classification.status,
                            "reason": classification.reason,
                            "choices": list(classification.choices or option.choices)})
        commands.append({"name": command.name, "description": command.help, "status": capability.policy.status,
                         "reason": capability.policy.reason, "choices": list(capability.choices), "options": options})
    global_options = []
    for option in grammar.options:
        classification = policy.GLOBAL_OPTIONS[option.flags[0]]
        global_options.append({"flags": list(option.flags), "description": option.help,
                               "takesValue": option.takes_value, "repeatable": option.repeatable,
                               "commaList": option.comma_list, "status": classification.status,
                               "reason": classification.reason, "choices": list(option.choices)})
    return {"version": 1, "commands": commands, "globalOptions": global_options,
        "staticOperations": ["help", "version"],
        "limits": {"bodyBytes": policy.BODY_BYTES, "tokenCount": policy.TOKEN_COUNT, "tokenChars": policy.TOKEN_CHARS,
                   "outputBytes": policy.OUTPUT_BYTES, "perUserConcurrency": policy.USER_CONCURRENCY,
                   "perUserPerMinute": policy.USER_RATE, "globalConcurrency": policy.GLOBAL_CONCURRENCY,
                   "eventRows": reports.MAX_EVENTS, "eventWindowSeconds": reports.MAX_SINCE,
                   "wanDays": reports.MAX_DAYS, "completionCandidates": policy.COMPLETION_COUNT,
                   "completionDescriptionChars": policy.COMPLETION_DESCRIPTION_CHARS},
        "cancellation": "Stopping waiting does not cancel controller work."}


def work(request: Request, command: policy.ParsedCommand, body: ExecuteBody) -> Dict[str, Any]:
    state: TerminalState = request.app.state.terminal
    user, started, outcome = request.state.session.username, state.clock(), "internal_error"
    operation, args = command.operation, command.args
    try:
        clean = cleaner(request)
        warnings = []
        if operation == "version":
            chunks: Iterable[str] = [f"hlp {__version__}"]
        elif operation == "help":
            selected = [{"name": name, "description": capability.policy.reason,
                         "choices": list(capability.choices), "options": [
                             {"flag": flag, "status": item.status, "reason": item.reason,
                              "choices": list(item.choices)} for flag, item in capability.options.items()]}
                        for name, capability in policy.CAPABILITIES.items()
                        if capability.operation is not None and (args.topic is None or name == args.topic)
                        and ROLE_RANK[request.state.session.role] >= ROLE_RANK[capability.minimum_role]]
            chunks = [json.dumps(selected, ensure_ascii=False, indent=2)]
        else:
            built = request.app.state.service.build(reports.adapter(operation, args, request))
            document = built.document
            if operation == "client" and not document.data:
                candidates = [{k: clean(str(v))[:256] for k, v in row.items()}
                              for row in candidate_rows(document.meta["matches"][:20])]
                raise ApiError(409, "client_ambiguous", "No single client matches.", candidates=candidates)
            warnings = [clean(w)[:500] for w in built.warnings[:32]]
            document = replace(document, data=clean_data(document.data, clean))
            if getattr(args, "json", False):
                chunks = json.JSONEncoder(ensure_ascii=False, indent=2).iterencode(document.data)
            else:
                text = reports.render(operation, args, document)
                chunks = (clean(line) for line in text.splitlines(keepends=True))
                # Scrubbing is line-oriented; restore only renderer-owned line separators.
                chunks = (line + "\n" for line in chunks)
        output, truncated = bounded(chunks)
        outcome = "completed"
        return {"correlationId": logs.current_request_id(), "requestId": body.requestId, "operation": operation,
                "status": "completed", "output": output, "warnings": warnings, "truncated": truncated}
    except UniFiAPIError as error:
        mapped = from_controller(error)
        mapped.extra["executionStatus"] = "failed"
        outcome = mapped.code
        raise mapped from None
    except ApiError as error:
        outcome = error.code
        error.extra.setdefault("executionStatus", "failed")
        raise
    finally:
        try:
            state.audit(request, operation, outcome, started,
                        resources=() if operation in ("info", "help", "version") else (args.site,))
        finally:
            state.release(user)


Body = TypeVar("Body", ExecuteBody, CompleteBody)


async def read_model(request: Request, model: type[Body]) -> Body:
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise ApiError(415, "unsupported_media_type", "The body must be JSON.")
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > policy.BODY_BYTES:
            raise ApiError(413, "terminal_body_too_large", "The terminal request is too large.")
        raw.extend(chunk)
    try:
        body = model.model_validate_json(raw)
    except ValidationError:
        raise ApiError(422, "invalid_parameter", "The terminal request is not valid.") from None
    if any(printable(token) != token or any(ord(c) < 32 for c in token) for token in body.argv):
        raise ApiError(422, "invalid_parameter", "The command arguments are not valid.")
    return body


async def read_body(request: Request) -> ExecuteBody:
    return await read_model(request, ExecuteBody)


def router() -> APIRouter:
    api = APIRouter(prefix=API, tags=["terminal"])

    @api.get("/capabilities", response_model=CapabilitiesResult, summary="Reviewed browser terminal capabilities",
             responses={503: {"description": "The CLI grammar needs policy review before terminal use",
                              "content": {"application/json": {"schema": TerminalError.model_json_schema()}}}})
    def terminal_capabilities(request: Request) -> Dict[str, Any]:
        return capabilities(request.state.session.role)

    @api.post("/complete", response_model=CompleteResult, summary="Complete reviewed static terminal arguments",
              responses={code: {**detail, "content": {"application/json": {
                  "schema": TerminalError.model_json_schema()}}}
                  for code, detail in error_responses(401, 403, 413, 415, 422, 429, 500, 503,
                      text={413: "The request body is too large", 415: "The body must be JSON"}).items()},
              openapi_extra={"requestBody": {"required": True, "content": {"application/json": {
                  "schema": CompleteBody.model_json_schema()}}}})
    async def terminal_complete(request: Request) -> Dict[str, Any]:
        body = await read_model(request, CompleteBody)
        state: TerminalState = request.app.state.terminal
        user = request.state.session.username
        state.acquire(user)
        try:
            candidates, truncated = terminal_completion.complete(body.argv, body.tokenIndex, body.cursor,
                                                                request.state.session.role)
            return {"correlationId": logs.current_request_id(), "requestId": body.requestId,
                    "tokenIndex": body.tokenIndex, "candidates": candidates, "truncated": truncated}
        except ApiError:
            raise
        except Exception as error:
            logs.warn(f"terminal completion failed ({type(error).__name__}); see correlation id")
            raise ApiError(500, "terminal_internal", "The terminal request could not be completed.",
                           correlationId=logs.current_request_id()) from None
        finally:
            state.release(user)

    @api.post("/execute", response_model=ExecuteResult, summary="Execute one reviewed read-only report",
              responses={code: {**detail, "content": {"application/json": {
                  "schema": TerminalError.model_json_schema()}}}
                  for code, detail in error_responses(401, 403, 404, 409, 413, 415, 422, 429, 500, 502, 504,
                      text={413: "The request body is too large", 415: "The body must be JSON"}).items()},
              openapi_extra={"requestBody": {"required": True, "content": {"application/json": {
                  "schema": ExecuteBody.model_json_schema()}}}})
    async def terminal_execute(request: Request) -> JSONResponse:
        dispatched = False
        try:
            body = await read_body(request)
            try:
                command = policy.parse_command(body.argv)
            except policy.UnsupportedCapability:
                raise ApiError(422, "terminal_unsupported", "This capability is unavailable in the browser.") from None
            except StrictParseError:
                raise ApiError(422, "invalid_parameter", "The command arguments are not valid.") from None
            capability = policy.CAPABILITIES.get(command.operation)
            minimum = capability.minimum_role if capability else "viewer"
            if ROLE_RANK.get(request.state.session.role, 0) < ROLE_RANK[minimum]:
                raise ApiError(403, "forbidden", "This operation is not permitted.")
            reports.validate(command, request)
            state: TerminalState = request.app.state.terminal
            state.acquire(request.state.session.username)
            try:
                context = contextvars.copy_context()
                future = state.pool.submit(context.run, work, request, command, body)
                dispatched = True
            except Exception:
                state.release(request.state.session.username)
                raise
            # Shield keeps a cancelled HTTP task from cancelling queued/running controller work.
            wrapped = asyncio.wrap_future(future)
            wrapped.add_done_callback(lambda done: None if done.cancelled() else done.exception())
            try:
                result = await asyncio.wait_for(asyncio.shield(wrapped), timeout=request.app.state.config.timeout)
            except asyncio.TimeoutError:
                raise ApiError(504, "terminal_timeout", "Stopped waiting; controller work may still be running.",
                               executionStatus="outcome_unknown") from None
            return JSONResponse(result)
        except ApiError as error:
            error.extra.update(correlationId=logs.current_request_id())
            error.extra.setdefault("executionStatus", "did_not_run")
            raise
        except Exception as error:
            logs.warn(f"terminal execution failed ({type(error).__name__}); see correlation id")
            raise ApiError(500, "terminal_internal", "The terminal request could not be completed.",
                           correlationId=logs.current_request_id(),
                           executionStatus="outcome_unknown" if dispatched else "did_not_run") from None

    return api
