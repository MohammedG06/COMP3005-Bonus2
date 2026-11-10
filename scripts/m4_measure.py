#!/usr/bin/env python3
"""REPORT_M4 measurements. Run on the course dataset and its 20 queries, paste the output.

  python scripts/m4_measure.py qerror      --db DIR --queries q20.sql --frames 32
  python scripts/m4_measure.py speedup     --db DIR --queries q20.sql --frames 32
  python scripts/m4_measure.py correlation --db DIR --table T
  python scripts/m4_measure.py rules       --db DIR --queries q20.sql --frames 32

qerror      estimated against actual rows at EVERY node of every plan, with median and worst q-error
speedup     page I/O of the optimizer's plan against the plan with no optimizer (joins in the order
            written, no rewrites, one fixed algorithm), per query, and their ratio
correlation finds pairs of columns of one table whose combined predicate is badly estimated
rules       total actual I/O of the workload with each rewrite rule switched off in turn (one row per rule)
Run `opt analyze` (or ANALYZE;) first, or the estimates use the documented defaults."""
import argparse
import itertools
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dbms.errors import DBError
from dbms.execution.runner import execute
from dbms.optimizer.api import optimize, est_vs_actual, unoptimized
from dbms.optimizer.estimate import cond_sel, CI
from dbms.optimizer.rewrite import ALL_RULES
from dbms.optimizer.stats import StatsProvider
from dbms.ra.condition import parse_condition
from dbms.sql.parser import parse_sql
from dbms.sql.translate import CatalogView, Compiler
from dbms.storage.database import Database
from dbms.system import split_statements


def load_queries(db_path, path):
    out = []
    with Database(db_path, 8) as db:
        cat = CatalogView.from_database(db)
    for i, (stmt, _) in enumerate(split_statements(open(path, encoding="utf-8").read()), 1):
        try:
            plan, _ = Compiler(cat).compile_query(parse_sql(stmt)[0])
            out.append((i, stmt.strip(), plan))
        except DBError as e:
            print(f"# query {i} skipped: {e.message}", file=sys.stderr)
    return out


def io(res):
    return res["totals"]["page_reads"] + res["totals"]["page_writes"]


def cmd_qerror(a):
    allq, rows = [], []
    for i, sql, plan in load_queries(a.db, a.queries):
        with Database(a.db, 16) as db:
            phys, info = optimize(db, plan, a.frames)
        res = execute(a.db, phys, a.frames)
        pairs = est_vs_actual(phys, res)
        pairs = [p for p in pairs if p["q_error"] is not None]          # nodes cut short by a LIMIT are not comparable
        qs = [p["q_error"] for p in pairs]
        allq += qs
        worst = max(pairs, key=lambda p: p["q_error"])
        rows.append((i, len(pairs), statistics.median(qs), worst["q_error"], worst["label"], worst["est_rows"], worst["actual_rows"]))
    print("| query | nodes | median q-error | worst q-error | worst node | est rows | actual rows |")
    print("|---|---|---|---|---|---|---|")
    for r in rows:
        print(f"| {r[0]} | {r[1]} | {r[2]:.2f} | {r[3]:.2f} | {r[4]} | {r[5]} | {r[6]} |")
    print(f"\nOver all {len(allq)} plan nodes of {len(rows)} queries: median q-error = {statistics.median(allq):.2f}, "
          f"worst = {max(allq):.2f}")


def cmd_speedup(a):
    print("| query | rows | optimizer I/O | no optimizer, nested loop I/O | ratio (nested loop / optimizer) | est cost | est vs actual I/O |")
    print("|---|---|---|---|---|---|---|")
    for i, sql, plan in load_queries(a.db, a.queries):
        with Database(a.db, 16) as db:
            phys, info = optimize(db, plan, a.frames)
        res = execute(a.db, phys, a.frames)
        try:
            base = execute(a.db, unoptimized(plan)("nested_loop_join"), a.frames)
        except DBError as e:
            print(f"| {i} | {len(res['rows'])} | {io(res)} | ERROR {e.message[:40]} | | | |")
            continue
        same = sorted(res["rows"]) == sorted(base["rows"])
        print(f"| {i} | {len(res['rows'])}{'' if same else ' (DIFFERENT ANSWER!)'} | {io(res)} | {io(base)} | "
              f"{io(base) / max(1, io(res)):.2f} | {info['total_cost']:.0f} | {info['total_cost'] / max(1, io(res)):.2f} |")


