"""Reviewed web-terminal grammar policy. Parsing grants no permission and performs no execution.

Every option spelling is deliberately listed here, including aliases and unavailable commands' options.
The parity test makes CLI changes require a web-policy decision rather than silently exposing them.
"""

import argparse
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Optional, Sequence, Tuple

from ..cli import StrictParseError, build_parser
from ..completion import Spec

BODY_BYTES = 16 * 1024
TOKEN_COUNT = 64
TOKEN_CHARS = 256
USER_CONCURRENCY = 1
USER_RATE = 30
GLOBAL_CONCURRENCY = 4
OUTPUT_BYTES = 256 * 1024


@dataclass(frozen=True)
class Policy:
    status: str
    reason: str
    choices: Tuple[str, ...] = ()


READ = Policy("supported", "Reviewed read-only report argument.")
HELP = Policy("restricted", "Static help only; never runs a command.")
SITE = Policy("restricted", "Validated site; no site-level ACL is implied.")
SERVER = Policy("unavailable", "Connection, configuration and presentation are server-owned.")
FILE = Policy("unavailable", "Client-selected server files are not permitted.")
NOTIFY = Policy("unavailable", "Notifications and persistent watch are not permitted.")
TEXT_JSON = Policy("unavailable", "The first release offers text and JSON, not CSV.")
FORMAT = Policy("restricted", "Only the plain-text tree is offered.", ("text",))
BOUNDED = Policy("restricted", "Server-owned web caps apply; CLI all-results values are not unlimited.")


def _options(*groups: Tuple[str, Policy]) -> Mapping[str, Policy]:
    return MappingProxyType({flag: policy for flags, policy in groups for flag in flags.split()})


GLOBAL_OPTIONS = _options(
    ("-h --help", HELP), ("--version", READ), ("--site", SITE),
    ("--verbose --debug --demo --timeout --parallel --env-file --color --plain --no-progress", SERVER))


@dataclass(frozen=True)
class Capability:
    policy: Policy
    options: Mapping[str, Policy]
    operation: Optional[str]
    minimum_role: str = "viewer"


def _report(name: str, flags: str = "", *groups: Tuple[str, Policy]) -> Capability:
    return Capability(Policy("supported", "Reviewed read-only report."),
                      _options(("-h --help", HELP), (flags, READ), *groups), name)


def _unavailable(reason: str, flags: str) -> Capability:
    policy = Policy("unavailable", reason)
    return Capability(policy, _options(("-h --help " + flags, policy)), None)


CAPABILITIES: Mapping[str, Capability] = MappingProxyType({
    "audit": _report("audit", "--fail-on --show-ignored --no-emoji --json", ("--config", FILE)),
    "client": _report("client", "--json --no-events --no-emoji", ("--since", BOUNDED), ("--config", FILE)),
    "completion": _unavailable("Shell completion scripts are not browser completion.", ""),
    "diagnose": _report("diagnose", "--fail-on --only --skip --no-events --show-ignored --no-emoji --json",
                        ("--since", BOUNDED), ("--config", FILE),
                        ("--watch --notify --notify-min --notify-redact --notify-dry-run --notify-baseline "
                         "--notify-state", NOTIFY)),
    "diff": _unavailable("Saved comparisons need reviewed server-managed resource IDs.",
                         "--dir --last-two --all --json"),
    "doctor": _unavailable("Installation diagnostics are not exposed to terminal viewers.",
                           "--offline --no-events --config --json"),
    "events": _report("events", "--category --severity --event --client --device -s --search --summary --json",
                      ("--since --limit", BOUNDED)),
    "export": _unavailable("Exports write files and need reviewed authenticated downloads.",
                           "-o --output-dir --format --include-offline"),
    "firewall": _report("firewall", "--all --zones --search --no-emoji --json"),
    "info": _report("info"),
    "init": _unavailable("Installation and configuration changes are not terminal operations.",
                         "--dir --url --site --verify --api-key-stdin --no-input --force --check"),
    "new-clients": _report("new-clients", "-s --search --json"),
    "query": _report("query", "-s --search --include-offline --json --switch --down --errors --network --ssid "
                     "--ap --offline", ("--config", FILE), ("--csv", TEXT_JSON)),
    "serve": _unavailable("Server process lifecycle is not a terminal operation.",
                          "--host --allowed-host --forwarded-allow-ips --allow-public-controller --read-only "
                          "--scheduler --port --data-dir --config"),
    "snapshot": _unavailable("Snapshots write server files and need a separate capability review.",
                             "-o --output --dir --keep --force"),
    "topology": _report("topology", "--clients --json --no-emoji", ("--format", FORMAT), ("--config", FILE)),
    "wan": _report("wan", "--json", ("--days", BOUNDED), ("--config", FILE)),
    "web-user": _unavailable("Account and security changes are not terminal operations.",
                             "--role --password-stdin --data-dir"),
    "wifi": _report("wifi", "--band --ap --min-signal --all --json"),
})


