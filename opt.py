#!/usr/bin/env python3
"""Launcher so `python opt.py plan ...` works without installing anything."""
import sys
from dbms.optimizer.cli import main

if __name__ == "__main__":
    sys.exit(main())