def cmd_correlation(a):
    with Database(a.db, 16) as db:
        sp = StatsProvider(db)
        tables = [a.table] if a.table else db.catalog.list_tables()
        cat = CatalogView.from_database(db)
    print("| table | columns | values | independence estimate | actual rows | q-error |")
    print("|---|---|---|---|---|---|")
    found = []
    for t in tables:
        with Database(a.db, 16) as db:
            ts = StatsProvider(db).table(t)
        cols = [c for c in cat.columns(t) if ts.cols[c.name].ndv >= 2]
        for c1, c2 in itertools.combinations(cols, 2):
            sql = f"SELECT {c1.name}, {c2.name}, COUNT(*) AS n FROM {t} GROUP BY {c1.name}, {c2.name} ORDER BY n DESC LIMIT 1;"
            plan, _ = Compiler(cat).compile_query(parse_sql(sql)[0])
            with Database(a.db, 16) as db:
                phys, _ = optimize(db, plan, a.frames)
            res = execute(a.db, phys, a.frames)
            if not res["rows"]:
                continue
            v1, v2, actual = res["rows"][0]
            if actual < max(10, ts.n_tuples // 100):         # tiny groups are noise, not correlation
                continue
            lit = lambda v: f"'{v}'" if isinstance(v, str) else str(v)
            cond = parse_condition(f"{c1.name} = {lit(v1)} and {c2.name} = {lit(v2)}")
            cols_ci = {(None, c.name): CI(ts.cols[c.name].ndv, ts.cols[c.name], 4.0) for c in (c1, c2)}
            est = max(1, round(ts.n_tuples * cond_sel(cond, cols_ci)))
            q = max(est, actual) / min(est, actual)
            found.append((q, t, c1.name, c2.name, v1, v2, est, actual))
    for q, t, c1, c2, v1, v2, est, actual in sorted(found, reverse=True)[:10]:
        print(f"| {t} | {c1}, {c2} | {v1!r}, {v2!r} | {est} | {actual} | {q:.1f} |")
    print("\nThe top rows are candidates: the optimizer multiplies the two selectivities (independence), the data does not.")


RULE_NAMES = {1: "split conjunctions", 2: "push select below join/cross", 3: "cross + equality -> join",
              4: "push select through rename/project/union", 5: "push select below group_by",
              6: "push projections down", 7: "drop no-op projections, merge selects"}


def cmd_rules(a):
    """One row per rule: total actual page I/O of the whole workload with the rule on and with it off."""
    qs = load_queries(a.db, a.queries)

    def total(rules):
        t = 0
        for i, sql, plan in qs:
            with Database(a.db, 16) as db:
                phys, info = optimize(db, plan, a.frames, rules)
            t += io(execute(a.db, phys, a.frames))
        return t

    base = total(ALL_RULES)
    print(f"| rule | what it does | total I/O, all rules on | total I/O, this rule off | change |")
    print("|---|---|---|---|---|")
    for r in range(1, 8):
        off = total(ALL_RULES - {r})
        print(f"| {r} | {RULE_NAMES[r]} | {base} | {off} | {off - base:+d} ({(off / max(1, base) - 1) * 100:+.1f}%) |")
    print("\nA rule that changes nothing here is worth nothing on this data; say why (for example, no query has a "
          "predicate it could move).")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["qerror", "speedup", "correlation", "rules"])
    ap.add_argument("--db", required=True)
    ap.add_argument("--queries")
    ap.add_argument("--table")
    ap.add_argument("--frames", type=int, default=32)
    a = ap.parse_args()
    if a.cmd != "correlation" and not a.queries:
        sys.exit("--queries FILE is required")
    {"qerror": cmd_qerror, "speedup": cmd_speedup, "correlation": cmd_correlation, "rules": cmd_rules}[a.cmd](a)
