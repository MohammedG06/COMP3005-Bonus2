"""`storage` command line (spec section 7)."""
import argparse
import json
import sys

from ..errors import DBError, format_error
from .database import Database
from .layout import layout_text
from .loader import load_csv
from .records import parse_schema
from .trace import read_trace, run_trace


def _out(obj):
    print(json.dumps(obj))


def cmd_create_table(a):
    cols = parse_schema(a.schema)
    with Database(a.db, a.frames, a.policy, create=True) as db:
        db.create_table(a.name, cols)
    print(f"created table {a.name}")


def cmd_load(a):
    with Database(a.db, a.frames, a.policy) as db:
        n = load_csv(db, a.table, a.file)
        info = db.catalog.get_table(a.table)
    print(f"loaded {n} rows into {a.table} ({info.num_tuples} tuples, {info.num_pages} data pages)")


def cmd_scan(a):
    with Database(a.db, a.frames, a.policy) as db:
        heap, info = db.open_heap(a.table)
        db.pool.clear()               # cold cache: counters below describe the scan alone
        db.pool.reset_stats()
        rows = []
        gen = heap.scan()
        try:
            for _, t in gen:
                rows.append(list(t))
                if a.limit is not None and len(rows) >= a.limit:
                    break
        finally:
            gen.close()
        stats = db.pool.stats()
        if db.pool.pinned_count():
            raise DBError("storage", "scan finished with pages still pinned (bug)")
    if a.json:
        _out({"table": a.table, "schema": [c.to_json(False) for c in info.columns],
              "rows": rows, "stats": stats})
    else:
        names = [c.name for c in info.columns]
        cells = [[str(v) for v in r] for r in rows]
        w = [max([len(n)] + [len(c[i]) for c in cells]) for i, n in enumerate(names)]
        print(" | ".join(n.ljust(x) for n, x in zip(names, w)).rstrip())
        print("-+-".join("-" * x for x in w))
        for c in cells:
            print(" | ".join(v.ljust(x) for v, x in zip(c, w)).rstrip())
        print(f"({len(rows)} rows)  {stats}")


def cmd_layout(a):
    with Database(a.db, a.frames, a.policy) as db:
        heap, info = db.open_heap(a.table)
        chain = heap.dir_chain()
        fr = db.pool.fetch_page(a.page, heap.file_id)
        try:
            print(layout_text(fr.data, a.page, a.page in chain, heap.codec))
        finally:
            db.pool.unpin_page(a.page, False, heap.file_id)


def cmd_trace(a):
    res = run_trace(read_trace(a.file), a.frames, a.policy)
    if a.json:
        _out(res)
    else:
        print(res)


def cmd_tables(a):
    with Database(a.db, a.frames, a.policy) as db:
        out = [db.catalog.get_table(n).to_dict() for n in db.catalog.list_tables()]
    if a.json:
        _out(out)
    else:
        for t in out:
            print(f"{t['name']}: {t['schema']}  pages={t['num_pages']} tuples={t['num_tuples']}")


def build_parser():
    ap = argparse.ArgumentParser(prog="storage", description="Module 1: storage engine")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, frames=True):
        p.add_argument("--db", required=True, help="database directory")
        p.add_argument("--frames", type=int, default=16)
        p.add_argument("--policy", choices=["lru", "clock"], default="lru")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("create-table"); common(p)
    p.add_argument("--name", required=True); p.add_argument("--schema", required=True)
    p.set_defaults(fn=cmd_create_table)
    p = sub.add_parser("load"); common(p)
    p.add_argument("--table", required=True); p.add_argument("--file", required=True)
    p.set_defaults(fn=cmd_load)
    p = sub.add_parser("scan"); common(p)
    p.add_argument("--table", required=True); p.add_argument("--limit", type=int)
    p.set_defaults(fn=cmd_scan)
    p = sub.add_parser("layout"); common(p)
    p.add_argument("--table", required=True); p.add_argument("--page", type=int, required=True)
    p.set_defaults(fn=cmd_layout)
    p = sub.add_parser("trace")
    p.add_argument("--db", default=None, help="accepted for compatibility; the trace uses a simulated disk")
    p.add_argument("--file", required=True); p.add_argument("--frames", type=int, default=16)
    p.add_argument("--policy", choices=["lru", "clock"], default="lru"); p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_trace)
    p = sub.add_parser("tables"); common(p)
    p.set_defaults(fn=cmd_tables)
    return ap


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
        return 0
    except DBError as e:
        print(format_error(e), file=sys.stderr)
        return 1
    except Exception as e:           # never a stack trace
        print(f"error: internal: {type(e).__name__}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
