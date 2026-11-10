"""Reference evaluator: runs a plan tree on a provider, with SQL (bag) semantics.

This is the "logical layer" of the system. It is deliberately simple (no page budget, no cost);
Module 2 does the real execution, and this module is the oracle its answers are checked against.
It accepts physical op names too (seqscan = scan, filter = select, *_join = join).
"""
import itertools

from ..errors import DBError
from ..schema import Column, Schema, INT, VARCHAR, REAL, NUMERIC
from ..tuples import tuple_key, dedupe
from .condition import compile_condition, resolve
from .nodes import attr_str

ALIASES = {"seqscan": "scan", "filter": "select", "nested_loop_join": "join",
           "sort_merge_join": "join", "hash_join": "join"}


class RowSet:
    def __init__(self, schema, rows):
        self.schema = schema
        self.rows = rows

    def to_list(self):
        return list(self.rows)


def evaluate(node, provider):
    op = ALIASES.get(node.op, node.op)
    fn = _OPS.get(op)
    if fn is None:
        raise DBError("plan", f"Node '{node.op}': not supported by the evaluator")
    return fn(node, provider)


def _scan(n, prov):
    if not prov.has_table(n.table):
        raise DBError("name", f"unknown table '{n.table}'", n.pos)
    alias = getattr(n, "alias", None) or n.table                 # seqscan may carry an alias (self joins)
    return RowSet(prov.table_schema(n.table).qualified(alias), prov.table_rows(n.table))


def _select(n, prov):
    rs = evaluate(n.child, prov)
    pred = compile_condition(n.cond, rs.schema)
    return RowSet(rs.schema, (r for r in rs.rows if pred(r, None)))


def _project(n, prov):
    rs = evaluate(n.child, prov)
    idx = [resolve(rs.schema, a) for a in n.columns]
    aliases = getattr(n, "aliases", None) or [None] * len(idx)
    out = Schema([Column(al, rs.schema[i].type, rs.schema[i].length) if al else rs.schema[i]
                  for i, al in zip(idx, aliases)])
    dup = out.first_duplicate()
    if dup is not None:
        raise DBError("schema", f"project would produce two columns named '{dup.qname}'", n.pos)
    return RowSet(out, (tuple(r[i] for i in idx) for r in rs.rows))


def _rename(n, prov):
    rs = evaluate(n.child, prov)
    out = rs.schema.qualified(n.to)
    dup = out.first_duplicate()
    if dup is not None:
        raise DBError("schema", f"rename[{n.to}] would give two columns the name '{dup.name}'", n.pos)
    return RowSet(out, rs.rows)


def _product_schema(n, left, right):
    out = left.schema.concat(right.schema)
    dup = out.first_duplicate()
    if dup is not None:
        raise DBError("schema", f"both inputs have a column '{dup.qname}', so the qualified names collide; "
                      f"use rename on one side", n.pos)
    return out


def _join(n, prov):
    left, right = evaluate(n.left, prov), evaluate(n.right, prov)
    schema = _product_schema(n, left, right)
    pred = compile_condition(n.cond, schema, len(left.schema))
    rrows = list(right.rows)
    return RowSet(schema, (a + b for a in left.rows for b in rrows if pred(a, b)))


def _cross(n, prov):
    left, right = evaluate(n.left, prov), evaluate(n.right, prov)
    schema = _product_schema(n, left, right)
    rrows = list(right.rows)
    return RowSet(schema, (a + b for a in left.rows for b in rrows))


def _distinct(n, prov):
    rs = evaluate(n.child, prov)
    return RowSet(rs.schema, dedupe(rs.rows))


def _sort(n, prov):
    rs = evaluate(n.child, prov)
    keys = [(resolve(rs.schema, a), d) for a, d in n.keys]
    rows = list(rs.rows)
    for i, d in reversed(keys):                     # stable multi-key sort, last key first
        rows.sort(key=lambda r, i=i: r[i], reverse=(d == "desc"))
    return RowSet(rs.schema, iter(rows))


