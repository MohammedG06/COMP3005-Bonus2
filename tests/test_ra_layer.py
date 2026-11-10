"""Engine refactor tests: the 25 Bonus-1 cases on the new layered library, plus JSON round trip.

Cases whose meaning CHANGED under bag semantics / the Module 2 rules (logged in DESIGN_LOG.md):
  23  project[DID](Employees) now returns 3 tuples (project keeps duplicates); distinct removes them
  21  union compatibility is by column count and type only (names may differ); names-only mismatch is no longer an error
  10, 11  the tree labels are now the shared logical operator names (minus -> Except, times -> Cross)
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dbms.errors import DBError
from dbms.ra import (parse_ra, render_tree, evaluate, load_relations, parse_condition, unparse,
                     compile_condition, to_json, from_json, to_dict, from_dict)
from dbms.ra.condition import parse_attr
from dbms.ra.format import format_table
from dbms.ra.nodes import Node, cond_str
from dbms.ra.tokenizer import tokenize
from dbms.schema import Schema, Column, INT, VARCHAR

EMPLOYEES = """
// employees and their departments
Employees (EID, Name, Age, DID) = {
  E1, John, 32, D1
  E2, Alice, 28, D2
  E3, Bob, 29, D1
}
"""
EMP_DEPT = """
Emp (EID, Name, DID, MgrID) = {
  1, Ann, 10, 0
  2, Bo, 10, 1
  3, Cy, 20, 1
}
Dept (DID, DName) = {
  10, Sales
  20, Eng
}
"""


def run(query, data):
    prov = load_relations(data)
    rs = evaluate(parse_ra(query), prov)
    return rs.schema, list(rs.rows)


def err(query, data=""):
    try:
        run(query, data)
    except DBError as e:
        return e
    raise AssertionError("expected a DBError")


def texts(src):
    return [t.text for t in tokenize(src)][:-1]


def col(name, *vals):
    return f"{name} (x) = {{\n" + "".join(f"  {v}\n" for v in vals) + "}\n"


class Tokenizer(unittest.TestCase):
    def test_01_no_whitespace(self):
        self.assertEqual(texts("select[x1=3](R)"), ["select", "[", "x1", "=", "3", "]", "(", "R", ")"])

    def test_02_whitespace_same_tree(self):
        self.assertEqual(render_tree(parse_ra("select[x1=3](R)")), render_tree(parse_ra("select[ x1 = 3 ](R)")))

    def test_03_ge_one_token(self):
        self.assertEqual(texts("select[Age>=30](R)")[3], ">=")

    def test_04_gt_then_negative(self):
        toks = tokenize("select[Age>-30](R)")
        self.assertEqual([t.text for t in toks][3:5], [">", "-30"])
        self.assertEqual(toks[4].value, -30)

    def test_04b_not_equal_forms(self):
        self.assertEqual(texts("a<>1")[1], "<>")
        self.assertEqual(texts("a!=1")[1], "!=")
        self.assertEqual(cond_str(parse_condition("a != 1")), cond_str(parse_condition("a <> 1")))

    def test_05_paren_in_string(self):
        _, rows = run("select[Name='Bob)'](R)", "R (Name, k) = {\n 'Bob)', 1\n Alice, 2\n}")
        self.assertEqual(rows, [("Bob)", 1)])

    def test_06_comma_in_string(self):
        _, rows = run("select[Name='a,b'](R)", "R (Name, k) = {\n 'a,b', 1\n c, 2\n}")
        self.assertEqual(rows, [("a,b", 1)])

    def test_07_doubled_quote(self):
        _, rows = run("select[Name='O''Brien'](R)", "R (Name, k) = {\n 'O''Brien', 1\n Bob, 2\n}")
        self.assertEqual(rows, [("O'Brien", 1)])

    def test_08_keyword_as_attribute(self):
        _, rows = run("select[union=3](R)", "R (union, x) = {\n 3, 1\n 4, 2\n}")
        self.assertEqual(rows, [(3, 1)])
        _, rows = run("select[not=3 and and=1](R)", "R (not, and) = {\n 3, 1\n 3, 2\n}")
        self.assertEqual(rows, [(3, 1)])

    def test_09_unterminated(self):
        e = err("select[Name='Bob](R)", "R (Name) = {\n Bob\n}")
        self.assertEqual((e.category, e.pos), ("lexical", len("select[Name=")))


class Grammar(unittest.TestCase):
    def test_10_union_minus_left(self):
        lines = render_tree(parse_ra("A union B minus C")).split("\n")
        self.assertEqual(lines[:2], ["Except", "├── Union"])

    def test_11_minus_left_assoc_data(self):
        self.assertEqual(render_tree(parse_ra("A minus B minus C")).split("\n")[:2], ["Except", "├── Except"])
        d = col("A", 1) + col("B", 1) + col("C", 1)
        self.assertEqual(run("A minus B minus C", d)[1], [])
        self.assertEqual(run("A minus (B minus C)", d)[1], [(1,)])

    def test_12_13_condition_precedence(self):
        c = parse_ra("select[not (a=1 and b=2) or c>3](R)").cond
        self.assertEqual(cond_str(c), "Or(Not(And(Eq(Attr(a), Num(1)), Eq(Attr(b), Num(2)))), Gt(Attr(c), Num(3)))")
        c = parse_ra("select[a=1 and b=2 or c=3](R)").cond
        self.assertEqual(cond_str(c), "Or(And(Eq(Attr(a), Num(1)), Eq(Attr(b), Num(2))), Eq(Attr(c), Num(3)))")

    def test_14_nesting(self):
        _, rows = run("project[Name](select[Age>30](select[DID='D1'](Employees)))", EMPLOYEES)
        self.assertEqual(rows, [("John",)])

    def test_15_parens(self):
        d = col("A", 1, 2) + col("B", 3) + col("C", 2, 3, 9) + col("D", 3, 2)
        self.assertEqual(run("(A union B) minus (C intersect D)", d)[1], [(1,)])
        d2 = col("A", 1) + col("B", 1) + col("C", 1)
        self.assertEqual(run("A union B minus C", d2)[1], [])
        self.assertEqual(run("A union (B minus C)", d2)[1], [(1,)])

    def test_16_missing_paren(self):
        q = "select[Age>30](R"
        e = err(q, "R (Age) = {\n 1\n}")
        self.assertEqual((e.category, e.pos), ("syntax", len(q)))
        self.assertIn("')'", e.message)

    def test_17_empty_project(self):
        e = err("project[](R)", "R (a) = {\n 1\n}")
        self.assertEqual((e.category, e.pos), ("syntax", len("project[")))


class Semantics(unittest.TestCase):
    def test_18_two_columns(self):
        _, rows = run("select[A=B](R)", "R (A, B) = {\n 1, 1\n 1, 2\n 3, 3\n}")
        self.assertEqual(sorted(rows), [(1, 1), (3, 3)])

    def test_19_qualified_join(self):
        schema, rows = run("Emp join[Emp.DID=Dept.DID] Dept", EMP_DEPT)
        self.assertEqual(schema.qnames(), ["Emp.EID", "Emp.Name", "Emp.DID", "Emp.MgrID", "Dept.DID", "Dept.DName"])
        self.assertEqual(len(rows), 3)
        self.assertIn((1, "Ann", 10, 0, 10, "Sales"), rows)

    def test_20_self_join_needs_rename(self):
        _, rows = run("rename[E2](Emp) join[Emp.MgrID=E2.EID] Emp", EMP_DEPT)
        self.assertEqual(len(rows), 2)
        self.assertIn((1, "Ann", 10, 0, 2, "Bo", 10, 1), rows)
        self.assertEqual(err("Emp join[Emp.MgrID=Emp.EID] Emp", EMP_DEPT).category, "schema")

    def test_21_union_compat(self):
        self.assertEqual(err("R union S", "R (x, y) = {\n 1, 2\n}\nS (x) = {\n 1\n}").category, "schema")
        self.assertEqual(err("R union S", "R (x) = {\n 1\n}\nS (y) = {\n 'a'\n}").category, "schema")
        # CHANGED: names may differ now (Module 2 rule); output takes the left names
        schema, rows = run("R union S", "R (x) = {\n 1\n}\nS (y) = {\n 2\n}")
        self.assertEqual((schema.qnames(), sorted(rows)), (["R.x"], [(1,), (2,)]))

    def test_22_type_error(self):
        self.assertEqual(err("select[Age>'30'](Employees)", EMPLOYEES).category, "type")

    def test_23_project_keeps_duplicates(self):
        _, rows = run("project[DID](Employees)", EMPLOYEES)
        self.assertEqual(len(rows), 3)                                  # CHANGED from 2 (bags)
        prov = load_relations(EMPLOYEES)
        tree = Node("distinct", child=parse_ra("project[DID](Employees)"))
        self.assertEqual(len(list(evaluate(tree, prov).rows)), 2)

    def test_24_project_repeated_column(self):
        self.assertEqual(err("project[Name, Name](Employees)", EMPLOYEES).category, "schema")

    def test_25_empty_result(self):
        schema, rows = run("select[Age>100](Employees)", EMPLOYEES)
        out = format_table(schema, rows)
        self.assertIn("Employees.EID", out)
        self.assertIn("(0 tuples)", out)

    def test_name_and_ambiguity_errors(self):
        self.assertEqual(err("Nope", EMPLOYEES).category, "name")
        self.assertEqual(err("select[Salary>1](Employees)", EMPLOYEES).category, "name")
        self.assertEqual(err("select[DID=10](Emp times Dept)", EMP_DEPT).category, "ambiguity")


class StandaloneAndJson(unittest.TestCase):
    def test_condition_parser_standalone(self):
        c = parse_condition("R.a > 500 and R.b = 'cs'")
        schema = Schema([Column("a", INT, qual="R"), Column("b", VARCHAR, 5, qual="R")])
        f = compile_condition(c, schema)
        self.assertTrue(f((501, "cs"), None))
        self.assertFalse(f((501, "xx"), None))
        self.assertFalse(f((500, "cs"), None))

    def test_condition_unparse_round_trip(self):
        for text in ["a = 1", "not (a = 1 and b = 2) or c > 3", "a = 1 and (b = 2 or c = 3)",
                     "a = 1 or b = 2 or c = 3", "a = 1 or (b = 2 or c = 3)", "not not a = 1",
                     "R.a <> 'it''s'", "x >= -5"]:
            c1 = parse_condition(text)
            c2 = parse_condition(unparse(c1))
            self.assertEqual(cond_str(c1), cond_str(c2), text)

    def test_condition_keywords_case_insensitive(self):
        self.assertEqual(cond_str(parse_condition("a = 1 AND b = 2")), cond_str(parse_condition("a = 1 and b = 2")))

    def test_evaluator_has_no_dependency_on_lists(self):
        class Provider:                      # anything with these three methods will do
            def has_table(self, n): return n == "T"
            def table_schema(self, n): return Schema([Column("x", INT)])
            def table_rows(self, n): return ((i,) for i in range(5))      # a generator, not a list
        rs = evaluate(parse_ra("select[x>2](T)"), Provider())
        self.assertEqual(list(rs.rows), [(3,), (4,)])

    ALL_PLANS = {
        "scan": {"op": "scan", "table": "R"},
        "select": {"op": "select", "condition": "R.a > 1", "child": {"op": "scan", "table": "R"}},
        "project": {"op": "project", "columns": ["R.a", "R.b"], "child": {"op": "scan", "table": "R"}},
        "rename": {"op": "rename", "to": "e", "child": {"op": "scan", "table": "R"}},
        "join": {"op": "join", "condition": "R.a = S.a", "left": {"op": "scan", "table": "R"}, "right": {"op": "scan", "table": "S"}},
        "cross": {"op": "cross", "left": {"op": "scan", "table": "R"}, "right": {"op": "scan", "table": "S"}},
        "group_by": {"op": "group_by", "keys": ["R.a"], "aggregates": [["count", "*", "n"], ["avg", "R.b"]], "child": {"op": "scan", "table": "R"}},
        "distinct": {"op": "distinct", "child": {"op": "scan", "table": "R"}},
        "sort": {"op": "sort", "keys": [["R.a", "asc"], ["R.b", "desc"]], "child": {"op": "scan", "table": "R"}},
        "limit": {"op": "limit", "n": 10, "child": {"op": "scan", "table": "R"}},
        "union": {"op": "union", "left": {"op": "scan", "table": "R"}, "right": {"op": "scan", "table": "S"}},
        "union_all": {"op": "union_all", "left": {"op": "scan", "table": "R"}, "right": {"op": "scan", "table": "S"}},
        "intersect": {"op": "intersect", "left": {"op": "scan", "table": "R"}, "right": {"op": "scan", "table": "S"}},
        "except": {"op": "except", "left": {"op": "scan", "table": "R"}, "right": {"op": "scan", "table": "S"}},
    }
    PHYSICAL_PLANS = {
        "seqscan": {"op": "seqscan", "table": "R"},
        "filter": {"op": "filter", "condition": "R.a > 500 and R.b = 'cs'", "child": {"op": "seqscan", "table": "R"}},
        "nested_loop_join": {"op": "nested_loop_join", "condition": "R.b < S.b", "left": {"op": "seqscan", "table": "R"}, "right": {"op": "seqscan", "table": "S"}},
        "sort_merge_join": {"op": "sort_merge_join", "condition": "R.b = S.b", "left": {"op": "seqscan", "table": "R"}, "right": {"op": "seqscan", "table": "S"}},
        "hash_join": {"op": "hash_join", "condition": "R.b = S.b", "left": {"op": "seqscan", "table": "R"}, "right": {"op": "seqscan", "table": "S"}},
        "distinct_hash": {"op": "distinct", "mode": "hash", "child": {"op": "seqscan", "table": "R"}},
        "group_by_sort": {"op": "group_by", "mode": "sort", "keys": [], "aggregates": [["max", "R.name"]], "child": {"op": "seqscan", "table": "R"}},
    }

    def test_json_round_trip_every_node_type(self):
        for name, d in self.ALL_PLANS.items():
            self.assertEqual(to_dict(from_dict(d, "logical")), d, name)
        for name, d in self.PHYSICAL_PLANS.items():
            self.assertEqual(to_dict(from_dict(d, "physical")), d, name)
        t = parse_ra("project[Name](select[Age>30 or not DID='D1'](Employees))")
        self.assertEqual(to_json(from_json(to_json(t))), to_json(t))

    def test_plan_validation_messages(self):
        bad = [
            ({"op": "sort", "child": {"op": "scan", "table": "R"}}, "Node 'sort': missing required field 'keys'."),
            ({"op": "filter", "condition": 5, "child": {"op": "seqscan", "table": "R"}}, "Node 'filter': field 'condition' must be a string."),
            ({"op": "merge_join", "left": {}, "right": {}}, "Node 'merge_join': unknown operator 'merge_join'."),
        ]
        for d, msg in bad:
            with self.assertRaises(DBError) as cm:
                from_dict(d, "physical")
            self.assertTrue(cm.exception.message.startswith(msg), cm.exception.message)
        with self.assertRaises(DBError) as cm:
            from_json('{"op": "scan", ')
        self.assertIn("malformed JSON", cm.exception.message)
        self.assertIn("line 1", cm.exception.message)

    def test_bag_semantics_operators(self):
        prov = load_relations("R (a, b) = {\n 1, 1\n 1, 2\n 2, 1\n}")
        rows = lambda q: list(evaluate(parse_ra(q), prov).rows)
        self.assertEqual(len(rows("project[a](R)")), 3)
        self.assertEqual(len(rows("project[a](R) union project[a](R)")), 2)         # union dedupes
        t = from_dict({"op": "union_all", "left": parse_dict("project[a](R)"), "right": parse_dict("project[a](R)")})
        self.assertEqual(len(list(evaluate(t, prov).rows)), 6)                     # union_all does not

    def test_group_by_and_sort_limit(self):
        prov = load_relations("R (k, v) = {\n a, 1\n a, 2\n b, 5\n}")
        d = {"op": "limit", "n": 1, "child": {"op": "sort", "keys": [["n", "desc"]], "child": {
            "op": "group_by", "keys": ["R.k"], "aggregates": [["count", "*", "n"], ["sum", "R.v", "s"]],
            "child": {"op": "scan", "table": "R"}}}}
        rs = evaluate(from_dict(d), prov)
        self.assertEqual(list(rs.rows), [("a", 2, 3)])
        empty = load_relations("R (k, v) = {\n}")
        d2 = {"op": "group_by", "keys": [], "aggregates": [["count", "*"]], "child": {"op": "scan", "table": "R"}}
        self.assertEqual(list(evaluate(from_dict(d2), empty).rows), [(0,)])
        d3 = {"op": "group_by", "keys": ["R.k"], "aggregates": [["count", "*"]], "child": {"op": "scan", "table": "R"}}
        self.assertEqual(list(evaluate(from_dict(d3), empty).rows), [])


def parse_dict(q):
    return to_dict(parse_ra(q))


if __name__ == "__main__":
    unittest.main()
