"""The whole system behind one object (spec section 6, integration). Wiring only, no new algorithms:

  SQL text -> M3 (lexer, parser, analysis, logical plan) -> M4 (rewrites, statistics, cost model,
  join order, physical plan) -> M2 (iterators under a budget of B frames) -> M1 (buffer pool, heap files)
"""
import time

from .errors import DBError
from .ra.plan_json import to_dict
from .optimizer.api import optimize, est_vs_actual
from .optimizer.planner import explain_lines
from .optimizer.stats import analyze_table
from .execution.runner import execute
from .sql.parser import parse_sql
from .sql.translate import CatalogView, Compiler, check_ddl
from .storage.database import Database
from .storage.loader import load_csv
from .storage.records import parse_schema


class Result:
    def __init__(self, kind, schema=None, rows=None, message=None, stats=None, lines=None):
        self.kind, self.schema, self.rows = kind, schema or [], rows or []
        self.message, self.stats, self.lines = message, stats or {}, lines or []


def split_statements(text):
    """Split at ';' outside quotes and -- comments. Returns [(statement text, offset in text)]."""
    out, buf, i, n, start = [], [], 0, len(text), 0
    in_q = False
    while i < n:
        c = text[i]
        if in_q:
            buf.append(c)
            if c == "'":
                if i + 1 < n and text[i + 1] == "'":
                    buf.append("'")
                    i += 1
                else:
                    in_q = False
        elif c == "'":
            in_q = True
            buf.append(c)
        elif c == "-" and i + 1 < n and text[i + 1] == "-":
            while i < n and text[i] != "\n":
                buf.append(" ")
                i += 1
            continue
        elif c == ";":
            if "".join(buf).strip():
                out.append(("".join(buf), start))
            buf, start = [], i + 1
        else:
            buf.append(c)
        i += 1
    if "".join(buf).strip():
        out.append(("".join(buf), start))
    return out


def _words(stmt):
    return stmt.strip().split()


