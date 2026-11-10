#!/usr/bin/env python3
"""REPORT_M1 table 3: page reads for a full scan of a 500-data-page table at B = 2, 8, 64.

  python scripts/m1_scan_cost.py          (takes a few seconds to load ~100k rows)"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dbms.storage.database import Database
from dbms.storage.records import parse_schema

PAGES = 500
PER_PAGE = 4088 // (16 + 4)       # 204 records of 16 bytes


def main():
    with tempfile.TemporaryDirectory() as d:
        with Database(d, 16, create=True) as db:
            db.create_table("T", parse_schema("a:INT,b:INT,c:INT,d:INT"))
            heap, _ = db.open_heap("T")
            for i in range(PAGES * PER_PAGE):
                heap.insert((i, i, i, i))
            db.refresh_counts("T", PAGES * PER_PAGE)
            info = db.catalog.get_table("T")
        print(f"table T: {info.num_tuples} tuples on {info.num_pages} data pages\n")
        print("| B | page reads | hits | misses | evictions | max pinned during scan |")
        print("|---|---|---|---|---|---|")
        for b in (2, 8, 64):
            with Database(d, b) as db:
                heap, _ = db.open_heap("T")
                db.pool.clear()
                db.pool.reset_stats()
                peak = 0
                for _ in heap.scan():
                    peak = max(peak, db.pool.pinned_count())
                s = db.pool.stats()
                print(f"| {b} | {s['reads']} | {s['hits']} | {s['misses']} | {s['evictions']} | {peak} |")


if __name__ == "__main__":
    main()
