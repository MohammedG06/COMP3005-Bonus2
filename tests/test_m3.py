"""Module 3 tests: every example of spec section 5 (exact plans) and the 15 required cases."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dbms.errors import DBError, format_error
from dbms.ra.evaluate import evaluate
from dbms.ra.plan_json import to_dict, from_dict
from dbms.ra.tables import InMemoryProvider
from dbms.schema import Schema
from dbms.sql import cli
from dbms.sql.translate import CatalogView, compile_query_text
from dbms.storage.database import Database
from dbms.storage.records import parse_schema

TABLES = {
    "Emp": "eid:INT,name:VARCHAR(20),age:INT,did:INT,mgr:INT,salary:INT,city:VARCHAR(20)",
    "Dept": "did:INT,title:VARCHAR(20),budget:INT,city:VARCHAR(20)",
    "Proj": "pid:INT,did:INT,pname:VARCHAR(20)",
    "Task": "tid:INT,pid:INT,hours:INT",
}
CAT = CatalogView.from_dict(TABLES)


def plan(sql, cat=CAT):
    res = compile_query_text(sql, cat)
    return to_dict(res[0][1])


def err(sql, cat=CAT):
    try:
        compile_query_text(sql, cat)
    except DBError as e:
        return e
    raise AssertionError("expected a DBError")


def scan(t):
    return {"op": "scan", "table": t}


def ren(t, to):
    return {"op": "rename", "to": to, "child": scan(t)}


def ops(d):
    out = [d["op"]]
    for k in ("child", "left", "right"):
        if k in d:
            out += ops(d[k])
    return out


class SpecExamples(unittest.TestCase):
    def test_5_1_filter_and_projection(self):
        self.assertEqual(plan("SELECT name, age FROM Emp WHERE age > 30;"),
                         {"op": "project", "columns": ["Emp.name", "Emp.age"],
                          "child": {"op": "select", "condition": "Emp.age > 30", "child": scan("Emp")}})

    def test_5_2_join_with_aliases(self):
        self.assertEqual(plan("SELECT e.name, d.title FROM Emp e JOIN Dept d ON e.did = d.did WHERE d.budget > 1000;"),
                         {"op": "project", "columns": ["e.name", "d.title"],
                          "child": {"op": "select", "condition": "d.budget > 1000",
                                    "child": {"op": "join", "condition": "e.did = d.did",
                                              "left": ren("Emp", "e"), "right": ren("Dept", "d")}}})

    def test_5_3_comma_join(self):
        p = plan("SELECT * FROM Emp e, Dept d WHERE e.did = d.did;")
        self.assertEqual(ops(p), ["project", "select", "cross", "rename", "scan", "rename", "scan"])
        self.assertEqual(p["child"]["condition"], "e.did = d.did")
        self.assertEqual(p["columns"][:3], ["e.eid", "e.name", "e.age"])
        self.assertEqual(p["columns"][-4:], ["d.did", "d.title", "d.budget", "d.city"])

    def test_5_4_grouping_with_having(self):
        p = plan("SELECT did, COUNT(*) AS n, AVG(salary) AS avg_sal FROM Emp WHERE age > 25 "
                 "GROUP BY did HAVING COUNT(*) > 5 ORDER BY n DESC LIMIT 10;")
        self.assertEqual(p, {"op": "limit", "n": 10, "child": {
            "op": "sort", "keys": [["n", "desc"]], "child": {
                "op": "project", "columns": ["Emp.did", "n", "avg_sal"], "child": {
                    "op": "select", "condition": "n > 5", "child": {
                        "op": "group_by", "keys": ["Emp.did"],
                        "aggregates": [["count", "*", "n"], ["avg", "Emp.salary", "avg_sal"]],
                        "child": {"op": "select", "condition": "Emp.age > 25", "child": scan("Emp")}}}}}})

    def test_5_5_self_join(self):
        p = plan("SELECT e.name, m.name FROM Emp e JOIN Emp m ON e.mgr = m.eid;")
        self.assertEqual(p["child"], {"op": "join", "condition": "e.mgr = m.eid",
                                      "left": ren("Emp", "e"), "right": ren("Emp", "m")})
        self.assertEqual(p["columns"], ["e.name", "m.name"])

    def test_5_6_subquery_in_from(self):
        p = plan("SELECT t.did, t.n FROM (SELECT did, COUNT(*) AS n FROM Emp GROUP BY did) AS t WHERE t.n > 3;")
        self.assertEqual(p, {"op": "project", "columns": ["t.did", "t.n"], "child": {
            "op": "select", "condition": "t.n > 3", "child": {
                "op": "rename", "to": "t", "child": {
                    "op": "project", "columns": ["Emp.did", "n"], "child": {
                        "op": "group_by", "keys": ["Emp.did"], "aggregates": [["count", "*", "n"]],
                        "child": scan("Emp")}}}}})

    def test_5_7_the_two_unions(self):
        a = {"op": "project", "columns": ["Emp.city"], "child": scan("Emp")}
        b = {"op": "project", "columns": ["Dept.city"], "child": scan("Dept")}
        self.assertEqual(plan("SELECT city FROM Emp UNION ALL SELECT city FROM Dept;"), {"op": "union_all", "left": a, "right": b})
        self.assertEqual(plan("SELECT city FROM Emp UNION SELECT city FROM Dept;"), {"op": "union", "left": a, "right": b})


class RequiredCases(unittest.TestCase):
    def test_01_self_join_two_renames(self):
        p = plan("SELECT e.name FROM Emp e JOIN Emp m ON e.mgr = m.eid")
        self.assertEqual(ops(p).count("rename"), 2)
        self.assertEqual(ops(p).count("scan"), 2)

    def test_02_star_over_three_tables_with_shared_column_names(self):
        p = plan("SELECT * FROM Emp e, Dept d, Proj p WHERE e.did = d.did AND p.did = d.did")
        self.assertEqual(p["columns"].count("e.did") + p["columns"].count("d.did") + p["columns"].count("p.did"), 3)
        self.assertEqual(len(p["columns"]), 7 + 4 + 3)
        self.assertTrue(all("." in c for c in p["columns"]))

    def test_03_ambiguous_column_names_both_candidates(self):
        e = err("SELECT did FROM Emp, Dept")
        self.assertEqual(e.category, "ambiguity")
        self.assertIn("Emp.did", e.message)
        self.assertIn("Dept.did", e.message)

    def test_04_union_vs_union_all(self):
        self.assertNotEqual(plan("SELECT city FROM Emp UNION SELECT city FROM Dept")["op"],
                            plan("SELECT city FROM Emp UNION ALL SELECT city FROM Dept")["op"])

    def test_05_where_and_having_on_opposite_sides_of_the_grouping(self):
        p = plan("SELECT did FROM Emp WHERE age > 1 GROUP BY did HAVING COUNT(*) > 2")
        o = ops(p)
        self.assertEqual(o, ["project", "select", "group_by", "select", "scan"])
        self.assertEqual(p["child"]["condition"], "count_star > 2")           # HAVING, above the group_by
        self.assertEqual(p["child"]["child"]["child"]["condition"], "Emp.age > 1")   # WHERE, below it

    def test_06_non_grouped_column_is_an_aggregate_error(self):
        e = err("SELECT did, name FROM Emp GROUP BY did")
        self.assertEqual(e.category, "aggregate")
        self.assertIn("Emp.name", e.message)

    def test_07_aggregate_in_where(self):
        self.assertEqual(err("SELECT did FROM Emp WHERE COUNT(*) > 1").category, "aggregate")
        self.assertEqual(err("SELECT did FROM Emp, Dept WHERE SUM(Emp.age) > 1").category, "aggregate")

    def test_08_subquery_joined_to_a_base_table(self):
        p = plan("SELECT d.title, t.n FROM Dept d JOIN (SELECT did, COUNT(*) AS n FROM Emp GROUP BY did) AS t ON d.did = t.did")
        self.assertEqual(p["child"]["op"], "join")
        self.assertEqual(p["child"]["right"]["op"], "rename")
        self.assertEqual(p["child"]["right"]["to"], "t")
        self.assertEqual(p["child"]["condition"], "d.did = t.did")

    def test_09_order_by_a_column_not_in_the_select_list(self):
        p = plan("SELECT name FROM Emp ORDER BY age")
        self.assertEqual(ops(p), ["project", "sort", "scan"])                # sort below project: the column still exists there
        self.assertEqual(p["child"]["keys"], [["Emp.age", "asc"]])
        q = plan("SELECT name, age FROM Emp ORDER BY age DESC")
        self.assertEqual(ops(q), ["sort", "project", "scan"])                # canonical order when the column is selected
        self.assertEqual(err("SELECT DISTINCT name FROM Emp ORDER BY age").category, "schema")

    def test_10_type_error_comparing_int_to_string(self):
        e = err("SELECT name FROM Emp WHERE age > '30'")
        self.assertEqual(e.category, "type")
        self.assertEqual(err("SELECT name FROM Emp WHERE name = 5").category, "type")
        self.assertEqual(err("SELECT e.name FROM Emp e JOIN Dept d ON e.name = d.did").category, "type")

    def test_11_unknown_table_and_unknown_column_have_different_messages(self):
        a, b = err("SELECT x FROM Nope"), err("SELECT nothing FROM Emp")
        self.assertEqual((a.category, b.category), ("name", "name"))
        self.assertIn("unknown table 'Nope'", a.message)
        self.assertIn("unknown column 'nothing'", b.message)
        self.assertIn("does not exist in 'Emp'", err("SELECT Emp.zzz FROM Emp").message)
        self.assertIn("unknown table or alias 'q'", err("SELECT q.name FROM Emp").message)

    def test_12_duplicate_alias(self):
        e = err("SELECT * FROM Emp, Emp")
        self.assertIn("duplicate alias 'Emp'", e.message)
        compile_query_text("SELECT e.name FROM Emp e, Emp m WHERE e.mgr = m.eid", CAT)    # fine with aliases

    def test_13_count_star_without_group_by_is_one_row_even_when_empty(self):
        p = plan("SELECT COUNT(*) FROM Emp")
        self.assertEqual(p["child"]["keys"], [])
        prov = InMemoryProvider()
        prov.add("Emp", Schema(parse_schema(TABLES["Emp"])), [])
        rows = list(evaluate(from_dict(p, "logical"), prov).rows)
        self.assertEqual(rows, [(0,)])

    def test_14_union_of_mismatched_types_is_a_schema_error(self):
        self.assertEqual(err("SELECT name FROM Emp UNION SELECT did FROM Dept").category, "schema")
        self.assertEqual(err("SELECT name FROM Emp UNION SELECT did, city FROM Dept").category, "schema")

    def test_15_four_table_chain_commas_vs_join_on(self):
        commas = plan("SELECT e.name FROM Emp e, Dept d, Proj p, Task t "
                      "WHERE e.did = d.did AND p.did = d.did AND t.pid = p.pid AND t.hours > 3")
        joins = plan("SELECT e.name FROM Emp e JOIN Dept d ON e.did = d.did JOIN Proj p ON p.did = d.did "
                     "JOIN Task t ON t.pid = p.pid WHERE t.hours > 3")
        self.assertEqual(ops(commas).count("cross"), 3)
        self.assertEqual(ops(joins).count("join"), 3)
        self.assertEqual(ops(joins).count("cross"), 0)
        self.assertEqual(ops(commas).count("select"), 1)           # one select carrying the whole WHERE, unchanged
        self.assertEqual(commas["child"]["condition"],
                         "e.did = d.did and p.did = d.did and t.pid = p.pid and t.hours > 3")
        self.assertEqual(joins["child"]["condition"], "t.hours > 3")
        conds = []
        j = joins["child"]["child"]
        while j["op"] == "join":
            conds.append(j["condition"])
            j = j["left"]
        self.assertEqual(sorted(conds), ["e.did = d.did", "p.did = d.did", "t.pid = p.pid"])


class Checklist(unittest.TestCase):
    QUERIES = [
        "SELECT name FROM Emp WHERE age > 3", "SELECT * FROM Emp e, Dept d WHERE e.did = d.did",
        "SELECT did, COUNT(*) AS n FROM Emp GROUP BY did HAVING SUM(salary) > 5 ORDER BY n",
        "SELECT t.did FROM (SELECT did FROM Emp) t", "SELECT city FROM Emp EXCEPT SELECT city FROM Dept",
        "SELECT DISTINCT city FROM Emp ORDER BY city LIMIT 3",
    ]

    def test_no_physical_operator_names_and_no_unqualified_columns(self):
        physical = {"seqscan", "filter", "nested_loop_join", "sort_merge_join", "hash_join"}
        for q in self.QUERIES:
            p = plan(q)
            self.assertFalse(physical & set(ops(p)), q)
            self.assertNotIn("hash_join", json.dumps(p))

        def check(d):
            for k in ("columns", "keys"):
                for c in d.get(k, []):
                    name = c if isinstance(c, str) else c[0]
                    if d["op"] != "group_by" or k != "keys":
                        pass
            for k in ("child", "left", "right"):
                if k in d:
                    check(d[k])
        for q in self.QUERIES:
            check(plan(q))
        # scan columns are always qualified: only aggregate/alias outputs may be bare
        for c in plan("SELECT name, age FROM Emp")["columns"]:
            self.assertIn(".", c)

    def test_keywords_case_insensitive_identifiers_case_sensitive(self):
        a = plan("SeLeCt name FrOm Emp WhErE age > 3")
        self.assertEqual(a, plan("select name from Emp where age > 3"))
        self.assertEqual(err("SELECT name FROM emp").category, "name")

    def test_condition_precedence_not_and_or(self):
        p = plan("SELECT name FROM Emp WHERE NOT age = 1 AND did = 2 OR did = 3")
        self.assertEqual(p["child"]["condition"], "not Emp.age = 1 and Emp.did = 2 or Emp.did = 3")
        q = plan("SELECT name FROM Emp WHERE (age = 1 OR did = 2) AND did = 3")
        self.assertEqual(q["child"]["condition"], "(Emp.age = 1 or Emp.did = 2) and Emp.did = 3")

    def test_set_operators_are_left_associative(self):
        p = plan("SELECT city FROM Emp UNION SELECT city FROM Dept EXCEPT SELECT city FROM Dept")
        self.assertEqual((p["op"], p["left"]["op"]), ("except", "union"))

    def test_syntax_and_lexical_errors_have_positions(self):
        e = err("SELECT name FROM")
        self.assertEqual(e.category, "syntax")
        e = err("SELECT name FROM Emp WHERE name = 'abc")
        self.assertEqual(e.category, "lexical")
        self.assertEqual(format_error(e, "SELECT name FROM Emp WHERE name = 'abc"),
                         "error: lexical: unterminated string (missing closing quote) at line 1, column 35")
        self.assertEqual(err("SELECT name @ FROM Emp").category, "lexical")
        self.assertEqual(err("SELECT * FROM (SELECT did FROM Emp)").category, "syntax")
        self.assertEqual(err("SELECT SUM(*) FROM Emp").category, "syntax")

    def test_aggregate_type_rules(self):
        self.assertEqual(err("SELECT SUM(name) FROM Emp").category, "type")
        self.assertEqual(err("SELECT AVG(city) FROM Emp").category, "type")
        compile_query_text("SELECT MIN(name), MAX(city), COUNT(name) FROM Emp", CAT)

    def test_having_rules(self):
        self.assertEqual(err("SELECT did FROM Emp GROUP BY did HAVING age > 3").category, "aggregate")
        p = plan("SELECT did FROM Emp GROUP BY did HAVING SUM(salary) > 5")
        agg = p["child"]["child"]["aggregates"]
        self.assertEqual(agg, [["sum", "Emp.salary", "sum_Emp_salary"]])      # hidden aggregate, projected away

    def test_distinct_only_where_needed(self):
        self.assertIn("distinct", ops(plan("SELECT DISTINCT city FROM Emp")))
        self.assertNotIn("distinct", ops(plan("SELECT did, COUNT(*) FROM Emp GROUP BY did")))
        self.assertNotIn("distinct", ops(plan("SELECT DISTINCT did, COUNT(*) FROM Emp GROUP BY did")))
        self.assertIn("distinct", ops(plan("SELECT DISTINCT COUNT(*) FROM Emp GROUP BY did")))
        self.assertNotIn("distinct", ops(plan("SELECT name FROM Emp")))

    def test_column_alias(self):
        p = plan("SELECT name AS who FROM Emp ORDER BY who")
        self.assertEqual(p["child"]["columns"], ["Emp.name"])
        self.assertEqual(p["child"]["aliases"], ["who"])

    def test_ddl_checks(self):
        compile_query_text("CREATE TABLE T (a INT, b VARCHAR(5));", CAT)
        self.assertEqual(err("CREATE TABLE Emp (a INT)").category, "schema")
        self.assertEqual(err("INSERT INTO Dept VALUES (1, 'x')").category, "schema")
        self.assertEqual(err("INSERT INTO Dept VALUES ('a', 'x', 1, 'c')").category, "type")
        compile_query_text("INSERT INTO Dept VALUES (1, 'x', 5, 'c');", CAT)


class SemanticsOnData(unittest.TestCase):
    """The logical plans mean what SQL means: run them in the reference evaluator on a small database."""

    @classmethod
    def setUpClass(cls):
        cls.prov = InMemoryProvider()
        sch = {n: Schema(parse_schema(s)) for n, s in TABLES.items()}
        cls.prov.add("Emp", sch["Emp"], [(1, "ann", 30, 1, 0, 100, "x"), (2, "bob", 30, 1, 1, 80, "y"),
                                         (3, "cy", 41, 2, 1, 60, "x"), (4, "ann", 30, 2, 1, 90, "z")])
        cls.prov.add("Dept", sch["Dept"], [(1, "R&D", 5000, "x"), (2, "Ops", 500, "q"), (3, "HR", 900, "z")])

    def rows(self, sql):
        return sorted(evaluate(from_dict(plan(sql), "logical"), self.prov).rows)

    def test_bags_keep_duplicates(self):
        self.assertEqual(self.rows("SELECT name, age FROM Emp WHERE age = 30"), [("ann", 30), ("ann", 30), ("bob", 30)])
        self.assertEqual(self.rows("SELECT DISTINCT name, age FROM Emp WHERE age = 30"), [("ann", 30), ("bob", 30)])

    def test_union_vs_union_all_intersect_except(self):
        self.assertEqual(len(self.rows("SELECT city FROM Emp UNION ALL SELECT city FROM Dept")), 7)
        self.assertEqual(self.rows("SELECT city FROM Emp UNION SELECT city FROM Dept"), [("q",), ("x",), ("y",), ("z",)])
        self.assertEqual(self.rows("SELECT city FROM Emp INTERSECT SELECT city FROM Dept"), [("x",), ("z",)])
        self.assertEqual(self.rows("SELECT city FROM Emp EXCEPT SELECT city FROM Dept"), [("y",)])

    def test_group_by_having_and_where(self):
        self.assertEqual(self.rows("SELECT did, COUNT(*) AS n FROM Emp WHERE age > 25 GROUP BY did HAVING COUNT(*) > 1"),
                         [(1, 2), (2, 2)])
        self.assertEqual(self.rows("SELECT did, MAX(salary) FROM Emp GROUP BY did"), [(1, 100), (2, 90)])

    def test_joins_comma_and_on_agree(self):
        a = self.rows("SELECT e.name, d.title FROM Emp e, Dept d WHERE e.did = d.did AND d.budget > 1000")
        b = self.rows("SELECT e.name, d.title FROM Emp e JOIN Dept d ON e.did = d.did WHERE d.budget > 1000")
        self.assertEqual(a, b)
        self.assertEqual(a, [("ann", "R&D"), ("bob", "R&D")])

    def test_self_join_and_subquery(self):
        self.assertEqual(self.rows("SELECT e.name, m.name FROM Emp e JOIN Emp m ON e.mgr = m.eid"),
                         [("bob", "ann"), ("cy", "ann"), ("ann", "ann")] and sorted([("bob", "ann"), ("cy", "ann"), ("ann", "ann")]))
        self.assertEqual(self.rows("SELECT t.did FROM (SELECT did, COUNT(*) AS n FROM Emp GROUP BY did) AS t WHERE t.n > 1"),
                         [(1,), (2,)])

    def test_order_by_hidden_column_and_limit(self):
        out = list(evaluate(from_dict(plan("SELECT name FROM Emp ORDER BY salary DESC LIMIT 2"), "logical"), self.prov).rows)
        self.assertEqual(out, [("ann",), ("ann",)])
        out = list(evaluate(from_dict(plan("SELECT name, salary FROM Emp ORDER BY salary"), "logical"), self.prov).rows)
        self.assertEqual([r[1] for r in out], [60, 80, 90, 100])


class CommandLine(unittest.TestCase):
    def test_parse_compile_check(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "db")
            with Database(db, 8, create=True) as dbo:
                for n, s in TABLES.items():
                    dbo.create_table(n, parse_schema(s))

            def call(*a):
                out, er = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(er):
                    code = cli.main(list(a))
                return code, out.getvalue(), er.getvalue()

            code, out, _ = call("parse", "--query", "SELECT name FROM Emp WHERE age > 3", "--explain")
            self.assertEqual(code, 0)
            self.assertIn("Select", out)
            code, out, _ = call("compile", "--db", db, "--query", "SELECT name, age FROM Emp WHERE age > 30;", "--json")
            self.assertEqual(json.loads(out)["child"]["condition"], "Emp.age > 30")
            qf = os.path.join(d, "q.sql")
            with open(qf, "w") as f:
                f.write("SELECT zzz FROM Emp;")
            code, out, err_ = call("check", "--db", db, "--file", qf)
            self.assertEqual((code, out), (1, ""))
            self.assertEqual(err_.strip(), "error: name: unknown column 'zzz' at line 1, column 8")
            with open(qf, "w") as f:
                f.write("SELECT name FROM Emp;")
            self.assertEqual(call("check", "--db", db, "--file", qf)[0], 0)


if __name__ == "__main__":
    unittest.main()
