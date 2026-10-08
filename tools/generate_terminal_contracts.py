"""Generate the version-one terminal wire schemas from the endpoint models (requires the web extra)."""

import argparse
import json
from pathlib import Path

from homelab_probe.server.terminal_api import (
    CapabilitiesResult,
    CompleteBody,
    CompleteResult,
    ExecuteBody,
    ExecuteResult,
    TerminalError,
)

ROOT = Path(__file__).resolve().parent.parent
MODELS = {"terminal-capabilities.v1": CapabilitiesResult,
          "terminal-execute-request.v1": ExecuteBody, "terminal-execute-result.v1": ExecuteResult,
          "terminal-complete-request.v1": CompleteBody, "terminal-complete-result.v1": CompleteResult,
          "terminal-error.v1": TerminalError}


def schemas() -> dict[str, str]:
    result = {}
    for name, model in MODELS.items():
        schema = model.model_json_schema()
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = f"urn:homelab-probe:{name}"
        result[name + ".schema.json"] = json.dumps(schema, indent=2, sort_keys=True) + "\n"
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "docs" / "terminal")
    directory = parser.parse_args().output
    directory.mkdir(exist_ok=True)
    for name, text in schemas().items():
        (directory / name).write_text(text, encoding="utf-8")
