"""Static, request-local completion. No handlers, service, configuration or filesystem access."""

from typing import Literal, Sequence, TypedDict

from .. import logs
from ..cli import build_parser
from ..completion import Option, spec
from . import terminal_policy as policy
from .auth import ROLE_RANK
from .errors import ApiError

# Build immutable metadata at startup: argparse's gettext may consult locale files/environment.
GRAMMAR = spec(build_parser(strict=True))


class Candidate(TypedDict):
    label: str
    description: str
    kind: Literal["command", "option", "choice"]


def complete(argv: Sequence[str], token_index: int, cursor: int, role: str) -> tuple[list[Candidate], bool]:
    """Replace the active token, using only its prefix and the tokens before it. Never authorize execution."""
    grammar = GRAMMAR
    if policy.compatibility_errors(grammar):
        raise ApiError(503, "terminal_unreviewed", "The CLI grammar needs review before terminal use.")
    commands = {c.name: c for c in grammar.commands if policy.CAPABILITIES[c.name].operation is not None
                and ROLE_RANK.get(role, 0) >= ROLE_RANK[policy.CAPABILITIES[c.name].minimum_role]}
    if ROLE_RANK.get(role, 0) < ROLE_RANK["viewer"]:
        return [], False
    options, classifications = grammar.options, policy.GLOBAL_OPTIONS
    command = None
    pending = None
    used: set[str] = set()
    positional_seen = False
    help_topics = False

    def allowed(option: Option) -> bool:
        return not option.path and all(classifications[f].status != "unavailable" for f in option.flags)

    def choices(option: Option) -> tuple[str, ...]:
        return classifications[option.flags[0]].choices or option.choices

    def valid_value(option: Option, value: str) -> bool:
        values = choices(option)
        if option.lowercase:
            value = value.lower()
        return not values or all(v in values for v in (value.split(",") if option.comma_list else [value]))

    for index, token in enumerate(argv[:token_index]):
        if pending is not None:
            if not valid_value(pending, token):
                return [], False
            pending = None
            continue
        if index == 0 and token == "hlp":
            continue
        if help_topics:
            return [], False
        if command is None and token in ("help", "-h", "--help"):
            help_topics = True
            continue
        if token.startswith("-"):
            flag, equals, value = token.partition("=")
            option = next((o for o in options if flag in o.flags), None)
            if option is None or not allowed(option) or (option.long in used and not option.repeatable):
                return [], False
            used.add(option.long)
            if not option.takes_value:
                if equals or flag in ("-h", "--help", "--version"):
                    return [], False
            elif equals:
                if not valid_value(option, value):
                    return [], False
            else:
                pending = option
        elif command is None:
            if token not in commands:
                return [], False
            command = commands[token]
            options, classifications = command.options, policy.CAPABILITIES[token].options
            used.clear()
        else:
            if positional_seen or (command.choices and token not in command.choices):
                return [], False
            positional_seen = True

    prefix = argv[token_index][:cursor]
    candidates: list[Candidate] = []

    def add(label: str, description: str, kind: Literal["command", "option", "choice"]) -> None:
        if label.startswith(prefix):
            candidates.append({"label": label, "description": description, "kind": kind})

    def values(option: Option, value_prefix: str, leader: str = "") -> None:
        items = choices(option)
        selected: list[str] = []
        if option.comma_list:
            selected = value_prefix.split(",")[:-1]
            if any(item not in items for item in selected):
                return
            leader += "".join(item + "," for item in selected)
        for value in items:
            if value not in selected:
                add(leader + value, option.help, "choice")

    if pending is not None:
        values(pending, prefix)
    elif "=" in prefix and prefix.startswith("-"):
        flag, value = prefix.split("=", 1)
        option = next((o for o in options if flag in o.flags), None)
        if option is not None and allowed(option) and option.takes_value and (
                option.long not in used or option.repeatable):
            values(option, value, flag + "=")
    else:
        if command is None:
            for name, item in commands.items():
                add(name, item.help, "command")
            if not help_topics:
                add("help", policy.HELP.reason, "command")
                add("version", "Show the application version.", "command")
        elif not positional_seen:
            for value in command.choices:
                add(value, command.help, "choice")
        if not help_topics:
            for option in options:
                if allowed(option) and (option.long not in used or option.repeatable):
                    for flag in option.flags:
                        add(flag, option.help, "option")

    # Never offer a different argument after truncation or redaction.
    result: list[Candidate] = []
    truncated = False
    for candidate in sorted(candidates, key=lambda c: c["label"]):
        label = logs.scrub(candidate["label"], limit=policy.TOKEN_CHARS)
        description = logs.scrub(candidate["description"], limit=policy.COMPLETION_DESCRIPTION_CHARS)
        if label != candidate["label"] or len(result) >= policy.COMPLETION_COUNT:
            truncated = True
            continue
        truncated |= len(candidate["description"]) > policy.COMPLETION_DESCRIPTION_CHARS
        result.append({"label": label, "description": description, "kind": candidate["kind"]})
    return result, truncated
