#!/usr/bin/env python3
"""Launcher so `python exec.py run ...` works without installing anything."""
import sys
from dbms.execution.cli import main

if __name__ == "__main__":
    sys.exit(main())
