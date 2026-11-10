#!/usr/bin/env python3
"""Random differential test of the WHOLE pipeline (a development tool, not a graded deliverable).

  python scripts/fuzz_pipeline.py --seeds 1-30 --queries 40

For each random SQL query: compile it (Module 3), optimize it with a random subset of rewrite rules
switched off (Module 4) at three frame budgets, execute it (Module 2 on Module 1) and compare the rows with
the reference evaluator run directly on the logical plan. Any difference is a bug. It uses only this
project's own code (no database engine), so it is fine to keep in the repository."""
import argparse
import os
import random
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dbms.errors import DBError
from dbms.execution.runner import execute
from dbms.optimizer.api import optimize
from dbms.optimizer.rewrite import ALL_RULES
from dbms.optimizer.stats import analyze_table
from dbms.ra.evaluate import evaluate
from dbms.ra.tables import InMemoryProvider
from dbms.schema import Schema
from dbms.sql.translate import CatalogView, compile_query_text
from dbms.storage.database import Database
from dbms.storage.records import parse_schema

TABLES = {"A": "id:INT,x:INT,y:INT,s:VARCHAR(6),pad:VARCHAR(120)", "B": "id:INT,x:INT,z:INT,s:VARCHAR(6),pad:VARCHAR(120)",
          "C": "id:INT,y:INT,w:INT,pad:VARCHAR(120)", "D": "id:INT,z:INT,w:INT,pad:VARCHAR(120)"}
SIZES = {"A": 70, "B": 50, "C": 35, "D": 25}
LINKS = {("A", "B"): "x", ("A", "C"): "y", ("B", "D"): "z", ("C", "D"): "w"}


def build(path, rnd, analyze):
    prov = InMemoryProvider()
    with Database(path, 16, create=True) as db:
        for t, sch in TABLES.items():
            cols = parse_schema(sch)
            db.create_table(t, cols)
            h, _ = db.open_heap(t)
            rows = []
            for i in range(SIZES[t]):
                r = tuple(i if c.name == "id" else (rnd.randrange(12) if c.type == "INT" else
                          (rnd.choice("abcdef") * rnd.randrange(1, 3) if c.name == "s" else "p" * 100)) for c in cols)
                h.insert(r)
                rows.append(r)
            db.refresh_counts(t, len(rows))
            prov.add(t, Schema(cols), rows)
        if analyze:
            for t in TABLES:
                analyze_table(db, t, rnd.choice([3, 8, 20]), 8)
        return prov, CatalogView.from_database(db)


def cond(rnd, cols):
    c = rnd.choice(cols)
    base = f"{c} {rnd.choice(['=', '<>', '<', '<=', '>', '>='])} {rnd.randrange(12)}"
    r = rnd.random()
    if r < .15:
        return f"NOT ({base})"
    if r < .35:
        return f"({base} {rnd.choice(['AND', 'OR'])} {rnd.choice(cols)} {rnd.choice(['<', '>', '='])} {rnd.randrange(12)})"
    return base


