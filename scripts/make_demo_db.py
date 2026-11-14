#!/usr/bin/env python3
"""Builds a demo database with realistic skew and one strongly correlated pair of columns, plus a file
of 20 queries. For trying the whole system before the course dataset is published.

  python scripts/make_demo_db.py --out demo_db [--scale 1]
  python shell.py --db demo_db
  python scripts/m4_measure.py qerror --db demo_db --queries demo_db/queries.sql"""
import argparse
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dbms.optimizer.stats import analyze_table
from dbms.storage.database import Database
from dbms.storage.records import parse_schema

QUERIES = """
SELECT e.name, d.title FROM Emp e JOIN Dept d ON e.did = d.did WHERE e.age > 60;
SELECT e.name FROM Emp e, Dept d WHERE e.did = d.did AND d.budget > 1500 AND e.age < 30;
SELECT did, COUNT(*) AS n, AVG(sal) AS a FROM Emp GROUP BY did HAVING COUNT(*) > 40 ORDER BY n DESC;
SELECT e.name, p.pname FROM Emp e, Proj p, Dept d WHERE p.did = d.did AND e.did = d.did AND p.budget > 500;
SELECT COUNT(*) FROM Emp WHERE city = 'Ottawa' AND zip = 'K1A';
SELECT COUNT(*) FROM Emp WHERE city = 'Toronto' AND zip = 'M5V';
SELECT DISTINCT city FROM Emp WHERE age > 40 ORDER BY city;
SELECT e.name FROM Emp e WHERE e.sal > 900 ORDER BY e.sal DESC LIMIT 5;
SELECT d.title, COUNT(*) AS n FROM Dept d, Emp e, Proj p WHERE e.did = d.did AND p.did = d.did GROUP BY d.title;
SELECT name FROM Emp WHERE age > 55 UNION SELECT pname FROM Proj WHERE budget > 900;
SELECT t.did, t.n FROM (SELECT did, COUNT(*) AS n FROM Emp GROUP BY did) AS t WHERE t.n > 50;
SELECT e.name, m.name FROM Emp e JOIN Emp m ON e.mgr = m.eid WHERE m.age > 58;
SELECT COUNT(*) FROM Emp WHERE age >= 30 AND age <= 40;
SELECT e.name, p.pname FROM Emp e, Proj p WHERE e.did = p.did AND e.city = 'Ottawa' AND e.zip = 'K1A';
SELECT p.pname, d.title FROM Proj p JOIN Dept d ON p.did = d.did WHERE p.budget > 800;
SELECT city, COUNT(*) AS n FROM Emp GROUP BY city;
SELECT e.name, p.pname FROM Emp e JOIN Dept d ON e.did = d.did JOIN Proj p ON p.did = d.did WHERE e.age > 60 AND p.budget > 900;
SELECT MIN(sal), MAX(sal), AVG(age) FROM Emp WHERE did < 10;
SELECT did FROM Emp WHERE age > 50 INTERSECT SELECT did FROM Proj WHERE budget > 900;
SELECT title FROM Dept WHERE budget > 1000 EXCEPT SELECT d.title FROM Dept d, Proj p WHERE p.did = d.did AND p.budget > 950;
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--scale", type=int, default=1)
    a = ap.parse_args()
    rnd = random.Random(42)
    n_emp, n_dept, n_proj = 3000 * a.scale, 40, 600 * a.scale
    places = [("Ottawa", "K1A"), ("Toronto", "M5V"), ("Montreal", "H2X"), ("Calgary", "T2P"), ("Halifax", "B3H")]
    with Database(a.out, 32, create=True) as db:
        for name, sch in (("Emp", "eid:INT,name:VARCHAR(12),age:INT,sal:INT,did:INT,mgr:INT,city:VARCHAR(10),zip:VARCHAR(4),pad:VARCHAR(60)"),
                          ("Dept", "did:INT,title:VARCHAR(12),budget:INT,pad:VARCHAR(60)"),
                          ("Proj", "pid:INT,pname:VARCHAR(12),did:INT,budget:INT,pad:VARCHAR(60)")):
            db.create_table(name, parse_schema(sch))
        h, _ = db.open_heap("Dept")
        for i in range(n_dept):
            h.insert((i, f"dept{i}", rnd.randrange(200, 2000), "d" * 40))
        db.refresh_counts("Dept", n_dept)
        h, _ = db.open_heap("Emp")
        for i in range(n_emp):
            city, zipc = rnd.choices(places, weights=[5, 4, 3, 2, 1])[0]          # city determines zip: strongly correlated
            did = min(n_dept - 1, int(rnd.expovariate(0.08)))                      # skew: low department numbers are big
            h.insert((i, f"emp{i % 700}", 20 + rnd.randrange(46), rnd.randrange(300, 1000), did, rnd.randrange(n_emp), city, zipc, "e" * 40))
        db.refresh_counts("Emp", n_emp)
        h, _ = db.open_heap("Proj")
        for i in range(n_proj):
            h.insert((i, f"proj{i % 200}", rnd.randrange(n_dept), rnd.randrange(100, 1000), "p" * 40))
        db.refresh_counts("Proj", n_proj)
        for t in ("Emp", "Dept", "Proj"):
            analyze_table(db, t, 20, 16)
    with open(os.path.join(a.out, "queries.sql"), "w") as f:
        f.write(QUERIES.strip() + "\n")
    print(f"built {a.out} with {n_emp} Emp, {n_dept} Dept, {n_proj} Proj rows (statistics collected); queries in {a.out}/queries.sql")


if __name__ == "__main__":
    main()
