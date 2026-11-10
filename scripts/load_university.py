#!/usr/bin/env python3
"""Builds a database from the course's university insert file (smallRelationsInsertFile.sql).

  python scripts/load_university.py --sql smallRelationsInsertFile.sql --out uni_db

The textbook DDL uses NUMERIC, CHECK constraints and one NULL, none of which exist in this project, so the
tables are recreated with INT and VARCHAR only: NUMERIC columns become INT (the data is whole numbers),
and the single NULL grade in `takes` is stored as the empty string ''. IDs stay VARCHAR (leading zeros)."""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dbms.optimizer.stats import analyze_table
from dbms.storage.database import Database
from dbms.storage.records import parse_schema

SCHEMAS = {
    "classroom": "building:VARCHAR(15),room_number:VARCHAR(7),capacity:INT",
    "department": "dept_name:VARCHAR(20),building:VARCHAR(15),budget:INT",
    "course": "course_id:VARCHAR(8),title:VARCHAR(50),dept_name:VARCHAR(20),credits:INT",
    "instructor": "ID:VARCHAR(5),name:VARCHAR(20),dept_name:VARCHAR(20),salary:INT",
    "section": "course_id:VARCHAR(8),sec_id:VARCHAR(8),semester:VARCHAR(6),year:INT,building:VARCHAR(15),room_number:VARCHAR(7),time_slot_id:VARCHAR(4)",
    "teaches": "ID:VARCHAR(5),course_id:VARCHAR(8),sec_id:VARCHAR(8),semester:VARCHAR(6),year:INT",
    "student": "ID:VARCHAR(5),name:VARCHAR(20),dept_name:VARCHAR(20),tot_cred:INT",
    "takes": "ID:VARCHAR(5),course_id:VARCHAR(8),sec_id:VARCHAR(8),semester:VARCHAR(6),year:INT,grade:VARCHAR(2)",
    "advisor": "s_ID:VARCHAR(5),i_ID:VARCHAR(5)",
    "time_slot": "time_slot_id:VARCHAR(4),day:VARCHAR(1),start_hr:INT,start_min:INT,end_hr:INT,end_min:INT",
    "prereq": "course_id:VARCHAR(8),prereq_id:VARCHAR(8)",
}


def split_values(s):
    out, buf, in_q, i = [], [], False, 0
    while i < len(s):
        c = s[i]
        if in_q:
            if c == "'" and i + 1 < len(s) and s[i + 1] == "'":
                buf.append("'")
                i += 1
            elif c == "'":
                in_q = False
            else:
                buf.append(c)
        elif c == "'":
            in_q = True
        elif c == ",":
            out.append("".join(buf).strip())
            buf = []
        else:
            buf.append(c)
        i += 1
    out.append("".join(buf).strip())
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sql", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    rows = {t: [] for t in SCHEMAS}
    for line in open(a.sql, encoding="utf-8"):
        line = line.strip().rstrip(";")
        if not line.lower().startswith("insert into"):
            continue
        rest = line[len("insert into"):].strip()
        table, _, vals = rest.partition(" values")
        vals = vals.strip()[1:-1]
        rows[table.strip()].append(split_values(vals))
    with Database(a.out, 32, create=True) as db:
        for t, sch in SCHEMAS.items():
            cols = parse_schema(sch)
            db.create_table(t, cols)
            heap, _ = db.open_heap(t)
            for r in rows[t]:
                vals = tuple(("" if v.lower() == "null" else v) if c.type == "VARCHAR" else int(v) for c, v in zip(cols, r))
                heap.insert(vals)
            db.refresh_counts(t, len(rows[t]))
            analyze_table(db, t, 5, 8)
            print(f"{t}: {len(rows[t])} rows")


if __name__ == "__main__":
    main()
