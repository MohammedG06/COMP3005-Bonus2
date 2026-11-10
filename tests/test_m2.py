"""Module 2 tests: differential tests against the reference evaluator + the spec's section 6 checklist."""
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
from dbms.execution import cli
from dbms.execution.context import ExecContext
from dbms.execution.planner import build_operator
from dbms.execution.runner import execute
from dbms.ra.evaluate import evaluate
from dbms.ra.plan_json import from_dict
from dbms.ra.tables import InMemoryProvider
from dbms.schema import Schema
from dbms.storage.database import Database
from dbms.storage.records import parse_schema

PAD = "p" * 150
PAD200 = "p" * 200


def bag(rows):
    return sorted(tuple(round(v, 6) if isinstance(v, float) else v for v in r) for r in rows)


class Env:
    """A temp database + an in-memory copy of every table for the reference evaluator."""

    def __init__(self, tables):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "db")
        self.prov = InMemoryProvider()
        with Database(self.path, 16, create=True) as db:
            for name, (schema, rows) in tables.items():
                cols = parse_schema(schema)
                db.create_table(name, cols)
                heap, _ = db.open_heap(name)
                for r in rows:
                    heap.insert(r)
                db.refresh_counts(name, len(rows))
                self.prov.add(name, Schema(cols), list(rows))

    def run(self, plan, B=8):
        return execute(self.path, plan, B)

    def ref(self, plan):
        return list(evaluate(from_dict(plan, "physical"), self.prov).rows)

    def pages(self, name):
        with Database(self.path, 8) as db:
            return db.catalog.get_table(name).num_pages

    def cleanup(self):
        self.tmp.cleanup()


def scan(t):
    return {"op": "seqscan", "table": t}


def join(kind, cond, l, r):
    return {"op": kind, "condition": cond, "left": l, "right": r}


JOINS = ("nested_loop_join", "sort_merge_join", "hash_join")


def wide_tables(nr=120, ns=90, keys=15, seed=3):
    rnd = random.Random(seed)
    return {
        "R": ("a:INT,b:INT,pad:VARCHAR(200)", [(i, rnd.randrange(keys), PAD) for i in range(nr)]),
        "S": ("b:INT,c:VARCHAR(20),pad:VARCHAR(200)", [(rnd.randrange(keys), f"c{i}", PAD) for i in range(ns)]),
        "T": ("c:VARCHAR(20),d:INT", [(f"c{i}", i % 7) for i in range(0, ns, 2)]),
    }


class JoinTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = Env(wide_tables())

    @classmethod
    def tearDownClass(cls):
        cls.env.cleanup()

    def test_all_three_joins_return_the_same_bag_at_every_budget(self):
        want = bag(self.env.ref(join("hash_join", "R.b = S.b", scan("R"), scan("S"))))
        self.assertGreater(len(want), 120)                      # output larger than both inputs
        for kind in JOINS:
            for B in (4, 5, 8, 16, 64):
                res = self.env.run(join(kind, "R.b = S.b", scan("R"), scan("S")), B)
                self.assertEqual(bag(res["rows"]), want, f"{kind} B={B}")
                self.assertLessEqual(res["peak_frames"], B, f"{kind} B={B}")

    def test_nested_loop_comparisons_are_exactly_r_times_s(self):
        for B in (4, 8, 64):
            res = self.env.run(join("nested_loop_join", "R.b = S.b", scan("R"), scan("S")), B)
            self.assertEqual(res["totals"]["comparisons"], 120 * 90)

    def test_nested_loop_page_reads_follow_the_formula(self):
        M, N = self.env.pages("R"), self.env.pages("S")
        for B in (4, 5, 8, 16):
            res = self.env.run(join("nested_loop_join", "R.b = S.b", scan("R"), scan("S")), B)
            blocks = math.ceil(M / (B - 2))
            # formula M + ceil(M/(B-2)) * N, plus one directory page per scan (1 outer + `blocks` inner scans)
            self.assertEqual(res["totals"]["page_reads"], M + blocks * N + 1 + blocks, f"B={B}")
            self.assertEqual(res["totals"]["page_writes"], 0)

    def test_hash_join_cost_is_3_times_m_plus_n_when_partitions_fit(self):
        M, N = self.env.pages("R"), self.env.pages("S")
        res = self.env.run(join("hash_join", "R.b = S.b", scan("R"), scan("S")), 64)
        t = res["totals"]
        io_pages = t["page_reads"] + t["page_writes"]
        # partition phase writes about M+N, probe reads about M+N, scans read M+N (+2 directory pages);
        # a partial page per partition makes the partition files slightly larger than M+N
        self.assertGreaterEqual(io_pages, 3 * (M + N))
        self.assertLessEqual(io_pages, 3 * (M + N) + 2 + 4 * 63)

    def test_duplicate_keys_on_both_sides_give_four_rows(self):
        env = Env({"R": ("b:INT", [(1,), (2,), (2,), (3,)]), "S": ("b:INT", [(2,), (2,), (4,)])})
        try:
            for kind in JOINS:
                res = env.run(join(kind, "R.b = S.b", scan("R"), scan("S")), 4)
                self.assertEqual(bag(res["rows"]), [(2, 2)] * 4, kind)
        finally:
            env.cleanup()

    def test_equal_key_run_larger_than_one_frame_spills(self):
        rows_r = [(1, i, PAD) for i in range(60)]                 # 60 wide tuples with one key: > 1 page
        rows_s = [(1, "x" + str(i), PAD) for i in range(5)]
        env = Env({"R": ("b:INT,a:INT,pad:VARCHAR(200)", rows_r), "S": ("b:INT,c:VARCHAR(20),pad:VARCHAR(200)", rows_s)})
        try:
            res = env.run(join("sort_merge_join", "S.b = R.b", scan("S"), scan("R")), 4)
            self.assertEqual(len(res["rows"]), 300)
            self.assertTrue(any("spilled" in n for node in res["nodes"] for n in node["notes"]))
        finally:
            env.cleanup()

    def test_three_way_join_at_b4_uses_materialization_and_is_correct(self):
        for outer in JOINS:
            for inner in JOINS:
                plan = join(outer, "S.c = T.c", join(inner, "R.b = S.b", scan("R"), scan("S")), scan("T"))
                res = self.env.run(plan, 4)
                self.assertEqual(bag(res["rows"]), bag(self.env.ref(plan)), f"{outer}/{inner}")
                self.assertLessEqual(res["peak_frames"], 4)

    def test_non_equality_condition_only_for_nested_loop(self):
        plan = join("nested_loop_join", "R.a < S.b", scan("R"), scan("S"))
        self.assertEqual(bag(self.env.run(plan, 4)["rows"]), bag(self.env.ref(plan)))
        with self.assertRaises(DBError) as cm:
            self.env.run(join("hash_join", "R.a < S.b", scan("R"), scan("S")), 8)
        self.assertEqual(cm.exception.category, "plan")
        with self.assertRaises(DBError):
            self.env.run(join("sort_merge_join", "R.a < S.b", scan("R"), scan("S")), 8)

    def test_join_through_filter_and_project_pipeline(self):
        l = {"op": "filter", "condition": "R.a > 30 and R.b <> 3", "child": scan("R")}
        r = {"op": "project", "columns": ["S.b", "S.c"], "child": scan("S")}
        for kind in JOINS:
            plan = join(kind, "R.b = S.b", l, r)
            self.assertEqual(bag(self.env.run(plan, 4)["rows"]), bag(self.env.ref(plan)), kind)

    def test_self_join_with_aliases(self):
        a = {"op": "seqscan", "table": "T", "alias": "t1"}
        b = {"op": "seqscan", "table": "T", "alias": "t2"}
        for kind in JOINS:
            res = self.env.run(join(kind, "t1.d = t2.d", a, b), 4)
            ref = [x + y for x in self.env.prov.table_rows("T") for y in self.env.prov.table_rows("T") if x[1] == y[1]]
            self.assertEqual(bag(res["rows"]), bag(ref), kind)
        with self.assertRaises(DBError) as cm:                  # without an alias the names collide
            self.env.run(join("hash_join", "T.d = T.d", scan("T"), scan("T")), 8)
        self.assertEqual(cm.exception.category, "schema")

    def test_same_plan_same_counters_every_time(self):
        for kind in JOINS:
            runs = [self.env.run(join(kind, "S.c = T.c", scan("S"), scan("T")), 4)["totals"] for _ in range(3)]
            self.assertEqual(runs[0], runs[1])
            self.assertEqual(runs[1], runs[2])


