"""Module 4 tests: statistics, estimates, rewrites, the join order search, and whole-pipeline correctness."""
import contextlib
import io
import json
import math
import os
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dbms.errors import DBError
from dbms.execution.runner import execute
from dbms.optimizer import cli
from dbms.optimizer.api import optimize, est_vs_actual
from dbms.optimizer.estimate import Estimator, cond_sel
from dbms.optimizer.planner import Planner
from dbms.optimizer.rewrite import Rewriter, ALL_RULES
from dbms.optimizer.stats import analyze_table, TableStats, ColStats, Bucket, StatsProvider
from dbms.ra.condition import parse_condition
from dbms.ra.evaluate import evaluate
from dbms.ra.nodes import render_tree
from dbms.ra.plan_json import to_dict, from_dict
from dbms.ra.tables import InMemoryProvider
from dbms.schema import Schema
from dbms.sql.translate import CatalogView, compile_query_text
from dbms.storage.database import Database
from dbms.storage.records import parse_schema


# ----------------------------------------------------------------------------- a stub statistics provider
class StubStats:
    def __init__(self, cat, tables):
        self.cat, self.t = cat, tables

    def table(self, name):
        return self.t[name]

    def columns(self, name):
        return self.cat.columns(name)


def ts(name, n, pages, cols, width=80.0):
    return TableStats(name, n, pages, width, {c: ColStats(c, "INT", ndv, lo, hi, 4.0, []) for c, (ndv, lo, hi) in cols.items()})


def logical(sql, cat):
    return compile_query_text(sql, cat)[0][1]


class SpecNumbers(unittest.TestCase):
    """The worked example of spec section 2: 10,000 employees, did has 50 values, age is 20..70."""

    def setUp(self):
        self.cat = CatalogView.from_dict({"Emp": "eid:INT,did:INT,age:INT", "Dept": "did:INT,title:VARCHAR(10)"})
        self.stats = StubStats(self.cat, {
            "Emp": ts("Emp", 10000, 200, {"eid": (10000, 0, 9999), "did": (50, 0, 49), "age": (51, 20, 70)}),
            "Dept": ts("Dept", 50, 1, {"did": (50, 0, 49), "title": (50, None, None)})})
        self.est = Estimator(self.stats, self.cat)

    def rows(self, sql):
        return self.est.est(logical(sql, self.cat)).rows

    def test_equality_range_and_conjunction(self):
        self.assertEqual(self.rows("SELECT eid FROM Emp WHERE did = 7"), 200)
        self.assertEqual(self.rows("SELECT eid FROM Emp WHERE age > 60"), 2000)
        self.assertEqual(self.rows("SELECT eid FROM Emp WHERE did = 7 AND age > 60"), 40)

    def test_or_not_and_default_cases(self):
        self.assertEqual(self.rows("SELECT eid FROM Emp WHERE did = 7 OR age > 60"), math.ceil(10000 * (0.02 + 0.2 - 0.004)))
        self.assertEqual(self.rows("SELECT eid FROM Emp WHERE NOT did = 7"), 9800)
        self.assertEqual(self.rows("SELECT eid FROM Emp WHERE did <> 7"), 9800)

    def test_equijoin_uses_max_ndv(self):
        self.assertEqual(self.rows("SELECT e.eid FROM Emp e, Dept d WHERE e.did = d.did"), 10000)   # 10000 x 50 / 50

    def test_estimates_never_drop_below_one_row(self):
        self.assertEqual(self.rows("SELECT eid FROM Emp WHERE did = 7 AND did = 8 AND age < 20"), 1)


class Histograms(unittest.TestCase):
    def test_skewed_equality_uses_the_bucket(self):
        cs = ColStats("x", "INT", 11, 0, 100, 4.0, [Bucket(0, 0, 900, 1), Bucket(1, 100, 100, 10)])
        from dbms.optimizer.estimate import CI
        ci = CI(11, cs, 4.0)
        self.assertAlmostEqual(cond_sel(parse_condition("x = 0"), {(None, "x"): ci}), 0.9)
        self.assertAlmostEqual(cond_sel(parse_condition("x = 50"), {(None, "x"): ci}), 100 / (10 * 1000))
        self.assertAlmostEqual(cond_sel(parse_condition("x <= 0"), {(None, "x"): ci}), 0.9)
        self.assertLess(cond_sel(parse_condition("x > 0"), {(None, "x"): ci}), 0.101)


