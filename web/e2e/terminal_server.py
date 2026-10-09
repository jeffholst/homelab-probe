"""Test-only launcher: serve the preview bundle through the real synthetic API's static-file path."""

import sys
from pathlib import Path

from homelab_probe import cli
from homelab_probe.server import static

static.DEFAULT_ROOT = Path(sys.argv[1])
raise SystemExit(cli.main(sys.argv[2:]))
