#!/usr/bin/env python3
"""Launcher so `python storage.py scan ...` works without installing anything."""
import sys
from dbms.storage.cli import main

if __name__ == "__main__":
    sys.exit(main())
