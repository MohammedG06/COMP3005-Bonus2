#!/usr/bin/env python3
"""Launcher so `python sql.py compile ...` works without installing anything."""
import sys
from dbms.sql.cli import main

if __name__ == "__main__":
    sys.exit(main())
