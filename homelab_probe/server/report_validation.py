"""Parameter checks shared by report routes and terminal adapters."""

import argparse
from typing import List, Optional

from ..commands import diagnose_areas
from ..config import ConfigError, validate_site
from ..events import SEVERITIES, parse_duration
from ..snapshot import EventQuery
from .errors import ApiError


def checked_site(site: str) -> str:
    try:
        return validate_site(site)
    except ConfigError:
        raise ApiError(422, "invalid_parameter", "The site name is not valid.") from None


def duration(text: str) -> int:
    try:
        return parse_duration(text)
    except ValueError as error:
        raise ApiError(422, "invalid_parameter", str(error)) from None


def event_query(since: str, category: Optional[List[str]], severity: Optional[List[str]], search: str) -> EventQuery:
    categories, severities = category or [], [s.lower() for s in severity or []]
    if any(len(c) > 64 for c in categories) or any(s not in SEVERITIES for s in severities):
        raise ApiError(422, "invalid_parameter", f"severity must be one of {', '.join(SEVERITIES)}.")
    return EventQuery(duration(since), tuple(categories), tuple(severities), search)


def areas(only: Optional[List[str]], skip: Optional[List[str]], no_events: bool) -> Optional[List[str]]:
    try:
        return diagnose_areas(argparse.Namespace(only=only or [], skip=skip or [], no_events=no_events))
    except ValueError as error:
        raise ApiError(422, "invalid_parameter", str(error)) from None
