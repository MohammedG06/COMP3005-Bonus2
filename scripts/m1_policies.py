#!/usr/bin/env python3
"""REPORT_M1 table 2: hit rate of every trace under both policies at B = 4, 16, 64, 256.

  python scripts/m1_policies.py path/to/trace1.txt path/to/trace2.txt ...
Prints one markdown table with 4 rows per trace (20 rows for 5 traces), LRU and CLOCK side by side."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dbms.storage.trace import read_trace, run_trace

SIZES = (4, 16, 64, 256)


def main(paths):
    if not paths:
        sys.exit("usage: python scripts/m1_policies.py trace1.txt [trace2.txt ...]")
    print("| trace | B | accesses | LRU hits | LRU hit rate | CLOCK hits | CLOCK hit rate |")
    print("|---|---|---|---|---|---|---|")
    for p in paths:
        ids = read_trace(p)
        for b in SIZES:
            lru, clock = run_trace(ids, b, "lru"), run_trace(ids, b, "clock")
            n = len(ids)
            print(f"| {os.path.basename(p)} | {b} | {n} | {lru['hits']} | {lru['hits'] / n:.1%} | "
                  f"{clock['hits']} | {clock['hits'] / n:.1%} |")


if __name__ == "__main__":
    main(sys.argv[1:])