def gen(rnd):
    k = rnd.random()
    if k < .45:
        n = rnd.choice([2, 3, 3, 4])
        names = rnd.sample(["A", "B", "C", "D"], n)
        al = {t: t.lower() for t in names}
        preds = []
        for i in range(1, n):
            for j in range(i):
                key = (names[j], names[i]) if (names[j], names[i]) in LINKS else (names[i], names[j])
                if key in LINKS:
                    preds.append(f"{al[names[j]]}.{LINKS[key]} = {al[names[i]]}.{LINKS[key]}")
                    break
            else:
                preds.append(f"{al[names[i]]}.id = {al[names[0]]}.id")
        numeric = [f"{al[t]}.id" for t in names] + [f"{al[t]}.{c.name}" for t in names for c in parse_schema(TABLES[t])
                                                     if c.type == "INT" and c.name != "id"]
        extra = [cond(rnd, numeric) for _ in range(rnd.randrange(0, 3))]
        sel = ", ".join(rnd.sample(numeric, rnd.randrange(1, 4)))
        if rnd.random() < .5:
            return f"SELECT {sel} FROM " + ", ".join(f"{t} {al[t]}" for t in names) + " WHERE " + " AND ".join(preds + extra)
        q = f"SELECT {sel} FROM {names[0]} {al[names[0]]}"
        for i in range(1, n):
            q += f" JOIN {names[i]} {al[names[i]]} ON {preds[i - 1]}"
        return q + (" WHERE " + " AND ".join(extra) if extra else "")
    if k < .65:
        g = rnd.choice(["A.x", "A.y", "A.s"])
        fn = rnd.choice(["COUNT(*)", "SUM(A.y)", "MIN(A.id)", "MAX(A.x)"])
        q = f"SELECT {g}, {fn} AS v FROM A" + (f" WHERE {cond(rnd, ['A.id', 'A.x', 'A.y'])}" if rnd.random() < .5 else "") + f" GROUP BY {g}"
        if rnd.random() < .5:
            q += f" HAVING COUNT(*) {rnd.choice(['>', '<', '>='])} {rnd.randrange(8)}"
        return q
    if k < .8:
        op = rnd.choice(["UNION", "UNION ALL", "INTERSECT", "EXCEPT"])
        return (f"SELECT x FROM A WHERE {cond(rnd, ['A.id', 'A.x', 'A.y'])} {op} "
                f"SELECT x FROM B WHERE {cond(rnd, ['B.id', 'B.x', 'B.z'])}")
    if k < .9:
        return "SELECT t.x, t.n, b.z FROM (SELECT x, COUNT(*) AS n FROM A GROUP BY x) AS t JOIN B b ON t.x = b.x WHERE t.n > 1"
    return (f"SELECT DISTINCT a.x, b.z FROM A a, B b WHERE a.x = b.x AND a.id > {rnd.randrange(100)} "
            f"ORDER BY a.x, b.z LIMIT {rnd.randrange(1, 40)}")


def run_seed(seed, n_queries):
    rnd = random.Random(seed)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "db")
        prov, cat = build(path, rnd, analyze=bool(seed % 2))
        bad = ok = 0
        for _ in range(n_queries):
            q = gen(rnd)
            try:
                (_, plan, _), = compile_query_text(q, cat)
            except DBError as e:
                print("COMPILE ERROR", q, e.message)
                bad += 1
                continue
            ref = sorted(evaluate(plan, prov).rows)
            limited = "LIMIT" in q
            fine = True
            for B in (4, 6, 16):
                off = set(rnd.sample(range(1, 8), rnd.choice([0, 1, 2, 3])))
                try:
                    with Database(path, 16) as db:
                        phys, _ = optimize(db, plan, B, ALL_RULES - off)
                    res = execute(path, phys, B)
                except Exception as e:
                    print(f"FAIL B={B} rules off={sorted(off)}: {q}\n   {type(e).__name__}: {getattr(e, 'message', e)}")
                    fine = False
                    break
                got = sorted(res["rows"])
                same = len(got) == len(ref) if limited else got == ref
                if not same or res["peak_frames"] > B:
                    print(f"MISMATCH B={B} rules off={sorted(off)} peak={res['peak_frames']}: {q}  ({len(got)} rows, expected {len(ref)})")
                    fine = False
                    break
            ok += fine
            bad += not fine
        return ok, bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", default="1-10", help="e.g. 7 or 1-30")
    ap.add_argument("--queries", type=int, default=40)
    a = ap.parse_args()
    lo, _, hi = a.seeds.partition("-")
    seeds = range(int(lo), int(hi or lo) + 1)
    t0, total_ok, total_bad = time.time(), 0, 0
    for s in seeds:
        ok, bad = run_seed(s, a.queries)
        total_ok += ok
        total_bad += bad
    print(f"seeds {a.seeds}: {total_ok} queries ok, {total_bad} bad, {time.time() - t0:.0f}s")
    sys.exit(1 if total_bad else 0)


if __name__ == "__main__":
    main()