def compatibility_errors(grammar: Spec, capabilities: Mapping[str, Capability] = CAPABILITIES,
                         global_options: Mapping[str, Policy] = GLOBAL_OPTIONS) -> Tuple[str, ...]:
    """Exact bidirectional parity, including aliases. Diagnostic names come from trusted grammar metadata."""
    errors = []
    commands = {command.name: command for command in grammar.commands}
    if commands.keys() != capabilities.keys():
        errors.append("Command classifications do not match the CLI.")
    scopes = [("global", grammar.options, global_options)]
    scopes += [(name, command.options, capabilities[name].options)
               for name, command in commands.items() if name in capabilities]
    for scope, options, policies in scopes:
        flags = {flag for option in options for flag in option.flags}
        if flags != policies.keys():
            errors.append(f"Option classifications do not match: {scope}.")
    return tuple(errors)


class UnsupportedCapability(ValueError):
    """Valid or invalid CLI input that cannot select an approved browser capability."""


@dataclass(frozen=True)
class ParsedCommand:
    operation: str
    args: argparse.Namespace


def parse_command(argv: Sequence[str]) -> ParsedCommand:
    """Resolve only explicitly permitted operations. The HTTP layer must still authenticate and apply limits."""
    if not argv or any(not isinstance(token, str) for token in argv):
        raise StrictParseError("The command arguments are not valid.")
    tokens = list(argv)
    if tokens[0] == "hlp":
        tokens = tokens[1:]
    if tokens in (["help"], ["-h"], ["--help"]):
        return ParsedCommand("help", argparse.Namespace(topic=None))
    if tokens in (["version"], ["--version"]):
        return ParsedCommand("version", argparse.Namespace())
    help_tokens = ("help", "-h", "--help")
    if len(tokens) == 2 and (tokens[0] in help_tokens or tokens[1] in ("-h", "--help")):
        topic = tokens[1] if tokens[0] in help_tokens else tokens[0]
        capability = CAPABILITIES.get(topic)
        if capability is None or capability.operation is None:
            raise UnsupportedCapability("This operation is unavailable in the browser.")
        return ParsedCommand("help", argparse.Namespace(topic=topic))
    args = build_parser(strict=True).parse_args(tokens)
    capability = CAPABILITIES.get(args.command)
    if capability is None or capability.operation is None:
        raise UnsupportedCapability("This operation is unavailable in the browser.")
    for scope, flags in args._terminal_options.items():
        policies = GLOBAL_OPTIONS if scope == "" else capability.options
        if any(flag not in policies or policies[flag].status == "unavailable" for flag in flags):
            raise UnsupportedCapability("This option is unavailable in the browser.")
    if getattr(args, "format", "text") not in FORMAT.choices or args.version:
        raise UnsupportedCapability("This option is unavailable in the browser.")
    wants_help = any("--help" in flags for flags in args._terminal_options.values())
    return ParsedCommand("help", argparse.Namespace(topic=args.command)) if wants_help else ParsedCommand(
        capability.operation, args)


def compatibility_markdown() -> str:
    """The documentation matrix is tested byte-for-byte against this registry."""
    rows = ["| Scope | Argument | Status | Reason |", "| --- | --- | --- | --- |"]
    scopes = [("global", GLOBAL_OPTIONS)]
    for name, capability in CAPABILITIES.items():
        rows.append(f"| `{name}` | command | {capability.policy.status} | {capability.policy.reason} |")
        scopes.append((name, capability.options))
    for scope, options in scopes:
        grouped: dict[Policy, list[str]] = {}
        for flag, policy in options.items():
            grouped.setdefault(policy, []).append(f"`{flag}`")
        for policy, flags in grouped.items():
            rows.append(f"| `{scope}` | {', '.join(flags)} | {policy.status} | {policy.reason} |")
    return "\n".join(rows) + "\n"
