#!/usr/bin/env python3
"""Bonus-1 style CLI over the refactored library:
     python ra.py --tree "project[Name](select[Age>30](Employees))"
     python ra.py --db data/employees.ra "project[DID](Employees)"
     python ra.py --json "project[Name](Employees)"        # print the plan as JSON"""
import argparse
import sys

from dbms.errors import DBError, format_error_caret
from dbms.ra import parse_ra, render_tree, evaluate, load_relations, to_json
from dbms.ra.format import format_table
from dbms.ra.tables import InMemoryProvider


def main(argv=None):
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="relational algebra (bag semantics)")
    ap.add_argument("query")
    ap.add_argument("--db", action="append", default=[])
    ap.add_argument("--tree", action="store_true")
    ap.add_argument("--json", action="store_true", help="print the plan as JSON, do not run it")
    ap.add_argument("--limit", type=int, default=100)
    a = ap.parse_args(argv)
    try:
        tree = parse_ra(a.query)
        if a.tree:
            print(render_tree(tree))
            return 0
        if a.json:
            print(to_json(tree))
            return 0
        prov = InMemoryProvider()
        for path in a.db:
            try:
                text = open(path, encoding="utf-8").read()
            except OSError as e:
                raise DBError("data", f"cannot read {path}: {e.strerror}")
            other = load_relations(text, path)
            for n in other.names():
                prov.add(n, other.table_schema(n), list(other.table_rows(n)))
        rs = evaluate(tree, prov)
        print(format_table(rs.schema, rs.rows, a.limit))
        return 0
    except DBError as e:
        print(format_error_caret(e, a.query), file=sys.stderr)
        return 1
    except Exception as e:
        print(f"error: internal: {type(e).__name__}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
