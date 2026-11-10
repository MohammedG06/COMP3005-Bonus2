"""Cardinality estimation (spec M4 section 2). Formulas, all under the stated assumptions:

  uniformity inside a histogram bucket, independence between predicates, containment for joins.

  col = v           bucket containing v: count / (bucket ndv * n);  v outside min..max: 1/n;  no histogram: 1/ndv
  col <  / <= / > / >=   whole buckets below v + a fraction of the one containing v (integers: linear);
                    no histogram: (v - min) / (max - min)
  col <> v          1 - sel(=)
  p AND q           sel(p) sel(q)                    p OR q   s(p) + s(q) - s(p) s(q)          NOT p   1 - s(p)
  col = col         1 / max(ndv_left, ndv_right)     any other col-to-col predicate   0.1
  output rows = ceil(input rows * selectivity), never below 1
Unknown columns (aggregate outputs) get the defaults: = is 1/10, a range is 1/3.
"""
import math

from ..ra.nodes import Node
from ..schema import INT
from .condutil import ColsOf, refs

PAGE = 4096
DEFAULT_RANGE = 1 / 3
DEFAULT_COLCOL = 0.1


class CI:
    __slots__ = ("ndv", "cs", "width")

    def __init__(self, ndv, cs, width):
        self.ndv, self.cs, self.width = max(1, ndv), cs, width


class Est:
    def __init__(self, rows, cols, pages=None):
        self.rows = max(1, int(rows))
        self.cols = cols                                 # {(qual, name): CI}
        self.pages_fixed = pages                         # a base table keeps its real page count

    @property
    def width(self):
        return sum(ci.width for ci in self.cols.values())

    @property
    def pages(self):
        if self.pages_fixed is not None:
            return self.pages_fixed
        return max(1, math.ceil(self.rows * self.width / PAGE))


def _cap(cols, rows):
    return {k: CI(min(ci.ndv, rows), ci.cs, ci.width) for k, ci in cols.items()}


# ================================================================================ selectivity
def _total(cs):
    return sum(b.count for b in cs.buckets) or 1


def sel_eq(ci, v):
    cs = ci.cs if ci else None
    if cs is not None and cs.buckets:
        tot = _total(cs)
        for b in cs.buckets:
            if b.lo <= v <= b.hi:
                return b.count / (max(1, b.ndv) * tot)
        return 1.0 / tot                                  # between buckets or outside min..max
    return 1.0 / (ci.ndv if ci else 10)


def sel_le(ci, v):
    """P(X <= v)."""
    cs = ci.cs if ci else None
    if cs is not None and cs.buckets:
        tot, acc = _total(cs), 0.0
        for b in cs.buckets:
            if b.hi <= v:
                acc += b.count
            elif b.lo > v:
                break
            else:
                if isinstance(v, int) and isinstance(b.lo, int) and b.hi > b.lo:
                    frac = (v - b.lo + 1) / (b.hi - b.lo + 1)
                else:
                    frac = 0.5
                acc += b.count * frac
                break
        return min(1.0, acc / tot)
    if cs is not None and cs.min is not None and isinstance(v, int) and isinstance(cs.min, int):
        if cs.max == cs.min:
            return 1.0 if v >= cs.min else 0.0
        return min(1.0, max(0.0, (v - cs.min) / (cs.max - cs.min)))
    return DEFAULT_RANGE


def sel_attr_lit(ci, op, v):
    has_hist = ci is not None and ci.cs is not None and bool(ci.cs.buckets)
    eq = sel_eq(ci, v)
    if op == "=":
        return eq
    if op == "<>":
        return max(0.0, 1.0 - eq)
    le = sel_le(ci, v)
    if op == "<=":
        return le
    if op == "<":
        return max(0.0, le - eq) if has_hist else le
    if op == ">":
        return max(0.0, 1.0 - le)
    return max(0.0, 1.0 - (max(0.0, le - eq) if has_hist else le))      # >=


FLIP = {"<": ">", ">": "<", "<=": ">=", ">=": "<=", "=": "=", "<>": "<>"}