class RewriteExample(unittest.TestCase):
    CAT = CatalogView.from_dict({"Emp": "eid:INT,name:VARCHAR(10),age:INT,did:INT", "Dept": "did:INT,title:VARCHAR(10),budget:INT"})

    def test_rules_one_two_three_six(self):
        plan = logical("SELECT e.name, d.title FROM Emp e, Dept d WHERE e.did = d.did AND e.age > 60 AND d.budget > 1000", self.CAT)
        rw = Rewriter(self.CAT)
        out = rw.run(plan)
        self.assertTrue({1, 2, 3, 6} <= set(rw.fired), rw.fired)
        text = render_tree(out)
        self.assertIn("Join(cond=Eq(Attr(e.did), Attr(d.did)))", text)
        self.assertNotIn("Cross", text)
        d = to_dict(out)
        join = d["child"]
        self.assertEqual(join["op"], "join")
        for side in ("left", "right"):                          # a projection above a pushed-down selection
            self.assertEqual(join[side]["op"], "project")
            self.assertEqual(join[side]["child"]["op"], "select")
        self.assertEqual(join["left"]["columns"], ["e.name", "e.did"])
        self.assertEqual(join["left"]["child"]["condition"], "e.age > 60")
        self.assertEqual(join["right"]["child"]["condition"], "d.budget > 1000")

    def test_each_rule_can_be_switched_off(self):
        plan = logical("SELECT e.name FROM Emp e, Dept d WHERE e.did = d.did AND e.age > 60", self.CAT)
        full = render_tree(Rewriter(self.CAT).run(plan))
        for rule in (2, 3):
            off = render_tree(Rewriter(self.CAT, ALL_RULES - {rule}).run(plan))
            self.assertNotEqual(full, off, f"rule {rule} had no effect")
        self.assertIn("Cross", render_tree(Rewriter(self.CAT, ALL_RULES - {3}).run(plan)))

    def test_selection_is_never_pushed_past_what_it_needs(self):
        plan = logical("SELECT did, COUNT(*) AS n FROM Emp GROUP BY did HAVING COUNT(*) > 2", self.CAT)
        self.assertIn("Select(cond=Gt(Attr(n)", render_tree(Rewriter(self.CAT).run(plan)))      # stays above the group_by
        plan = logical("SELECT t.did FROM (SELECT did, COUNT(*) AS n FROM Emp GROUP BY did) AS t WHERE t.did = 3", self.CAT)
        out = render_tree(Rewriter(self.CAT).run(plan))
        self.assertLess(out.index("GroupBy"), out.index("Select(cond=Eq(Attr(Emp.did)"))       # pushed below group_by on a key


class JoinOrderSearch(unittest.TestCase):
    """Spec section 5.1 worked example. Output sizes of the joins are given, so they are injected."""

    def setUp(self):
        self.cat = CatalogView.from_dict({"A": "x:INT", "B": "x:INT,y:INT", "C": "y:INT"})
        stats = StubStats(self.cat, {"A": ts("A", 5000, 100, {"x": (2500, 0, 1)}), "B": ts("B", 500, 10, {"x": (500, 0, 1), "y": (500, 0, 1)}),
                                     "C": ts("C", 20000, 400, {"y": (5000, 0, 1)})})
        self.planner = Planner(self.cat, stats, 32)
        self.planner.override = {0b011: (1000, 20), 0b110: (2000, 40), 0b111: (2000, 40)}
        self.plan = logical("SELECT a.x FROM A a, B b, C c WHERE a.x = b.x AND b.y = c.y", self.cat)

    def test_costs_and_winner(self):
        p, _, _ = self.planner.optimize(self.plan)
        rows = {(r["subset"], r["outer"], r["inner"], r["algo"]): r["cost"] for r in self.planner.dp_tables[0]["rows"]}
        self.assertEqual(rows[("{a,b}", "{b}", "a", "nested_loop_join")], 110)          # 10 + ceil(10/30) * 100
        self.assertEqual(rows[("{b,c}", "{b}", "c", "nested_loop_join")], 410)          # 10 + 1 * 400
        self.assertEqual(rows[("{a,b,c}", "{a,b}", "c", "nested_loop_join")], 530)      # 110 + 20 + 1 * 400
        self.assertEqual(rows[("{a,b,c}", "{b,c}", "a", "nested_loop_join")], 650)      # 410 + 40 + 2 * 100 (the spec text says 550, which uses 1 block for 40 pages)
        self.assertEqual(p.cum, 530)
        chosen = [r for r in self.planner.dp_tables[0]["rows"] if r["chosen"] and r["subset"] == "{a,b,c}"]
        self.assertEqual((chosen[0]["outer"], chosen[0]["inner"]), ("{a,b}", "c"))

    def test_plan_is_left_deep_and_every_node_is_annotated(self):
        p, _, _ = self.planner.optimize(self.plan)

        def walk(d):
            self.assertIn("est_rows", d)
            self.assertIn("est_cost", d)
            self.assertGreaterEqual(d["est_rows"], 1)
            for k in ("child", "left", "right"):
                if k in d:
                    walk(d[k])
        walk(p.d)
        top = p.d["child"] if p.d["op"] == "project" else p.d
        self.assertIn(top["right"]["op"], ("project", "seqscan"))                         # the inner is a base relation
        self.assertNotIn(top["left"]["op"], ("seqscan",))                                  # the outer is the intermediate join


