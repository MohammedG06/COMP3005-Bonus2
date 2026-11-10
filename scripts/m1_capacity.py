#!/usr/bin/env python3
"""REPORT_M1 table 1: records per page, computed by hand from spec section 3 vs measured.

  python scripts/m1_capacity.py
Computed = floor(4088 / (L + 4)) where L is the record length of the rows we load.
Measured = live records on a full data page of the loaded table (slot_count of page 1)."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dbms.storage import page as pg
from dbms.storage.database import Database
from dbms.storage.records import parse_schema

CASES = [
    ("all INT (4 columns)", "a:INT,b:INT,c:INT,d:INT", lambda i: (i, i, i, i), 16),
    ("short VARCHAR(10), 8-char values", "a:INT,b:VARCHAR(10),c:INT", lambda i: (i, "abcdefgh", i), 4 + 2 + 8 + 4),
    ("VARCHAR(200), 200-char values", "a:INT,b:VARCHAR(200)", lambda i: (i, "x" * 200), 4 + 2 + 200),
]


def main():
    print("| schema | record bytes L | computed floor(4088/(L+4)) | measured | difference |")
    print("|---|---|---|---|---|")
    for name, schema, mk, L in CASES:
        computed = 4088 // (L + 4)
        with tempfile.TemporaryDirectory() as d:
            with Database(d, 16, create=True) as db:
                db.create_table("T", parse_schema(schema))
                heap, _ = db.open_heap("T")
                for i in range(computed * 3 + 1):
                    heap.insert(mk(i))
                fr = db.pool.fetch_page(1, heap.file_id)          # first data page, full
                measured = pg.slot_count(fr.data)
                db.pool.unpin_page(1, False, heap.file_id)
        print(f"| {name} | {L} | {computed} | {measured} | {measured - computed} |")


if __name__ == "__main__":
    main()
