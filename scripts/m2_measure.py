#!/usr/bin/env python3
"""REPORT_M2 measurements. Run on your own machine and paste the output.

  python scripts/m2_measure.py cost        # cost vs formula for the three joins (spec section 5, table 1)
  python scripts/m2_measure.py passes      # sort passes vs formula (table 2)
  python scripts/m2_measure.py crossover   # cheapest join as B varies, then as size varies (table 3)
  python scripts/m2_measure.py skew        # 40% hot key on both sides (table 4)
  python scripts/m2_measure.py budget      # sort of 20 x B pages at B = 4 (table 5)
  python scripts/m2_measure.py all
  add --quick for smaller relations (a few seconds)

Relations use 150 character padding so that a page holds 24 tuples: many pages, few tuples,
which keeps the pure-Python nested loop (|R| x |S| comparisons) affordable."""
import math
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dbms.execution.runner import execute
from dbms.storage.database import Database
from dbms.storage.records import parse_schema

PAD = "p" * 150
PER_PAGE = 24
JOINS = ("nested_loop_join", "sort_merge_join", "hash_join")


def make_db(path, specs):
    """specs: {name: (schema, rows)}"""
    with Database(path, 16, create=True) as db:
        for name, (schema, rows) in specs.items():
            db.create_table(name, parse_schema(schema))
            heap, _ = db.open_heap(name)
            for r in rows:
                heap.insert(r)
            db.refresh_counts(name, len(rows))
        return {n: db.catalog.get_table(n).num_pages for n in specs}


def rel_rows(pages, domain, seed, hot=0.0):
    rnd = random.Random(seed)
    n = pages * PER_PAGE
    return [(7 if rnd.random() < hot else rnd.randrange(100, 100 + domain), i, PAD) for i in range(n)]


def join_plan(kind):
    return {"op": kind, "condition": "R.k = S.k", "left": {"op": "seqscan", "table": "R"},
            "right": {"op": "seqscan", "table": "S"}}


def sort_formula_passes(N, B):
    r0 = math.ceil(N / B)
    return 1 if r0 <= 1 else 1 + math.ceil(math.log(r0) / math.log(B - 1) - 1e-9)


def formula(kind, M, N, B):
    """Textbook (reads, writes). Block nested loop: M + ceil(M/(B-2)) N. Hash join: 3(M+N).
    Sort-merge: sort(M) + sort(N) + M + N, where sort(X) = 2 X passes (every pass reads and writes
    X pages, because the sorted file is written for the merge), plus one merge read of each file."""
    if kind == "nested_loop_join":
        return M + math.ceil(M / (B - 2)) * N, 0
    if kind == "hash_join":
        return 2 * (M + N), M + N
    pm, pn = sort_formula_passes(M, B), sort_formula_passes(N, B)
    return M * pm + N * pn + M + N, M * pm + N * pn


def tmp_db(specs):
    tmp = tempfile.TemporaryDirectory()
    path = os.path.join(tmp.name, "db")
    pages = make_db(path, specs)
    return tmp, path, pages


def cmd_cost(quick):
    sizes = (10, 20, 30) if quick else (20, 40, 80)
    print("| join | M=N (pages) | B | measured reads | measured writes | formula reads | formula writes | measured/formula I/O |")
    print("|---|---|---|---|---|---|---|---|")
    for sz in sizes:
        tmp, path, pages = tmp_db({"R": ("k:INT,a:INT,pad:VARCHAR(200)", rel_rows(sz, sz * PER_PAGE, 1)),
                                   "S": ("k:INT,b:INT,pad:VARCHAR(200)", rel_rows(sz, sz * PER_PAGE, 2))})
        M, N = pages["R"], pages["S"]
        for kind in JOINS:
            for B in (4, 16, 64, 256):
                res = execute(path, join_plan(kind), B)
                t = res["totals"]
                fr, fw = formula(kind, M, N, B)
                ratio = (t["page_reads"] + t["page_writes"]) / max(1, fr + fw)
                print(f"| {kind} | {M} | {B} | {t['page_reads']} | {t['page_writes']} | {fr} | {fw} | {ratio:.2f} |")
        tmp.cleanup()


def cmd_passes(quick):
    print("| N (pages) | B | initial runs (mine) | passes measured | passes spec formula 1+ceil(log_{B-1} ceil(N/B)) | note |")
    print("|---|---|---|---|---|---|")
    combos = [(40, 4), (80, 4), (160, 4), (80, 8), (160, 8), (200, 6), (60, 16)]
    if quick:
        combos = combos[:4]
    for N, B in combos:
        rows = [(i, (i * 7919) % 100003, PAD) for i in range(N * PER_PAGE)]
        tmp, path, pages = tmp_db({"R": ("a:INT,k:INT,pad:VARCHAR(200)", rows)})
        res = execute(path, {"op": "sort", "keys": [["R.k", "asc"]], "child": {"op": "seqscan", "table": "R"}}, B)
        info = res["nodes"][0]["info"]
        note = "N = 20 x B" if N == 20 * B else ""
        print(f"| {pages['R']} | {B} | {info['initial_runs']} | {info['passes']} | {sort_formula_passes(pages['R'], B)} | {note} |")
        tmp.cleanup()