class AlgorithmDependsOnBudget(unittest.TestCase):
    """Module 4 checklist: changing B changes which join algorithm is chosen."""

    def test_small_budget_prefers_hash_large_budget_prefers_nested_loop(self):
        cat = CatalogView.from_dict({"R": "a:INT", "S": "a:INT"})
        stats = StubStats(cat, {"R": ts("R", 20000, 200, {"a": (5000, 0, 1)}), "S": ts("S", 5000, 50, {"a": (5000, 0, 1)})})
        plan = logical("SELECT r.a FROM R r, S s WHERE r.a = s.a", cat)

        def join_op(B):
            p, _, _ = Planner(cat, stats, B).optimize(plan)
            d = p.d
            while d["op"] not in ("hash_join", "sort_merge_join", "nested_loop_join"):
                d = d["child"]
            return d["op"]
        self.assertEqual(join_op(4), "hash_join")             # block nested loop would need ceil(50/2) = 25 passes over R
        self.assertEqual(join_op(300), "nested_loop_join")    # S fits in one block: 50 + 200 beats 3 (50 + 200)


class WholePipeline(unittest.TestCase):
    """SQL -> logical -> optimizer -> executor must equal the reference evaluator, at several budgets."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.path = os.path.join(cls.tmp.name, "db")
        cls.prov = InMemoryProvider()
        rnd = random.Random(21)
        T = {"A": "id:INT,x:INT,y:INT,pad:VARCHAR(120)", "B": "id:INT,x:INT,z:INT,pad:VARCHAR(120)",
             "C": "id:INT,y:INT,w:INT,pad:VARCHAR(120)", "D": "id:INT,z:INT,w:INT,pad:VARCHAR(120)"}
        sizes = {"A": 70, "B": 50, "C": 35, "D": 25}
        with Database(cls.path, 16, create=True) as db:
            for t, sch in T.items():
                cols = parse_schema(sch)
                db.create_table(t, cols)
                h, _ = db.open_heap(t)
                rows = [tuple(i if c.name == "id" else (rnd.randrange(12) if c.type == "INT" else "p" * 100) for c in cols)
                        for i in range(sizes[t])]
                for r in rows:
                    h.insert(r)
                db.refresh_counts(t, len(rows))
                cls.prov.add(t, Schema(cols), rows)
            for t in T:
                analyze_table(db, t, 6, 8)
            cls.cat = CatalogView.from_database(db)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    QUERIES = [
        "SELECT b.x, c.w FROM B b, C c, A a WHERE b.id = c.id AND a.x = b.x AND a.y > 3",
        "SELECT c.w FROM D d JOIN C c ON d.w = c.w JOIN B b ON d.z = b.z JOIN A a ON c.y = a.y "
        "WHERE b.x > 10 AND (c.y >= 7 OR b.x > 0)",                          # regression: b.x > 10 used to be dropped
        "SELECT a.x, COUNT(*) AS n FROM A a, B b WHERE a.x = b.x GROUP BY a.x HAVING COUNT(*) > 2",
        "SELECT DISTINCT a.x, b.z FROM A a, B b WHERE a.x = b.x AND a.id > 10 ORDER BY a.x, b.z",
        "SELECT x FROM A WHERE y > 4 UNION SELECT x FROM B WHERE z < 6",
        "SELECT x FROM A INTERSECT SELECT x FROM B",
        "SELECT t.x, t.n, b.z FROM (SELECT x, COUNT(*) AS n FROM A GROUP BY x) AS t JOIN B b ON t.x = b.x WHERE t.n > 1",
        "SELECT a.id FROM A a, B b WHERE a.id < b.id AND a.x = 3",
        "SELECT e.id FROM A e, A m WHERE e.x = m.y AND m.id < 20",
        # regressions: the join order search may put a different relation first, and the plan's output
        # columns must still come out in the written order (a swapped build side did the same)
        "SELECT d.z, d.w, b.z FROM D d JOIN B b ON d.z = b.z WHERE d.w = 2",
        "SELECT c.id, c.w, b.id FROM C c, B b WHERE b.id = c.id AND b.x >= 2 AND c.y > 3",
        "SELECT a.id, a.y, c.y FROM A a JOIN C c ON a.y = c.y",
        "SELECT t.x FROM (SELECT x, COUNT(*) AS n FROM A GROUP BY x) AS t WHERE t.x = 3",
    ]

    def check(self, sql, frames=(4, 8, 32)):
        plan = logical(sql, self.cat)
        ref = sorted(evaluate(plan, self.prov).rows)
        for B in frames:
            with Database(self.path, 16) as db:
                phys, info = optimize(db, plan, B)
            res = execute(self.path, phys, B)
            self.assertEqual(sorted(res["rows"]), ref, f"{sql} at B={B}")
            self.assertLessEqual(res["peak_frames"], B)

    def test_queries_give_the_reference_answer_at_every_budget(self):
        for q in self.QUERIES:
            self.check(q)

    def test_comma_and_join_on_give_the_same_physical_plan(self):
        a = "SELECT e.id FROM A e, B b, C c WHERE e.x = b.x AND b.id = c.id AND e.y > 2"
        b = "SELECT e.id FROM A e JOIN B b ON e.x = b.x JOIN C c ON b.id = c.id WHERE e.y > 2"
        with Database(self.path, 16) as db:
            pa, _ = optimize(db, logical(a, self.cat), 16)
            pb, _ = optimize(db, logical(b, self.cat), 16)
        self.assertEqual(json.dumps(pa, sort_keys=True), json.dumps(pb, sort_keys=True))

    def test_rewriting_never_changes_the_answer_and_never_costs_more_here(self):
        sql = "SELECT b.x, c.w FROM B b, C c, A a WHERE b.id = c.id AND a.x = b.x AND a.y > 3 AND b.z < 8"
        plan = logical(sql, self.cat)
        ref = sorted(evaluate(plan, self.prov).rows)
        costs = {}
        for off in (set(), {2}, {3}, {6}, {1, 2, 3, 4, 5, 6, 7}):
            with Database(self.path, 16) as db:
                phys, info = optimize(db, plan, 8, ALL_RULES - off)
            res = execute(self.path, phys, 8)
            self.assertEqual(sorted(res["rows"]), ref, f"rules off: {off}")
            costs[tuple(sorted(off))] = info["total_cost"]
        self.assertLessEqual(costs[()], costs[(1, 2, 3, 4, 5, 6, 7)])

    def test_estimates_are_close_on_simple_queries(self):
        plan = logical("SELECT a.id FROM A a WHERE a.x = 3", self.cat)
        with Database(self.path, 16) as db:
            phys, _ = optimize(db, plan, 8)
        res = execute(self.path, phys, 8)
        for n in est_vs_actual(phys, res):
            self.assertLess(n["q_error"], 3.0, n)

    def test_unknown_statistics_fall_back_to_documented_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "db")
            with Database(p, 8, create=True) as db:
                db.create_table("T", parse_schema("a:INT"))
                h, _ = db.open_heap("T")
                for i in range(100):
                    h.insert((i,))
                db.refresh_counts("T", 100)
                sp = StatsProvider(db)
                self.assertEqual(sp.table("T").source, "default")
                self.assertEqual(sp.table("T").n_tuples, 100)
                self.assertEqual(sp.table("T").cols["a"].ndv, 10)
                analyze_table(db, "T", 4, 8)
                self.assertEqual(StatsProvider(db).table("T").source, "analyzed")


class Analyze(unittest.TestCase):
    def test_exact_counts_histogram_and_idempotence(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "db")
            rnd = random.Random(2)
            vals = [rnd.choice([1, 1, 1, 1, 2, 3, 4, 5, 6, 7, 8, 9]) for _ in range(900)]
            with Database(p, 8, create=True) as db:
                db.create_table("T", parse_schema("a:INT,s:VARCHAR(5)"))
                h, _ = db.open_heap("T")
                for v in vals:
                    h.insert((v, "k" + str(v % 4)))
                db.refresh_counts("T", len(vals))
                t1 = analyze_table(db, "T", 4, 6)
                c = t1.cols["a"]
                self.assertEqual((t1.n_tuples, c.ndv, c.min, c.max), (900, 9, 1, 9))
                self.assertLessEqual(len(c.buckets), 4)
                self.assertEqual(sum(b.count for b in c.buckets), 900)
                self.assertEqual(sum(b.ndv for b in c.buckets), 9)
                for a, b in zip(c.buckets, c.buckets[1:]):
                    self.assertLess(a.hi, b.lo)                                    # no value straddles two buckets
                self.assertEqual(t1.cols["s"].ndv, 4)
            with Database(p, 8) as db:
                sig = lambda t: [(c.name, c.ndv, c.min, c.max, [(b.lo, b.hi, b.count, b.ndv) for b in c.buckets]) for c in t.cols.values()]
                stored = db.catalog.get_stats("T")
                self.assertEqual(sig(stored), sig(t1))                             # survives a restart
                again = analyze_table(db, "T", 4, 6)
                self.assertEqual(sig(again), sig(t1))                              # running it twice changes nothing

    def test_second_analyze_leaves_catalog_file_byte_identical(self):
        """Regression: ANALYZE used to delete+insert its records, and dead slots are never reclaimed, so
        catalog.db grew on every run. The spec says running ANALYZE twice changes nothing."""
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "db")
            with Database(p, 8, create=True) as db:
                db.create_table("T", parse_schema("a:INT,s:VARCHAR(5)"))
                h, _ = db.open_heap("T")
                for v in range(700):
                    h.insert((v % 37, "k" + str(v % 5)))
                db.refresh_counts("T", 700)
                analyze_table(db, "T", 10, 6)
            snap = []
            for _ in range(3):
                with Database(p, 8) as db:
                    analyze_table(db, "T", 10, 6)
                with open(os.path.join(p, "catalog.db"), "rb") as f:
                    snap.append(f.read())
            self.assertEqual(snap[0], snap[1])
            self.assertEqual(snap[1], snap[2])

    def test_loading_more_rows_does_not_grow_the_catalog_without_bound(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "db")
            with Database(p, 8, create=True) as db:
                db.create_table("T", parse_schema("a:INT"))
                h, _ = db.open_heap("T")
                for i in range(30):
                    h.insert((i,))
                    db.refresh_counts("T", 1)
            self.assertLessEqual(os.path.getsize(os.path.join(p, "catalog.db")), 3 * 4096)

    def test_empty_table(self):
        with tempfile.TemporaryDirectory() as d:
            with Database(os.path.join(d, "db"), 8, create=True) as db:
                db.create_table("T", parse_schema("a:INT"))
                t = analyze_table(db, "T", 4, 6)
                self.assertEqual((t.n_tuples, t.cols["a"].ndv, t.cols["a"].buckets), (0, 0, []))


class CommandLine(unittest.TestCase):
    def test_analyze_plan_explain_dp_table(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "db")
            with Database(p, 8, create=True) as db:
                for t, s in (("A", "x:INT,pad:VARCHAR(100)"), ("B", "x:INT,pad:VARCHAR(100)")):
                    db.create_table(t, parse_schema(s))
                    h, _ = db.open_heap(t)
                    for i in range(60):
                        h.insert((i % 10, "p" * 80))
                    db.refresh_counts(t, 60)
                cat = CatalogView.from_database(db)
            lf = os.path.join(d, "l.json")
            with open(lf, "w") as f:
                json.dump(to_dict(logical("SELECT a.x FROM A a, B b WHERE a.x = b.x", cat)), f)

            def call(*a):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = cli.main(list(a))
                return code, out.getvalue(), err.getvalue()

            code, out, _ = call("analyze", "--catalog", p, "--table", "A", "--buckets", "5")
            self.assertEqual(code, 0)
            self.assertIn("analyzed A", out)
            code, out, _ = call("plan", "--catalog", p, "--logical", lf, "--frames", "16", "--json")
            plan = json.loads(out)
            self.assertIn("est_rows", plan)
            self.assertEqual(execute(p, plan, 16)["totals"]["tuples_out"], 360)         # 6 x 6 rows per key, 10 keys
            code, out, _ = call("explain", "--catalog", p, "--logical", lf, "--frames", "16", "--trees")
            self.assertIn("est_rows=", out)
            self.assertIn("after rewriting", out)
            code, out, _ = call("dp-table", "--catalog", p, "--logical", lf, "--frames", "16")
            self.assertIn("<== best", out)
            code, out, _ = call("plan", "--catalog", p, "--logical", lf, "--frames", "16", "--disable", "2,3", "--json")
            self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
