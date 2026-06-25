"""
archive_validator.py -- Top-level entry point.

Usage:
    python archive_validator.py --input-dir /path/to/archive [options]

Run with --help for full usage information.
"""

import sys
from archive_validator.cli import run

if __name__ == "__main__":
    sys.exit(run())
