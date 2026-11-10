"""`opt` command line (spec M4 section 6):  opt analyze | plan | explain | dp-table"""
import argparse
import json
import sys

from ..errors import DBError, format_error
from ..ra.nodes import render_tree
from ..ra.plan_json import from_json
from ..storage.database import Database
from .api import optimize
from .planner import explain_lines, dp_table_lines
from .rewrite import ALL_RULES
from .stats import analyze_table


def _logical(a):
    with open(a.logical, encoding="utf-8") as fh:
        return from_json(fh.read(), "logical")


def _rules(a):
    off = {int(x) for x in (a.disable or "").split(",") if x.strip()}
    return ALL_RULES - off


def cmd_analyze(a):
    with Database(a.db, a.frames) as db:
        tables = [a.table] if a.table else db.catalog.list_tables()
        for t in tables:
            ts = analyze_table(db, t, a.buckets, a.frames)
            print(f"analyzed {t}: {ts.n_tuples} tuples, {ts.n_pages} pages, width {ts.width:.1f}, "
                  + ", ".join(f"{c.name}: ndv {c.ndv}" for c in ts.cols.values()))


def cmd_plan(a):
    with Database(a.db, a.frames) as db:
        d, info = optimize(db, _logical(a), a.frames, _rules(a))
    print(json.dumps(d) if a.json else json.dumps(d, indent=2))


def cmd_explain(a):
    node = _logical(a)
    with Database(a.db, a.frames) as db:
        d, info = optimize(db, node, a.frames, _rules(a))
    if a.trees:
        print("logical plan before rewriting:")
        print(render_tree(node))
        print(f"\nafter rewriting (rules fired: {info['fired'] or 'none'}):")
        print(render_tree(info["rewritten"]))
        print("\nphysical plan:")
    print("\n".join(explain_lines(d)))
    print(f"total est_cost = {info['total_cost']:.0f} page I/Os, est_rows = {info['est_rows']}")


def cmd_dp(a):
    with Database(a.db, a.frames) as db:
        d, info = optimize(db, _logical(a), a.frames, _rules(a))
    if not info["dp_tables"]:
        print("(no join in this plan, so there is no join order to enumerate)")
    for t in info["dp_tables"]:
        print("\n".join(dp_table_lines(t)))
        print()


def build_parser():
    ap = argparse.ArgumentParser(prog="opt", description="Module 4: query optimizer")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, logical=True):
        p.add_argument("--db", "--catalog", dest="db", required=True)
        p.add_argument("--frames", type=int, default=32)
        if logical:
            p.add_argument("--logical", required=True)
            p.add_argument("--disable", help="comma separated rule numbers to switch off (1..7)")
    p = sub.add_parser("analyze"); common(p, False)
    p.add_argument("--table"); p.add_argument("--buckets", type=int, default=20)
    p.set_defaults(fn=cmd_analyze)
    p = sub.add_parser("plan"); common(p); p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_plan)
    p = sub.add_parser("explain"); common(p); p.add_argument("--trees", action="store_true"); p.set_defaults(fn=cmd_explain)
    p = sub.add_parser("dp-table"); common(p); p.set_defaults(fn=cmd_dp)
    return ap


def main(argv=None):
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    a = build_parser().parse_args(argv)
    try:
        a.fn(a)
        return 0
    except DBError as e:
        print(format_error(e), file=sys.stderr)
        return 1
    except OSError as e:
        print(f"error: storage: {e.strerror}: {getattr(e, 'filename', '')}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"error: internal: {type(e).__name__}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
