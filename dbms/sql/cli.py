"""`sql` command line (spec section 8):  sql parse | sql compile | sql check"""
import argparse
import json
import sys

from ..errors import DBError, format_error
from ..ra.plan_json import to_dict
from ..storage.database import Database
from .parser import parse_sql, ast_lines
from .translate import CatalogView, compile_query_text


def _read(a):
    if getattr(a, "file", None):
        with open(a.file, encoding="utf-8") as fh:
            return fh.read()
    if getattr(a, "query", None) is not None:
        return a.query
    raise DBError("syntax", "give --query TEXT or --file FILE")


def _catalog(a):
    with Database(a.db, 8) as db:
        return CatalogView.from_database(db)


def cmd_parse(a):
    src = _read(a)
    for st in parse_sql(src):
        print("\n".join(ast_lines(st)))


def cmd_compile(a):
    src = _read(a)
    plans = [to_dict(p) for _, p, _ in compile_query_text(src, _catalog(a)) if p is not None]
    if len(plans) == 1:
        print(json.dumps(plans[0], indent=2) if not a.json else json.dumps(plans[0]))
    else:
        print(json.dumps(plans, indent=2) if not a.json else json.dumps(plans))


def cmd_check(a):
    compile_query_text(_read(a), _catalog(a))


def main(argv=None):
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(prog="sql", description="Module 3: SQL front end")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("parse")
    p.add_argument("--query")
    p.add_argument("--file")
    p.add_argument("--explain", action="store_true")
    p.set_defaults(fn=cmd_parse)
    for name, fn in (("compile", cmd_compile), ("check", cmd_check)):
        p = sub.add_parser(name)
        p.add_argument("--db", "--catalog", dest="db", required=True)
        p.add_argument("--file")
        p.add_argument("--query")
        p.add_argument("--json", action="store_true")
        p.set_defaults(fn=fn)
    a = ap.parse_args(argv)
    src = None
    try:
        src = _read(a)
        a.fn(a)
        return 0
    except DBError as e:
        print(format_error(e, src), file=sys.stderr)
        return 1
    except OSError as e:
        print(f"error: storage: {e.strerror}: {getattr(e, 'filename', '')}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"error: internal: {type(e).__name__}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
