#!/usr/bin/env python3
# /// script
# requires-python = ">=3.8"
# dependencies = [
#   "requests",
#   "python-dotenv",
# ]
# ///
"""UniFi Sentinel launcher. Run with: uv run unifi-sentinel.py <command>"""

import sys

from unifi_sentinel.cli import main

if __name__ == "__main__":
    sys.exit(main())
