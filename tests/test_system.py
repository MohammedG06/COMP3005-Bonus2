"""Integration tests: the whole pipeline behind Engine and the shell."""
import contextlib
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import shell
from dbms.errors import DBError
from dbms.system import Engine, split_statements, format_result


class SplitStatements(unittest.TestCase):
    def test_semicolons_inside_strings_and_comments(self):
        parts = split_statements("SELECT 'a;b' FROM T; -- ignore; this\nSELECT 2 FROM U;  ")
        self.assertEqual([p[0].strip() for p in parts], ["SELECT 'a;b' FROM T", "SELECT 2 FROM U"])
        self.assertEqual(split_statements("SELECT 'it''s; ok' FROM T")[0][0], "SELECT 'it''s; ok' FROM T")

    def test_offsets_point_back_into_the_text(self):
        text = "SELECT 1 FROM A;\n  SELECT 2 FROM B;"
        for stmt, off in split_statements(text):
            self.assertEqual(text[off:off + len(stmt)], stmt)


class EngineSession(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "db")
        self.csv = os.path.join(self.tmp.name, "emp.csv")
        with open(self.csv, "w") as f:
            f.write("1,ann,30,10\n2,bo,41,10\n3,cy,25,20\n4,di,52,20\n5,ed,37,30\n")
        self.eng = Engine(self.path, frames=8)
        self.eng.run_all("""CREATE TABLE Emp (eid INT, name VARCHAR(10), age INT, did INT);
                            CREATE TABLE Dept (did INT, title VARCHAR(10));""")
        self.eng.run_all(f"LOAD Emp FROM '{self.csv}'")
        self.eng.run_all("INSERT INTO Dept VALUES (10, 'R&D'); INSERT INTO Dept VALUES (20, 'Ops'); INSERT INTO Dept VALUES (30, 'HR');")

    def tearDown(self):
        self.tmp.cleanup()

    def rows(self, sql):
        return self.eng.run_all(sql)[-1].rows

    def test_queries(self):
        self.assertEqual(self.rows("SELECT e.name, d.title FROM Emp e JOIN Dept d ON e.did = d.did WHERE e.age > 30 ORDER BY e.name"),
                         [("bo", "R&D"), ("di", "Ops"), ("ed", "HR")])
        self.assertEqual(self.rows("SELECT did, COUNT(*) AS n FROM Emp GROUP BY did HAVING COUNT(*) > 1 ORDER BY did"),
                         [(10, 2), (20, 2)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM Emp"), [(5,)])
        self.assertEqual(self.rows("SELECT DISTINCT did FROM Emp ORDER BY did DESC LIMIT 2"), [(30,), (20,)])
        self.assertEqual(self.rows("SELECT AVG(age) AS a FROM Emp"), [(37.0,)])

    def test_results_stream_before_a_later_error(self):
        seen = []
        with self.assertRaises(DBError) as cm:
            for r in self.eng.run("SELECT COUNT(*) FROM Emp; SELECT nope FROM Emp;"):
                seen.append(r)
        self.assertEqual(len(seen), 1)
        self.assertEqual(cm.exception.category, "name")
        self.assertEqual(cm.exception.line_col(cm.exception.statement), (1, 26 + 7 + 1))     # whole-script position: 1-based column of `nope`

    def test_statistics_change_the_estimates_not_the_answers(self):
        before = self.eng.run_all("SELECT * FROM Emp WHERE age > 30")[-1]
        self.eng.run_all("ANALYZE")
        after = self.eng.run_all("SELECT * FROM Emp WHERE age > 30")[-1]
        self.assertEqual(sorted(before.rows), sorted(after.rows))
        self.assertEqual(after.stats["actual_rows_root"], 3)
        self.assertEqual(after.stats["est_rows_root"], 3)

    def test_explain_and_explain_analyze(self):
        self.eng.run_all("ANALYZE")
        e = self.eng.run_all("EXPLAIN SELECT e.name FROM Emp e, Dept d WHERE e.did = d.did AND e.age > 30")[-1]
        text = "\n".join(e.lines)
        self.assertIn("est_rows=", text)
        self.assertIn("total est_cost", text)
        a = self.eng.run_all("EXPLAIN ANALYZE SELECT e.name FROM Emp e, Dept d WHERE e.did = d.did AND e.age > 30")[-1]
        text = "\n".join(a.lines)
        self.assertIn("actual_rows=", text)
        self.assertIn("q-error=", text)
        self.assertIn("peak frames held", text)

    def test_set_frames_and_every_budget_gives_the_same_rows(self):
        want = sorted(self.rows("SELECT e.name, d.title FROM Emp e, Dept d WHERE e.did = d.did"))
        for n in (3, 4, 8, 64):
            self.eng.run_all(f"SET frames = {n}")
            self.assertEqual(self.eng.frames, n)
            got = self.eng.run_all("SELECT e.name, d.title FROM Emp e, Dept d WHERE e.did = d.did")[-1]
            self.assertEqual(sorted(got.rows), want)
            self.assertLessEqual(got.stats["peak_frames"], n)
        with self.assertRaises(DBError):
            self.eng.run_all("SET frames = 2")

    def test_ddl_dml_errors(self):
        for sql, cat in [("CREATE TABLE Emp (a INT)", "schema"), ("INSERT INTO Emp VALUES (1, 'x')", "schema"),
                         ("INSERT INTO Nope VALUES (1)", "name"), ("LOAD Emp FROM '" + self.csv + ".missing'", "storage"),
                         ("SELECT * FROM", "syntax"), ("FROB Emp", "syntax")]:
            with self.assertRaises(DBError, msg=sql) as cm:
                self.eng.run_all(sql)
            self.assertEqual(cm.exception.category, cat, sql)

    def test_data_survives_a_restart(self):
        eng2 = Engine(self.path, frames=16)
        self.assertEqual(eng2.run_all("SELECT COUNT(*) FROM Emp")[-1].rows, [(5,)])
        self.assertEqual(eng2.run_all("SELECT COUNT(*) FROM Dept")[-1].rows, [(3,)])

    def test_format_result(self):
        out = format_result(self.eng.run_all("SELECT name, age FROM Emp WHERE age > 40 ORDER BY age")[-1])
        self.assertIn("name | age", out)
        self.assertIn("(2 rows)", out)
        self.assertIn("(0 rows)", format_result(self.eng.run_all("SELECT name FROM Emp WHERE age > 99")[-1]))


class ShellCommandLine(unittest.TestCase):
    def test_script_prints_as_it_goes_and_never_a_traceback(self):
        with tempfile.TemporaryDirectory() as d:
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = shell.main(["--db", os.path.join(d, "db"), "-c",
                                   "CREATE TABLE T (a INT); INSERT INTO T VALUES (1); INSERT INTO T VALUES (2); "
                                   "SELECT a FROM T ORDER BY a; SELECT zz FROM T;"])
            self.assertEqual(code, 1)
            self.assertIn("(2 rows)", out.getvalue())
            self.assertEqual(err.getvalue().strip(), "error: name: unknown column 'zz' at line 1, column " + str(len(
                "CREATE TABLE T (a INT); INSERT INTO T VALUES (1); INSERT INTO T VALUES (2); SELECT a FROM T ORDER BY a; SELECT ") + 1))
            self.assertNotIn("Traceback", err.getvalue())


if __name__ == "__main__":
    unittest.main()