def cond_sel(c, cols):
    """Selectivity of a condition over columns `cols` ({(qual, name): CI})."""
    if c.op == "and":
        return cond_sel(c.left, cols) * cond_sel(c.right, cols)
    if c.op == "or":
        a, b = cond_sel(c.left, cols), cond_sel(c.right, cols)
        return a + b - a * b
    if c.op == "not":
        return 1.0 - cond_sel(c.child, cols)
    l, r, op = c.left, c.right, c.cmp
    if l.op != "attr" and r.op != "attr":
        a, b = l.value, r.value
        ok = {"=": a == b, "<>": a != b, "<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op]
        return 1.0 if ok else 0.0
    if l.op == "attr" and r.op == "attr":
        cl, cr = cols.get((l.qual, l.name)), cols.get((r.qual, r.name))
        if op == "=":
            return 1.0 / max(cl.ndv if cl else 10, cr.ndv if cr else 10)
        if op == "<>":
            return 1.0 - 1.0 / max(cl.ndv if cl else 10, cr.ndv if cr else 10)
        return DEFAULT_COLCOL
    if l.op != "attr":                                   # literal on the left: flip
        l, r, op = r, l, FLIP[op]
    return sel_attr_lit(cols.get((l.qual, l.name)), op, r.value)


# ==================================================================================== estimator
class Estimator:
    """Estimates every logical node. `stats` must provide table(name) -> TableStats and columns(name)."""

    def __init__(self, stats, catalog):
        self.stats = stats
        self.cols_of = ColsOf(catalog)
        self._memo = {}

    def scan_est(self, table, qual):
        ts = self.stats.table(table)
        cols = {}
        for c in self.stats.columns(table):
            cs = ts.cols.get(c.name)
            cols[(qual, c.name)] = CI(cs.ndv if cs and cs.ndv else max(1, ts.n_tuples // 10), cs,
                                      cs.avg_width if cs else (4.0 if c.type == INT else 2 + (c.length or 1) / 2))
        return Est(max(1, ts.n_tuples), cols, pages=max(1, ts.n_pages))

    def est(self, n):
        key = id(n)
        hit = self._memo.get(key)
        if hit is None or hit[0] is not n:
            # the node is stored next to its estimate so it stays alive: Python reuses the id of a
            # garbage collected object, which would hand a new plan a stale estimate
            hit = self._memo[key] = (n, self._est(n))
        return hit[1]

    def join_rows(self, left, right, atoms):
        """Rows of left JOIN right under the atoms, and the merged columns (independence: product)."""
        cols = dict(left.cols)
        cols.update(right.cols)
        rows = left.rows * right.rows
        for a in atoms:
            rows *= cond_sel(a, cols)
        rows = max(1, math.ceil(rows))
        return Est(rows, _cap(cols, rows))

    def _est(self, n):
        op = n.op
        if op == "scan":
            return self.scan_est(n.table, n.table)
        if op == "rename":
            c = self.est(n.child)
            return Est(c.rows, {(n.to, k[1]): v for k, v in c.cols.items()}, pages=c.pages_fixed)
        if op == "select":
            c = self.est(n.child)
            rows = max(1, math.ceil(c.rows * cond_sel(n.cond, c.cols)))
            return Est(rows, _cap(c.cols, rows))
        if op == "project":
            c = self.est(n.child)
            al = getattr(n, "aliases", None) or [None] * len(n.columns)
            cols = {}
            for a, name in zip(n.columns, al):
                ci = c.cols.get((a.qual, a.name)) or CI(10, None, 8.0)
                cols[(None, name) if name else (a.qual, a.name)] = ci
            return Est(c.rows, cols)
        if op in ("join", "cross"):
            from .condutil import conjuncts
            return self.join_rows(self.est(n.left), self.est(n.right), conjuncts(n.cond) if op == "join" else [])
        if op == "group_by":
            c = self.est(n.child)
            groups = 1
            for k in n.keys:
                groups *= c.cols.get((k.qual, k.name), CI(10, None, 8)).ndv
            rows = 1 if not n.keys else max(1, min(c.rows, groups))
            cols = {(k.qual, k.name): CI(min(c.cols.get((k.qual, k.name), CI(10, None, 8)).ndv, rows),
                                         c.cols.get((k.qual, k.name), CI(10, None, 8)).cs, c.cols.get((k.qual, k.name), CI(10, None, 8)).width)
                    for k in n.keys}
            for _, _, name in n.aggregates:
                cols[(None, name)] = CI(rows, None, 8.0)
            return Est(rows, cols)
        if op == "distinct":
            c = self.est(n.child)
            d = 1
            for ci in c.cols.values():
                d *= ci.ndv
                if d >= c.rows:
                    break
            rows = max(1, min(c.rows, d))
            return Est(rows, _cap(c.cols, rows))
        if op in ("sort",):
            return self.est(n.child)
        if op == "limit":
            c = self.est(n.child)
            rows = max(1, min(c.rows, n.n))
            return Est(rows, _cap(c.cols, rows))
        l, r = self.est(n.left), self.est(n.right)
        if op in ("union", "union_all"):
            rows = l.rows + r.rows
        elif op == "intersect":
            rows = min(l.rows, r.rows)
        else:
            rows = l.rows
        return Est(rows, _cap(l.cols, rows))