def _limit(n, prov):
    rs = evaluate(n.child, prov)
    return RowSet(rs.schema, itertools.islice(rs.rows, n.n))


def _check_setop(n, left, right):
    if len(left.schema) != len(right.schema):
        raise DBError("schema", f"{n.op} needs the same number of columns on both sides "
                      f"({len(left.schema)} vs {len(right.schema)})", n.pos)
    for i, (a, b) in enumerate(zip(left.schema, right.schema)):
        if a.type != b.type:
            raise DBError("schema", f"{n.op}: column {i + 1} is {a.type} on the left but {b.type} on the right", n.pos)


def _setop(n, prov):
    left, right = evaluate(n.left, prov), evaluate(n.right, prov)
    _check_setop(n, left, right)
    if n.op == "union_all":
        return RowSet(left.schema, itertools.chain(left.rows, right.rows))
    if n.op == "union":
        return RowSet(left.schema, dedupe(itertools.chain(left.rows, right.rows)))
    rkeys = {tuple_key(r) for r in right.rows}
    if n.op == "intersect":
        return RowSet(left.schema, dedupe(r for r in left.rows if tuple_key(r) in rkeys))
    return RowSet(left.schema, dedupe(r for r in left.rows if tuple_key(r) not in rkeys))


def agg_name(func, arg):
    return f"{func}_star" if arg == "*" else f"{func}_{attr_str(arg).replace('.', '_')}"


def _group_by(n, prov):
    rs = evaluate(n.child, prov)
    kidx = [resolve(rs.schema, a) for a in n.keys]
    specs, out_cols = [], [rs.schema[i] for i in kidx]
    for func, arg, alias in n.aggregates:
        if arg == "*":
            idx, t = None, INT
        else:
            idx = resolve(rs.schema, arg)
            t = rs.schema[idx].type
            if func in ("sum", "avg") and t != INT:
                raise DBError("type", f"{func.upper()} needs an INT column, but {attr_str(arg)} is {t}", n.pos)
        rtype = INT if func in ("count", "sum") else (REAL if func == "avg" else t)
        specs.append((func, idx))
        out_cols.append(Column(alias or agg_name(func, arg), rtype))
    out_schema = Schema(out_cols)

    def finish(state):
        res = []
        for (func, _), st in zip(specs, state):
            if func == "count":
                res.append(st[0])
            elif func == "sum":
                res.append(st[0])
            elif func == "avg":
                res.append(st[0] / st[1])
            else:
                res.append(st[0])
        return res

    def run():
        groups = {}
        for r in rs.rows:
            k = tuple_key(tuple(r[i] for i in kidx))
            g = groups.get(k)
            if g is None:
                g = groups[k] = [tuple(r[i] for i in kidx), [None] * len(specs)]
                for j, (func, _) in enumerate(specs):
                    g[1][j] = [0, 0] if func in ("count", "sum", "avg") else [None, 0]
            for j, (func, idx) in enumerate(specs):
                st = g[1][j]
                v = None if idx is None else r[idx]
                if func == "count":
                    st[0] += 1
                elif func in ("sum", "avg"):
                    st[0] += v
                    st[1] += 1
                elif func == "min":
                    st[0] = v if st[0] is None or v < st[0] else st[0]
                else:
                    st[0] = v if st[0] is None or v > st[0] else st[0]
        if not groups and not kidx:
            for func, _ in specs:
                if func != "count":
                    raise DBError("aggregate", f"{func.upper()} of an empty input is undefined", n.pos)
            yield tuple(0 for _ in specs)
            return
        for key, state in groups.values():
            yield key + tuple(finish(state))

    return RowSet(out_schema, run())


_OPS = {"scan": _scan, "select": _select, "project": _project, "rename": _rename, "join": _join,
        "cross": _cross, "distinct": _distinct, "sort": _sort, "limit": _limit,
        "union": _setop, "union_all": _setop, "intersect": _setop, "except": _setop,
        "group_by": _group_by}
