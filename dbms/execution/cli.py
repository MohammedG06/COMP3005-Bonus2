"""`exec` command line (spec section 4):  exec run | exec trace"""
import argparse
import json
import sys

from ..errors import DBError, format_error
from .runner import execute


def _fmt_row(row, schema):
    out = []
    for v, col in zip(row, schema):
        out.append(round(v, 4) if isinstance(v, float) else v)
    return out


def _result_json(res):
    return {"schema": res["schema"], "rows": [_fmt_row(r, res["schema"]) for r in res["rows"]],
            "totals": res["totals"], "frames": res["frames"], "peak_frames": res["peak_frames"],
            "nodes": res["nodes"]}


def _trace_lines(res):
    lines = []
    for n in res["nodes"]:
        left = ("  " * n["depth"] + n["label"]).ljust(24)
        lines.append(f"{left} tuples_out={n['tuples_out']:<7} comparisons={n['comparisons']:<9} "
                     f"reads={n['reads']:<6} writes={n['writes']}")
    return lines


def cmd_run(a):
    res = execute(a.db, open(a.plan, encoding="utf-8").read(), a.frames, a.policy)
    if a.json:
        print(json.dumps(_result_json(res)))
    else:
        print(" | ".join(c[0] for c in res["schema"]))
        for r in res["rows"][:a.limit]:
            print(" | ".join(f"{v:.4f}" if isinstance(v, float) else str(v) for v in r))
        print(f"({len(res['rows'])} rows)  {res['totals']}  peak frames held: {res['peak_frames']} of {a.frames}")


def cmd_trace(a):
    res = execute(a.db, open(a.plan, encoding="utf-8").read(), a.frames, a.policy)
    if a.json:
        print(json.dumps({"nodes": res["nodes"], "totals": res["totals"], "peak_frames": res["peak_frames"]}))
        return
    print("\n".join(_trace_lines(res)))
    print("(reads/writes of a node include its children; own_reads/own_writes are in --json)")
    for n in res["nodes"]:
        for note in n["notes"]:
            print(f"  note [{n['label']}]: {note}")
        if n["info"].get("overflow"):
            print(f"  overflow [{n['label']}]: {n['info']['overflow']}")


def build_parser():
    ap = argparse.ArgumentParser(prog="exec", description="Module 2: execution engine")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("run", cmd_run), ("trace", cmd_trace)):
        p = sub.add_parser(name)
        p.add_argument("--db", required=True)
        p.add_argument("--plan", required=True)
        p.add_argument("--frames", type=int, default=32)
        p.add_argument("--policy", choices=["lru", "clock"], default="lru")
        p.add_argument("--json", action="store_true")
        p.add_argument("--limit", type=int, default=50)
        p.set_defaults(fn=fn)
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
    except OSError as e:
        print(f"error: storage: {e.strerror}: {getattr(e, 'filename', '')}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"error: internal: {type(e).__name__}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
