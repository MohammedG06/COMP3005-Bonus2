#!/usr/bin/env python3
"""The database shell: SQL goes in, rows come out.

  python shell.py --db mydb                       interactive prompt, statements end with ;
  python shell.py --db mydb -f script.sql         run a file
  python shell.py --db mydb -c "SELECT ..."       run one command

Besides the SQL subset:  EXPLAIN <query>   EXPLAIN ANALYZE <query>   ANALYZE [table]
                         SET frames = n    LOAD <table> FROM 'file.csv'
Interactive mode: \\q quits, and every query prints its page I/O, time and estimated against actual rows."""
import argparse
import sys

from dbms.errors import DBError, format_error
from dbms.system import Engine, format_result


def show_error(e, text):
    print(format_error(e, getattr(e, "statement", text)), file=sys.stderr)


def run_text(eng, text, limit, stats):
    ok = True
    try:
        for r in eng.run(text):                    # a generator: each result is printed as soon as it exists
            out = format_result(r, limit)
            if out:
                print(out)
            if stats and r.kind == "select":
                s = r.stats
                print(f"[rows={s['rows']} page_reads={s['page_reads']} page_writes={s['page_writes']} "
                      f"time={s['seconds']:.4f}s est_rows={s['est_rows_root']} actual_rows={s['actual_rows_root']} "
                      f"peak_frames={s['peak_frames']}/{eng.frames}]")
    except DBError as e:
        show_error(e, text)
        ok = False
    except OSError as e:
        print(f"error: storage: {e.strerror}: {getattr(e, 'filename', '')}", file=sys.stderr)
        ok = False
    except Exception as e:
        print(f"error: internal: {type(e).__name__}: {e}", file=sys.stderr)
        ok = False
    return ok


def main(argv=None):
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="COMP 3005 database shell")
    ap.add_argument("--db", required=True)
    ap.add_argument("--frames", type=int, default=32)
    ap.add_argument("--policy", choices=["lru", "clock"], default="lru")
    ap.add_argument("-c", "--command")
    ap.add_argument("-f", "--file")
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--stats", action="store_true", help="print page I/O and timing after every query")
    a = ap.parse_args(argv)
    eng = Engine(a.db, a.frames, a.policy)
    if a.command is not None:
        return 0 if run_text(eng, a.command, a.limit, a.stats) else 1
    if a.file:
        with open(a.file, encoding="utf-8") as fh:
            return 0 if run_text(eng, fh.read(), a.limit, a.stats) else 1
    print(f"COMP 3005 database, {a.db}, {eng.frames} frames. End statements with ';'  (\\q to quit)")
    buf = []
    while True:
        try:
            line = input("dbms> " if not buf else "  ... ")
        except EOFError:
            print()
            return 0
        if not buf and line.strip() in ("\\q", "quit", "exit"):
            return 0
        buf.append(line)
        if ";" in line:
            run_text(eng, "\n".join(buf), a.limit, True)
            buf = []


if __name__ == "__main__":
    sys.exit(main())
