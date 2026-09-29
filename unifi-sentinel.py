#!/usr/bin/env python3
"""UniFi Sentinel launcher. Run from the project root with: uv run unifi-sentinel.py <command>

Dependencies are declared once, in pyproject.toml.
"""

import sys

from unifi_sentinel.cli import main

if __name__ == "__main__":
    sys.exit(main())
