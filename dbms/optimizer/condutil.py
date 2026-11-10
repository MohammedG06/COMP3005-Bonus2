"""Helpers over condition trees (dbms.ra.nodes.Node with ops cmp / and / or / not / attr / num / str)."""
import copy

from ..ra.nodes import Node


def conjuncts(c):
    if c.op == "and":
        return conjuncts(c.left) + conjuncts(c.right)
    return [c]


def and_all(parts):
    out = parts[0]
    for p in parts[1:]:
        out = Node("and", None, left=out, right=p)
    return out


def refs(c):
    """Set of (qualifier, name) of every column a condition mentions."""
    if c.op == "attr":
        return {(c.qual, c.name)}
    if c.op == "cmp" or c.op in ("and", "or"):
        return refs(c.left) | refs(c.right)
    if c.op == "not":
        return refs(c.child)
    return set()


def is_col_eq(c):
    return c.op == "cmp" and c.cmp == "=" and c.left.op == "attr" and c.right.op == "attr"


def map_cols(c, fn):
    """Copy of the condition with every attr replaced by fn((qual, name)) -> (qual, name)."""
    if c.op == "attr":
        q, n = fn((c.qual, c.name))
        return Node("attr", c.pos, qual=q, name=n)
    if c.op == "cmp":
        return Node("cmp", c.pos, cmp=c.cmp, left=map_cols(c.left, fn), right=map_cols(c.right, fn))
    if c.op in ("and", "or"):
        return Node(c.op, c.pos, left=map_cols(c.left, fn), right=map_cols(c.right, fn))
    if c.op == "not":
        return Node("not", c.pos, child=map_cols(c.child, fn))
    return c


def clone(node, **changes):
    n = copy.copy(node)
    for k, v in changes.items():
        setattr(n, k, v)
    return n


def attr(qual, name):
    return Node("attr", None, qual=qual, name=name)


class ColsOf:
    """Output columns, as (qualifier, name) pairs, of a logical plan node."""

    def __init__(self, catalog):
        self.cat = catalog

    def __call__(self, n):
        op = n.op
        if op == "scan":
            return [(n.table, c.name) for c in self.cat.columns(n.table)]
        if op == "rename":
            return [(n.to, name) for _, name in self(n.child)]
        if op in ("select", "sort", "limit", "distinct"):
            return self(n.child)
        if op == "project":
            al = getattr(n, "aliases", None) or [None] * len(n.columns)
            return [(None, a) if a else (c.qual, c.name) for c, a in zip(n.columns, al)]
        if op == "group_by":
            return [(k.qual, k.name) for k in n.keys] + [(None, name) for _, _, name in n.aggregates]
        if op in ("join", "cross"):
            return self(n.left) + self(n.right)
        return self(n.left)                      # set operators: the left input names the columns
