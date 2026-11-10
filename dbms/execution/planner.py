"""Turns a validated physical plan (Node tree) into operator objects. All name, type and schema
errors are raised here, from the constructors, BEFORE any operator is opened, so a bad plan does
no I/O at all (spec section 4)."""
from ..errors import DBError
from .joins import NestedLoopJoin, SortMergeJoin, HashJoin, SetOp
from .operators import (SeqScan, Filter, Project, Rename, Limit, UnionAll, Sort, build_group_by)


def build_operator(node, ctx):
    op = node.op
    kids = [build_operator(c, ctx) for c in node.children()]
    if op == "seqscan":
        return SeqScan(ctx, node)
    if op == "filter":
        return Filter(ctx, node, kids[0])
    if op == "project":
        return Project(ctx, node, kids[0])
    if op == "rename":
        return Rename(ctx, node, kids[0])
    if op == "limit":
        return Limit(ctx, node, kids[0])
    if op == "union_all":
        return UnionAll(ctx, node, kids[0], kids[1])
    if op == "sort":
        return Sort(ctx, node, kids[0])
    if op == "distinct":
        return build_group_by(ctx, node, kids, getattr(node, "mode", "sort"), distinct=True)
    if op == "group_by":
        return build_group_by(ctx, node, kids, getattr(node, "mode", "sort"), distinct=False)
    if op == "union":                       # set union = concatenation + duplicate removal (sort based)
        return build_group_by(ctx, node, kids, "sort", distinct=True)
    if op == "nested_loop_join":
        return NestedLoopJoin(ctx, node, kids[0], kids[1])
    if op == "sort_merge_join":
        return SortMergeJoin(ctx, node, kids[0], kids[1])
    if op == "hash_join":
        return HashJoin(ctx, node, kids[0], kids[1])
    if op in ("intersect", "except"):
        return SetOp(ctx, node, kids[0], kids[1])
    raise DBError("plan", f"Node '{op}': unknown operator '{op}'.")