class SortTests(unittest.TestCase):
    def test_sort_of_20_times_b_pages_at_b4(self):
        rows = [(i, (i * 7919) % 1009, PAD) for i in range(2000)]            # 24 rows per page: about 84 pages
        env = Env({"R": ("a:INT,k:INT,pad:VARCHAR(200)", rows)})
        try:
            plan = {"op": "sort", "keys": [["R.k", "asc"], ["R.a", "desc"]], "child": scan("R")}
            self.assertGreaterEqual(env.pages("R"), 80)                      # 20 x B
            res = env.run(plan, 4)
            got = res["rows"]
            self.assertEqual(got, sorted(rows, key=lambda r: (r[1], -r[0])))
            self.assertLessEqual(res["peak_frames"], 4)
            self.assertGreater(res["totals"]["page_writes"], 0)             # it really went to disk
        finally:
            env.cleanup()

    def test_worked_example_variant_n8_b3(self):
        rows = [(i, (i * 37) % 152, PAD200) for i in range(152)]            # exactly 19 per page = 8 pages
        env = Env({"R": ("a:INT,k:INT,pad:VARCHAR(200)", rows)})
        try:
            self.assertEqual(env.pages("R"), 8)
            res = env.run({"op": "sort", "keys": [["R.k", "asc"]], "child": scan("R")}, 3)
            s = res["nodes"][0]
            self.assertEqual(s["info"]["initial_runs"], 4)                  # run = B-1 = 2 pages -> 4 runs
            self.assertEqual(s["info"]["passes"], 3)                        # pass 0, one merge pass, final stream
            self.assertEqual((s["own_reads"], s["own_writes"]), (16, 16))   # 8+8 on pass 1, 8 on the final pass
            self.assertEqual(res["nodes"][1]["own_reads"], 9)               # the scan: 8 pages + 1 directory page
        finally:
            env.cleanup()

    def test_input_that_fits_never_touches_the_disk(self):
        env = Env({"R": ("a:INT", [(5,), (3,), (9,), (1,)])})
        try:
            res = env.run({"op": "sort", "keys": [["R.a", "asc"]], "child": scan("R")}, 4)
            self.assertEqual(res["rows"], [(1,), (3,), (5,), (9,)])
            self.assertEqual(res["totals"]["page_writes"], 0)
            self.assertEqual(res["nodes"][0]["info"]["passes"], 1)
        finally:
            env.cleanup()

    def test_varchar_desc_and_stability(self):
        rows = [("b", 1), ("a", 2), ("b", 3), ("c", 4), ("a", 5)]
        env = Env({"R": ("s:VARCHAR(5),n:INT", rows)})
        try:
            res = env.run({"op": "sort", "keys": [["R.s", "desc"]], "child": scan("R")}, 4)
            self.assertEqual(res["rows"], [("c", 4), ("b", 1), ("b", 3), ("a", 2), ("a", 5)])
        finally:
            env.cleanup()

    def test_sorted_passes_for_several_n_and_b(self):
        for n_rows, B in [(40, 4), (300, 4), (300, 6), (600, 8), (900, 5)]:
            rows = [(i, (i * 31) % 997, PAD) for i in range(n_rows)]
            env = Env({"R": ("a:INT,k:INT,pad:VARCHAR(200)", rows)})
            try:
                res = env.run({"op": "sort", "keys": [["R.k", "asc"]], "child": scan("R")}, B)
                self.assertEqual([r[1] for r in res["rows"]], sorted(r[1] for r in rows))
                N = env.pages("R")
                R0 = math.ceil(N / (B - 1))
                expect = 1 if R0 == 1 else 1 + math.ceil(math.log(R0) / math.log(B - 1) - 1e-9)
                self.assertEqual(res["nodes"][0]["info"]["passes"], expect, (n_rows, B))
            finally:
                env.cleanup()


class AggregateAndSetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rnd = random.Random(5)
        cls.rows = [(i, rnd.randrange(400), f"n{rnd.randrange(30)}", rnd.randrange(1000), PAD) for i in range(1500)]
        cls.env = Env({
            "E": ("id:INT,dept:INT,name:VARCHAR(10),sal:INT,pad:VARCHAR(200)", cls.rows),
            "A": ("x:INT,y:VARCHAR(5)", [(i % 6, "a") for i in range(40)]),
            "B": ("x:INT,y:VARCHAR(5)", [(i % 4 + 3, "a") for i in range(30)]),
        })

    @classmethod
    def tearDownClass(cls):
        cls.env.cleanup()

    def gb(self, mode, keys=("E.dept",)):
        return {"op": "group_by", "mode": mode, "keys": list(keys),
                "aggregates": [["count", "*"], ["sum", "E.sal"], ["avg", "E.sal"], ["min", "E.name"], ["max", "E.id"]],
                "child": scan("E")}

    def test_group_by_modes_agree_with_reference_even_when_spilling(self):
        want = bag(self.env.ref(self.gb("sort")))
        for mode in ("sort", "hash"):
            for B in (4, 5, 8, 64):
                res = self.env.run(self.gb(mode), B)
                self.assertEqual(bag(res["rows"]), want, f"{mode} B={B}")
                self.assertLessEqual(res["peak_frames"], B)
        spilled = self.env.run(self.gb("hash"), 4)["nodes"][0]["info"]
        self.assertTrue(spilled["spilled"])                                 # 400 groups cannot fit in 2 pages

    def test_group_by_output_schema_names(self):
        res = self.env.run(self.gb("hash"), 8)
        self.assertEqual([c[0] for c in res["schema"]],
                         ["E.dept", "count_star", "sum_E_sal", "avg_E_sal", "min_E_name", "max_E_id"])

    def test_group_by_without_keys(self):
        plan = {"op": "group_by", "mode": "hash", "keys": [], "aggregates": [["count", "*"], ["max", "E.sal"]], "child": scan("E")}
        self.assertEqual(self.env.run(plan, 4)["rows"], [(1500, max(r[3] for r in self.rows))])

    def test_distinct_modes_agree(self):
        proj = {"op": "project", "columns": ["E.dept", "E.name"], "child": scan("E")}
        want = bag(self.env.ref({"op": "distinct", "mode": "sort", "child": proj}))
        for mode in ("sort", "hash"):
            for B in (4, 6, 64):
                res = self.env.run({"op": "distinct", "mode": mode, "child": proj}, B)
                self.assertEqual(bag(res["rows"]), want, f"{mode} B={B}")

    def test_project_keeps_duplicates(self):
        res = self.env.run({"op": "project", "columns": ["E.name"], "child": scan("E")}, 4)
        self.assertEqual(len(res["rows"]), 1500)

    def test_set_operators(self):
        A, Bt = scan("A"), scan("B")
        for op in ("union", "union_all", "intersect", "except"):
            plan = {"op": op, "left": A, "right": Bt}
            for B in (4, 8):
                self.assertEqual(bag(self.env.run(plan, B)["rows"]), bag(self.env.ref(plan)), f"{op} B={B}")
        self.assertEqual(len(self.env.run({"op": "union_all", "left": A, "right": Bt}, 4)["rows"]), 70)

    def test_set_operators_when_inputs_exceed_memory(self):
        E1 = {"op": "project", "columns": ["E.dept", "E.name"], "child": scan("E")}
        E2 = {"op": "project", "columns": ["E.dept", "E.name"], "child": {"op": "filter", "condition": "E.id < 700", "child": scan("E")}}
        for op in ("union", "intersect", "except"):
            plan = {"op": op, "left": E1, "right": E2}
            self.assertEqual(bag(self.env.run(plan, 4)["rows"]), bag(self.env.ref(plan)), op)

    def test_set_operator_type_mismatch_is_an_error(self):
        with self.assertRaises(DBError) as cm:
            self.env.run({"op": "union", "left": scan("A"), "right": {"op": "project", "columns": ["E.name", "E.id"], "child": scan("E")}}, 8)
        self.assertEqual(cm.exception.category, "schema")

    def test_limit_really_stops_pulling(self):
        res = self.env.run({"op": "limit", "n": 3, "child": scan("E")}, 4)
        self.assertEqual(len(res["rows"]), 3)
        self.assertEqual(res["nodes"][1]["tuples_out"], 3)                  # the scan produced 3, not 4
        self.assertEqual(res["nodes"][1]["own_reads"], 2)                   # one data page + the directory page
        both = self.env.run({"op": "limit", "n": 5, "child": {"op": "sort", "keys": [["E.sal", "desc"]], "child": scan("E")}}, 4)
        self.assertEqual([r[3] for r in both["rows"]], sorted((r[3] for r in self.rows), reverse=True)[:5])

    def test_sort_over_join_over_group_by_pipeline(self):
        g = {"op": "group_by", "mode": "hash", "keys": ["E.dept"], "aggregates": [["count", "*", "n"]], "child": scan("E")}
        plan = {"op": "sort", "keys": [["n", "desc"], ["E.dept", "asc"]], "child": g}
        res = self.env.run(plan, 4)
        want = sorted(self.env.ref(g), key=lambda r: (-r[1], r[0]))
        self.assertEqual(res["rows"], want)


class EmptyAndEdgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = Env({"E": ("a:INT,b:VARCHAR(5)", []), "F": ("a:INT,b:VARCHAR(5)", [(1, "x"), (2, "y"), (2, "y")])})

    @classmethod
    def tearDownClass(cls):
        cls.env.cleanup()

    def test_every_operator_on_an_empty_input(self):
        E, F = scan("E"), scan("F")
        plans = [
            E, {"op": "filter", "condition": "E.a > 0", "child": E},
            {"op": "project", "columns": ["E.b"], "child": E},
            {"op": "sort", "keys": [["E.a", "asc"]], "child": E},
            {"op": "distinct", "mode": "sort", "child": E}, {"op": "distinct", "mode": "hash", "child": E},
            {"op": "group_by", "mode": "sort", "keys": ["E.a"], "aggregates": [["count", "*"]], "child": E},
            {"op": "group_by", "mode": "hash", "keys": ["E.a"], "aggregates": [["count", "*"]], "child": E},
            {"op": "limit", "n": 2, "child": E},
        ] + [{"op": op, "left": E, "right": F} for op in ("union", "union_all", "intersect", "except")] + \
            [{"op": op, "left": F, "right": E} for op in ("union", "union_all", "intersect", "except")] + \
            [join(k, "E.a = F.a", E, F) for k in JOINS] + [join(k, "F.a = E.a", F, E) for k in JOINS] + \
            [join(k, "E.a = E2.a", E, {"op": "seqscan", "table": "E", "alias": "E2"}) for k in JOINS]
        for plan in plans:
            res = self.env.run(plan, 4)
            self.assertEqual(bag(res["rows"]), bag(self.env.ref(plan)), plan["op"])

    def test_group_by_on_empty_input(self):
        for mode in ("sort", "hash"):
            only_count = {"op": "group_by", "mode": mode, "keys": [], "aggregates": [["count", "*"]], "child": scan("E")}
            self.assertEqual(self.env.run(only_count, 4)["rows"], [(0,)])
            with self.assertRaises(DBError) as cm:
                self.env.run({"op": "group_by", "mode": mode, "keys": [], "aggregates": [["max", "E.a"]], "child": scan("E")}, 4)
            self.assertEqual(cm.exception.category, "aggregate")
            keyed = {"op": "group_by", "mode": mode, "keys": ["E.a"], "aggregates": [["count", "*"]], "child": scan("E")}
            self.assertEqual(self.env.run(keyed, 4)["rows"], [])

    def test_nothing_left_behind_after_any_run(self):
        self.env.run(join("hash_join", "F.a = F2.a", scan("F"), {"op": "seqscan", "table": "F", "alias": "F2"}), 4)
        self.assertFalse(os.path.exists(os.path.join(self.env.path, "tmp")))      # temp dir removed
        with Database(self.env.path, 4) as db:
            self.assertEqual((db.pool.n_pinned, len(db.pool.leased)), (0, 0))


class SkewTests(unittest.TestCase):
    def test_forty_percent_hot_key_on_both_sides(self):
        rnd = random.Random(11)
        def side(n):
            return [(7 if rnd.random() < 0.4 else rnd.randrange(100, 400), i, PAD) for i in range(n)]
        env = Env({"R": ("k:INT,i:INT,pad:VARCHAR(200)", side(400)), "S": ("k:INT,j:INT,pad:VARCHAR(200)", side(200))})
        try:
            plan = join("hash_join", "R.k = S.k", scan("R"), scan("S"))
            res = env.run(plan, 4)
            self.assertEqual(bag(res["rows"]), bag(env.ref(plan)))
            overflow = res["nodes"][0]["info"]["overflow"]
            self.assertTrue(any(o["action"] == "block nested loop fallback" for o in overflow), overflow)
            self.assertLessEqual(res["peak_frames"], 4)
        finally:
            env.cleanup()


class PlanValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = Env({"R": ("a:INT,b:VARCHAR(5)", [(1, "x")])})

    @classmethod
    def tearDownClass(cls):
        cls.env.cleanup()

    def err(self, plan, text=None):
        with self.assertRaises(DBError) as cm:
            execute(self.env.path, text if text is not None else plan, 8)
        return cm.exception

    def test_messages_name_the_node_and_problem(self):
        e = self.err({"op": "sort", "child": scan("R")})
        self.assertEqual(e.message[:48], "Node 'sort': missing required field 'keys'. [at ")
        e = self.err({"op": "filter", "condition": 5, "child": scan("R")})
        self.assertIn("Node 'filter': field 'condition' must be a string.", e.message)
        e = self.err({"op": "merge_join", "left": scan("R"), "right": scan("R")})
        self.assertIn("Node 'merge_join': unknown operator 'merge_join'.", e.message)
        e = self.err(None, text='{"op": "seqscan", "table": ')
        self.assertIn("malformed JSON", e.message)
        e = self.err({"op": "limit", "n": "3", "child": scan("R")})
        self.assertIn("Node 'limit'", e.message)
        e = self.err({"op": "filter", "condition": "R.a >", "child": scan("R")})
        self.assertIn("not a valid condition", e.message)

    def test_unknown_table_column_and_type_errors_before_any_work(self):
        db = Database(self.env.path, 8)
        ctx = ExecContext(db, 8)
        try:
            for plan, cat in [(scan("Nope"), "name"),
                              ({"op": "filter", "condition": "R.zzz = 1", "child": scan("R")}, "name"),
                              ({"op": "filter", "condition": "R.a = 'x'", "child": scan("R")}, "type"),
                              ({"op": "project", "columns": ["R.a", "R.a"], "child": scan("R")}, "schema")]:
                with self.assertRaises(DBError) as cm:
                    build_operator(from_dict(plan, "physical"), ctx)
                self.assertEqual(cm.exception.category, cat, plan)
            self.assertEqual((db.pool.n_pinned, len(db.pool.leased), len(ctx.temps)), (0, 0, 0))
        finally:
            db.close()

    def test_too_few_frames_is_a_clear_error_not_a_crash(self):
        for plan in ({"op": "sort", "keys": [["R.a", "asc"]], "child": scan("R")},
                     join("hash_join", "R.a = R2.a", scan("R"), {"op": "seqscan", "table": "R", "alias": "R2"}),
                     {"op": "group_by", "mode": "hash", "keys": ["R.a"], "aggregates": [], "child": scan("R")}):
            with self.assertRaises(DBError) as cm:
                execute(self.env.path, plan, 2)
            self.assertEqual(cm.exception.category, "plan", plan["op"])
            self.assertIn("frames", cm.exception.message)

    def test_cli_run_and_trace(self):
        plan_path = os.path.join(os.path.dirname(self.env.path), "p.json")
        with open(plan_path, "w") as f:
            json.dump(scan("R"), f)

        def call(*args):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(list(args))
            return code, out.getvalue(), err.getvalue()

        code, out, _ = call("run", "--db", self.env.path, "--plan", plan_path, "--frames", "8", "--json")
        j = json.loads(out)
        self.assertEqual((code, j["schema"], j["rows"]), (0, [["R.a", "INT"], ["R.b", "VARCHAR", 5]], [[1, "x"]]))
        self.assertEqual(set(j["totals"]), {"tuples_out", "comparisons", "page_reads", "page_writes"})
        code, out, _ = call("trace", "--db", self.env.path, "--plan", plan_path, "--frames", "8")
        self.assertIn("seqscan R", out)
        self.assertIn("tuples_out=1", out)
        code, _, err = call("run", "--db", self.env.path, "--plan", plan_path.replace("p.json", "missing.json"), "--frames", "8")
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()