class Engine:
    def __init__(self, path, frames=32, policy="lru", buckets=20):
        self.path, self.frames, self.policy, self.buckets = path, frames, policy, buckets
        with Database(path, frames, policy, create=True):
            pass                                              # creates catalog.db the first time

    # ---------------------------------------------------------------------------------- entry points
    def run(self, text):
        """Generator: runs the statements of `text` one by one and yields each Result as soon as it is done,
        so a script prints everything before a failing statement. Errors carry a position in `text`."""
        for stmt, off in split_statements(text):
            try:
                yield self.run_one(stmt)
            except DBError as e:
                if e.pos is not None:
                    e.pos += off
                e.statement = text
                raise

    def run_all(self, text):
        return list(self.run(text))

    def run_one(self, stmt):
        w = _words(stmt)
        head = [x.upper() for x in w[:2]]
        if head[:1] == ["EXPLAIN"]:
            analyze = head[1:2] == ["ANALYZE"]
            body = stmt.strip()[len("EXPLAIN"):].lstrip()
            if analyze:
                body = body[len("ANALYZE"):].lstrip()
            return self.explain(body, analyze, shift=stmt.find(body))
        if head[:1] == ["ANALYZE"]:
            return self.analyze(w[1] if len(w) > 1 else None)
        if head[:1] == ["SET"]:
            return self.set_option(stmt)
        if head[:1] == ["LOAD"]:
            return self.load(stmt)
        return self.sql(stmt)

    # ------------------------------------------------------------------------------------- statements
    def sql(self, stmt):
        asts = parse_sql(stmt)
        if len(asts) != 1:
            raise DBError("syntax", "expected exactly one statement")
        st = asts[0]
        with Database(self.path, self.frames, self.policy) as db:
            cat = CatalogView.from_database(db)
            if st.op == "create":
                check_ddl(st, cat)
                db.create_table(st.table, parse_schema(", ".join(
                    f"{n}:{t}" + (f"({l})" if l else "") for n, t, l in st.columns)))
                return Result("ddl", message=f"CREATE TABLE {st.table}")
            if st.op == "insert":
                check_ddl(st, cat)
                heap, _ = db.open_heap(st.table)
                heap.insert(tuple(v.value for v in st.values))
                db.refresh_counts(st.table, 1)
                return Result("dml", message="INSERT 1")
            plan, out_schema = Compiler(cat).compile_query(st)
            phys, info = optimize(db, plan, self.frames)
        t0 = time.perf_counter()
        res = execute(self.path, phys, self.frames, self.policy)
        secs = time.perf_counter() - t0
        pairs = est_vs_actual(phys, res)
        stats = {"rows": len(res["rows"]), "page_reads": res["totals"]["page_reads"],
                 "page_writes": res["totals"]["page_writes"], "seconds": secs, "est_rows_root": pairs[0]["est_rows"],
                 "actual_rows_root": pairs[0]["actual_rows"], "est_cost": info["total_cost"],
                 "peak_frames": res["peak_frames"], "nodes": pairs, "comparisons": res["totals"]["comparisons"]}
        return Result("select", res["schema"], res["rows"], stats=stats)

    def explain(self, body, analyze, shift=0):
        try:
            return self._explain(body, analyze)
        except DBError as e:
            if e.pos is not None:
                e.pos += shift
            raise

    def _explain(self, body, analyze):
        asts = parse_sql(body)
        if len(asts) != 1 or asts[0].op in ("create", "insert"):
            raise DBError("syntax", "EXPLAIN needs one SELECT query")
        with Database(self.path, self.frames, self.policy) as db:
            cat = CatalogView.from_database(db)
            plan, _ = Compiler(cat).compile_query(asts[0])
            phys, info = optimize(db, plan, self.frames)
        lines = explain_lines(phys)
        lines.append(f"total est_cost = {info['total_cost']:.0f} page I/Os")
        if not analyze:
            return Result("explain", lines=lines)
        res = execute(self.path, phys, self.frames, self.policy)
        pairs = est_vs_actual(phys, res)
        lines = [f"{'  ' * p['depth']}{p['label']:<{44 - 2 * p['depth']}} est_rows={p['est_rows']:<8} actual_rows={p['actual_rows']:<8} "
                 + (f"q-error={p['q_error']:<7.2f}" if p['q_error'] is not None else "(cut short by LIMIT) ")
                 + f" reads={p['reads']:<6} writes={p['writes']}" for p in pairs]
        t = res["totals"]
        lines.append(f"total: {t['page_reads']} page reads, {t['page_writes']} page writes, "
                     f"estimated {info['total_cost']:.0f}; peak frames held {res['peak_frames']} of {self.frames}")
        return Result("explain", lines=lines, stats={"nodes": pairs})

    def analyze(self, table):
        with Database(self.path, self.frames, self.policy) as db:
            names = [table] if table else db.catalog.list_tables()
            for t in names:
                db.catalog.get_table(t)
            lines = []
            for t in names:
                ts = analyze_table(db, t, self.buckets, max(3, min(self.frames, 32)))
                lines.append(f"ANALYZE {t}: {ts.n_tuples} tuples, {ts.n_pages} pages")
        return Result("ddl", message="\n".join(lines))

    def set_option(self, stmt):
        body = stmt.strip()[3:].strip().rstrip(";")
        if "=" not in body:
            raise DBError("syntax", "expected SET frames = <number>")
        name, val = (x.strip() for x in body.split("=", 1))
        if name.lower() == "frames":
            if not val.isdigit() or int(val) < 3:
                raise DBError("syntax", "frames must be a whole number of at least 3")
            self.frames = int(val)
            return Result("set", message=f"frames = {self.frames}")
        if name.lower() == "buckets":
            if not val.isdigit() or int(val) < 1:
                raise DBError("syntax", "buckets must be a positive whole number")
            self.buckets = int(val)
            return Result("set", message=f"buckets = {self.buckets}")
        raise DBError("syntax", f"unknown option '{name}' (use frames or buckets)")

    def load(self, stmt):
        w = stmt.strip().rstrip(";").split(None, 3)
        if len(w) < 4 or w[2].upper() != "FROM":
            raise DBError("syntax", "expected LOAD <table> FROM '<file>'")
        path = w[3].strip()
        if len(path) >= 2 and path[0] == "'" and path[-1] == "'":
            path = path[1:-1]
        with Database(self.path, max(self.frames, 8), self.policy) as db:
            n = load_csv(db, w[1], path)
        return Result("dml", message=f"LOAD {n}")


# ---------------------------------------------------------------------------------------- printing
def format_result(r, limit=100):
    if r.kind != "select":
        return "\n".join(r.lines) if r.lines else (r.message or "")
    base = [c[0].split(".", 1)[-1] for c in r.schema]
    names = [b if base.count(b) == 1 else c[0] for b, c in zip(base, r.schema)]     # qualify only when ambiguous
    shown = r.rows[:limit]
    cells = [[(f"{v:.4f}" if isinstance(v, float) else str(v)) for v in row] for row in shown]
    w = [max([len(n)] + [len(c[i]) for c in cells]) for i, n in enumerate(names)]
    lines = [" | ".join(n.ljust(x) for n, x in zip(names, w)).rstrip(), "-+-".join("-" * x for x in w)]
    lines += [" | ".join(v.ljust(x) for v, x in zip(c, w)).rstrip() for c in cells]
    lines.append(f"({len(r.rows)} row{'s' if len(r.rows) != 1 else ''})")
    if len(r.rows) > limit:
        lines.append(f"... {len(r.rows) - limit} more rows not shown")
    return "\n".join(lines)