def total_io(res):
    return res["totals"]["page_reads"] + res["totals"]["page_writes"]


def cmd_crossover(quick):
    sz = 20 if quick else 40
    tmp, path, pages = tmp_db({"R": ("k:INT,a:INT,pad:VARCHAR(200)", rel_rows(sz, sz * PER_PAGE, 1)),
                               "S": ("k:INT,b:INT,pad:VARCHAR(200)", rel_rows(sz, sz * PER_PAGE, 2))})
    print(f"Fixed relations: R = {pages['R']} pages, S = {pages['S']} pages. Total page I/O (reads + writes):\n")
    print("| B | nested_loop_join | sort_merge_join | hash_join | cheapest |")
    print("|---|---|---|---|---|")
    for B in (4, 5, 6, 8, 12, 16, 24, 32, 48, 64, 128):
        costs = {k: total_io(execute(path, join_plan(k), B)) for k in JOINS}
        best = min(costs, key=costs.get)
        print(f"| {B} | {costs['nested_loop_join']} | {costs['sort_merge_join']} | {costs['hash_join']} | {best} |")
    tmp.cleanup()
    B = 16
    print(f"\nFixed B = {B}, relation size varies (M = N):\n")
    print("| pages | nested_loop_join | sort_merge_join | hash_join | cheapest |")
    print("|---|---|---|---|---|")
    for sz in ((5, 10, 15, 20, 30) if quick else (5, 10, 15, 20, 30, 40, 60, 80)):
        tmp, path, pages = tmp_db({"R": ("k:INT,a:INT,pad:VARCHAR(200)", rel_rows(sz, sz * PER_PAGE, 1)),
                                   "S": ("k:INT,b:INT,pad:VARCHAR(200)", rel_rows(sz, sz * PER_PAGE, 2))})
        costs = {k: total_io(execute(path, join_plan(k), B)) for k in JOINS}
        best = min(costs, key=costs.get)
        print(f"| {pages['R']} | {costs['nested_loop_join']} | {costs['sort_merge_join']} | {costs['hash_join']} | {best} |")
        tmp.cleanup()


def cmd_skew(quick):
    """Uniform keys against 40% of the rows on one key, on both sides, at two budgets. At B = 4 even uniform data
    overflows (3 frames per partition is tiny), so B = 16 is the clearer contrast: uniform fits, skew does not."""
    sz = 15 if quick else 30
    print("| B | data | hash_join page reads | page writes | total I/O | comparisons | overflow handling | build partition pages (left) |")
    print("|---|---|---|---|---|---|---|---|")
    for B in (4, 16):
        for label, hot in (("uniform", 0.0), ("40% one key on both sides", 0.4)):
            tmp, path, pages = tmp_db({"R": ("k:INT,a:INT,pad:VARCHAR(200)", rel_rows(sz, 4000, 1, hot)),
                                       "S": ("k:INT,b:INT,pad:VARCHAR(200)", rel_rows(sz, 4000, 2, hot))})
            res = execute(path, join_plan("hash_join"), B)
            node = res["nodes"][0]
            t = res["totals"]
            actions = ", ".join(sorted({o["action"] for o in node["info"]["overflow"]})) or "none"
            print(f"| {B} | {label} | {t['page_reads']} | {t['page_writes']} | {t['page_reads'] + t['page_writes']} | "
                  f"{t['comparisons']} | {actions} | {node['info']['left_partition_pages']} |")
            tmp.cleanup()


def cmd_budget(quick):
    B = 4
    rows = [(i, (i * 7919) % 100003, PAD) for i in range(20 * B * PER_PAGE + 100)]
    tmp, path, pages = tmp_db({"R": ("a:INT,k:INT,pad:VARCHAR(200)", rows)})
    res = execute(path, {"op": "sort", "keys": [["R.k", "asc"]], "child": {"op": "seqscan", "table": "R"}}, B)
    ks = [r[1] for r in res["rows"]]
    print(f"input: {pages['R']} pages = {pages['R'] / B:.1f} x B at B = {B}")
    print(f"completed: {len(res['rows'])} rows, sorted = {ks == sorted(ks)}")
    print(f"largest number of frames held at once (pinned + leased, tracked by the buffer pool on every pin and lease): "
          f"{res['peak_frames']} of {B}")
    print(f"page I/O: {res['totals']['page_reads']} reads, {res['totals']['page_writes']} writes; "
          f"passes = {res['nodes'][0]['info']['passes']}")
    tmp.cleanup()


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--quick"]
    quick = "--quick" in sys.argv
    cmds = {"cost": cmd_cost, "passes": cmd_passes, "crossover": cmd_crossover, "skew": cmd_skew, "budget": cmd_budget}
    if not args or args[0] not in list(cmds) + ["all"]:
        sys.exit(__doc__)
    for name in (cmds if args[0] == "all" else [args[0]]):
        print(f"\n## {name}\n")
        cmds[name](quick)
