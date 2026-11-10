#!/usr/bin/env python3
"""Hidden workload runner (spec section 6): for every query print the result row count, page I/O,
wall time, and estimated against actual rows at the root.

  python scripts/run_workload.py --db DIR --queries queries.sql --frames 32 [--csv out.csv] [--analyze]

--analyze runs ANALYZE on every table first (do this once after loading the dataset).
Queries are separated by ';'. A failing query is reported and the run continues."""
import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dbms.errors import DBError, format_error
from dbms.system import Engine, split_statements


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--queries", required=True)
    ap.add_argument("--frames", type=int, default=32)
    ap.add_argument("--csv")
    ap.add_argument("--analyze", action="store_true")
    a = ap.parse_args()
    eng = Engine(a.db, a.frames)
    if a.analyze:
        for r in eng.run("ANALYZE;"):
            print(r.message)
    text = open(a.queries, encoding="utf-8").read()
    rows = []
    print("| # | rows | page reads | page writes | seconds | est rows (root) | actual rows (root) | est cost | q-error root |")
    print("|---|---|---|---|---|---|---|---|---|")
    for i, (stmt, off) in enumerate(split_statements(text), 1):
        try:
            r = eng.run_one(stmt)
        except DBError as e:
            print(f"| {i} | ERROR | | | | | | | {format_error(e)} |")
            rows.append([i, "ERROR", format_error(e)])
            continue
        if r.kind != "select":
            continue
        s = r.stats
        q = max(max(1, s["est_rows_root"]), max(1, s["actual_rows_root"])) / min(max(1, s["est_rows_root"]), max(1, s["actual_rows_root"]))
        print(f"| {i} | {s['rows']} | {s['page_reads']} | {s['page_writes']} | {s['seconds']:.3f} | {s['est_rows_root']} | "
              f"{s['actual_rows_root']} | {s['est_cost']:.0f} | {q:.2f} |")
        rows.append([i, s["rows"], s["page_reads"], s["page_writes"], f"{s['seconds']:.4f}", s["est_rows_root"],
                     s["actual_rows_root"], f"{s['est_cost']:.0f}", f"{q:.2f}"])
    if a.csv:
        with open(a.csv, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["query", "rows", "page_reads", "page_writes", "seconds", "est_rows_root", "actual_rows_root", "est_cost", "q_error_root"])
            w.writerows(rows)


if __name__ == "__main__":
    main()
