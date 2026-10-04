#!/usr/bin/env python3
"""Homelab Probe launcher. Run from the project root with: uv run hlp.py <command>

Dependencies are declared once, in pyproject.toml.
"""

import sys

from homelab_probe.cli import main

if __name__ == "__main__":
    sys.exit(main())
