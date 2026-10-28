"""pfin: an agent-first personal finance framework. See README.md."""
import sys
if sys.version_info<(3,11):sys.exit(f'pfin needs Python 3.11+, this is {sys.version.split()[0]} (macOS /usr/bin/python3 is 3.9: brew install python)')
__version__="0.1.0"
