"""Connectors: one read-only CLI per platform (login, status, fetch, keepalive, manifest). See docs/CONNECTORS.md."""
import sys

if sys.version_info < (3, 11):
    sys.exit(f"connectors need Python 3.11+, this is {sys.version.split()[0]} (macOS /usr/bin/python3 is 3.9: brew install python)")
