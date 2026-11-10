"""Statistics (spec M4 section 1): exact, no sampling, computed by ANALYZE and stored in the catalog.

  n_tuples, n_pages (data pages), width (average record bytes)
  per column: ndv, min, max, average width, equi-depth histogram (k buckets of lo, hi, count, ndv)

ANALYZE sorts every column with the Module 2 external sort, so it never holds a column in memory.
"""
import math

from ..execution.context import ExecContext
from ..execution.planner import build_operator
from ..ra.plan_json import from_dict
from ..schema import INT, VARCHAR


class Bucket:
    def __init__(self, lo, hi, count, ndv):
        self.lo, self.hi, self.count, self.ndv = lo, hi, count, ndv

    def __repr__(self):
        return f"[{self.lo}..{self.hi}: {self.count} rows, {self.ndv} distinct]"


class ColStats:
    def __init__(self, name, type_, ndv, lo, hi, avg_width, buckets):
        self.name, self.type, self.ndv, self.min, self.max = name, type_, ndv, lo, hi
        self.avg_width, self.buckets = avg_width, buckets


class TableStats:
    def __init__(self, table, n_tuples, n_pages, width, cols, source="analyzed"):
        self.table, self.n_tuples, self.n_pages, self.width = table, n_tuples, n_pages, width
        self.cols, self.source = cols, source


def default_stats(info):
    """No ANALYZE yet: documented defaults. Tuples and pages come from the catalog counters, a column
    is assumed to have 10% distinct values (at least 1), no min/max, no histogram, and a VARCHAR(n) is
    assumed to average n/2 + 2 bytes."""
    n = info.num_tuples
    cols, width = {}, 0.0
    for c in info.columns:
        w = 4.0 if c.type == INT else 2 + (c.length or 1) / 2
        width += w
        cols[c.name] = ColStats(c.name, c.type, max(1, n // 10), None, None, w, [])
    return TableStats(info.name, n, info.num_pages, width, cols, source="default")


def _build_buckets(sorted_stream, n, k):
    """Equi-depth: cut after every ceil(n/k) tuples, but never split a run of equal values."""
    target = max(1, math.ceil(n / k))
    buckets, cur, ndv, first, last = [], None, 0, None, None
    run_val, run_n = None, 0
    runs = 0

    def close_run():
        nonlocal cur
        if run_n == 0:
            return
        if cur is None:
            cur = Bucket(run_val, run_val, 0, 0)
        cur.hi = run_val
        cur.count += run_n
        cur.ndv += 1
        if cur.count >= target:
            buckets.append(cur)
            cur = None

    for (v,) in sorted_stream:
        if run_n and v == run_val:
            run_n += 1
            continue
        close_run()
        if run_n:
            runs += 1
        run_val, run_n = v, 1
        if first is None:
            first = v
    if run_n:
        close_run()
        runs += 1
        last = run_val
    if cur is not None:
        buckets.append(cur)
    return buckets, runs, first, last


def analyze_table(db, table, buckets=20, frames=16):
    """Scan the table once for width, then external-sort each column for ndv / min / max / histogram."""
    info = db.catalog.get_table(table)
    heap, _ = db.open_heap(table)
    ctx = ExecContext(db, frames)
    try:
        # pass 1: tuple count and average widths
        root = build_operator(from_dict({"op": "seqscan", "table": table}, "physical"), ctx)
        root.open()
        n, wsum = 0, [0] * len(info.columns)
        while True:
            t = root.next()
            if t is None:
                break
            n += 1
            for i, c in enumerate(info.columns):
                wsum[i] += 4 if c.type == INT else 2 + len(t[i].encode("utf-8"))
        root.close()
        n_pages = heap.count_data_pages()
        cols = {}
        for i, c in enumerate(info.columns):
            avg_w = (wsum[i] / n) if n else (4.0 if c.type == INT else 2 + c.length / 2)
            if n == 0:
                cols[c.name] = ColStats(c.name, c.type, 0, None, None, avg_w, [])
                continue
            q = f"{table}.{c.name}"
            plan = {"op": "sort", "keys": [[q, "asc"]],
                    "child": {"op": "project", "columns": [q], "child": {"op": "seqscan", "table": table}}}
            root = build_operator(from_dict(plan, "physical"), ctx)
            root.open()

            def stream():
                while True:
                    t = root.next()
                    if t is None:
                        return
                    yield t

            bks, ndv, lo, hi = _build_buckets(stream(), n, buckets)
            root.close()
            cols[c.name] = ColStats(c.name, c.type, ndv, lo, hi, avg_w, bks)
        ts = TableStats(table, n, n_pages, sum(cs.avg_width for cs in cols.values()), cols)
        db.catalog.put_stats(ts)
        return ts
    finally:
        ctx.cleanup()


class StatsProvider:
    """What the optimizer asks for: table stats (analyzed if available, else defaults)."""

    def __init__(self, db):
        self.db = db
        self._cache = {}

    def table(self, name):
        if name not in self._cache:
            ts = self.db.catalog.get_stats(name)
            self._cache[name] = ts if ts is not None else default_stats(self.db.catalog.get_table(name))
        return self._cache[name]

    def columns(self, name):
        return self.db.catalog.get_table(name).columns
