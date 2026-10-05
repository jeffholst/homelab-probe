"""The JSON Schemas of the API: the ones in ``docs/schemas`` plus the two keys every response adds.

A response is the document ``--json`` prints with ``generated_at`` and ``warnings`` added. A command whose ``--json`` is
a bare array cannot take keys, so its response is ``{"items": [...], "generated_at", "warnings"}``. The schemas of the
documents are open objects, so the first kind validates against them as they are; ``response_schema`` declares the two
keys anyway (the tests use a strict copy, which rejects anything undeclared) and builds the wrapper of the second.
"""

import copy
import json
from functools import cache
from pathlib import Path
from typing import Any, Dict, List, Optional

SUFFIX = ".v1.schema.json"
GENERATED_AT = {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$",
                "description": "When the data of this response was read from the controller (UTC). The age of the "
                               "oldest answer used, so a response built from the cache says how old it is."}
WARNINGS = {"type": "array", "items": {"type": "string"},
            "description": "What was degraded or served from the cache while building this response."}
EVENT_NOTES = {
    "truncated": {"type": "boolean", "description": "--limit cut the list: there are older events that match."},
    "read_cap_reached": {"type": "boolean", "description": "The controller read stopped at its 20,000-event cap."},
}


def schema_dir() -> Path:
    """Where the schema files are: the package data of an installed wheel, else ``docs/schemas`` of a checkout."""
    packaged = Path(__file__).resolve().parent.parent / "schemas"
    return packaged if packaged.is_dir() else Path(__file__).resolve().parent.parent.parent / "docs" / "schemas"


@cache
def names() -> List[str]:
    """The schemas that exist, by name (``wan``, ``query-ports``...). The only names ``/schemas/{name}`` serves."""
    return sorted(p.name[:-len(SUFFIX)] for p in schema_dir().glob(f"*{SUFFIX}"))


def load(name: str) -> Dict[str, Any]:
    """The schema called ``name``; ``KeyError`` for a name not in ``names()`` (so a path never reaches the disk)."""
    if name not in names():
        raise KeyError(name)
    return json.loads((schema_dir() / f"{name}{SUFFIX}").read_text(encoding="utf-8"))


def response_schema(name: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The schema of an API response for the document ``name``. ``extra`` adds declared keys (the event notes)."""
    schema = copy.deepcopy(load(name))
    if schema.get("type") == "array":
        defs = schema.pop("$defs", {})
        for key in ("$schema", "$id", "title", "description"):
            schema.pop(key, None)
        properties: Dict[str, Any] = {"items": schema, "generated_at": GENERATED_AT, "warnings": WARNINGS,
                                      **(extra or {})}
        return {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": f"urn:homelab-probe:api:{name}:v1",
                "title": f"{name} (API response)", "description": f"The `{name}` list with when it was read.",
                "type": "object", "properties": properties, "required": ["items", "generated_at", "warnings"],
                "$defs": defs}
    schema["properties"] = {**schema.get("properties", {}), "generated_at": GENERATED_AT, "warnings": WARNINGS,
                            **(extra or {})}
    schema["required"] = sorted({*schema.get("required", []), "generated_at", "warnings"})
    return schema
