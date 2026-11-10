"""One call that goes from a logical plan to an annotated physical plan (used by the CLI and the shell)."""
from ..ra.plan_json import from_dict
from ..sql.translate import CatalogView
from .planner import Planner
from .rewrite import ALL_RULES
from .stats import StatsProvider


def optimize(db, logical_node, frames, rules=ALL_RULES):
    """-> (physical plan dict with est_rows / est_cost on every node, info dict)."""
    cat = CatalogView.from_database(db)
    planner = Planner(cat, StatsProvider(db), frames, rules)
    p, rewritten, fired = planner.optimize(logical_node)
    return p.d, {"rewritten": rewritten, "fired": sorted(set(fired)), "dp_tables": planner.dp_tables,
                 "total_cost": p.cum, "est_rows": p.est.rows}


def unoptimized(logical_node):
    """The baseline for measurement 2: joins in the order written, one fixed algorithm, no rewrites."""
    from .condutil import and_all
    from ..ra.nodes import Node
    from ..ra.plan_json import to_dict

    def conv(n, algo):
        d = {}
        op = n.op
        if op == "scan":
            return {"op": "seqscan", "table": n.table}
        if op == "rename" and n.child.op == "scan":
            return {"op": "seqscan", "table": n.child.table, "alias": n.to}
        base = to_dict(n)
        for slot in ("child", "left", "right"):
            if hasattr(n, slot):
                base[slot] = conv(getattr(n, slot), algo)
        if op == "select":
            base["op"] = "filter"
        elif op == "join":
            base["op"] = algo
        elif op == "cross":
            base["op"] = "nested_loop_join"
            base["condition"] = "1 = 1"
        elif op in ("group_by", "distinct"):
            base["mode"] = "sort"
        return base
    return lambda algo="nested_loop_join": conv(logical_node, algo)


def _flatten(d, out=None):
    out = [] if out is None else out
    out.append(d)
    for slot in ("child", "left", "right"):
        if slot in d:
            _flatten(d[slot], out)
    return out


def _truncated_ids(d, ids, under_limit=False):
    """Ids of nodes whose actual row count is cut short by a LIMIT above them (a pipelined chain)."""
    if under_limit:
        ids.add(id(d))
    pipelined = ("project", "filter", "rename", "limit", "nested_loop_join", "sort_merge_join", "hash_join")
    for slot in ("child", "left", "right"):
        if slot in d:
            _truncated_ids(d[slot], ids, (under_limit and d["op"] in pipelined) or d["op"] == "limit")
    return ids


def est_vs_actual(phys, result):
    """Pairs every node of an optimizer plan with the executor's counters for it (same preorder).
    -> list of dicts with est_rows, actual_rows and the q-error max(est, actual) / min(est, actual).
    A node that a LIMIT cuts short is flagged truncated: its actual rows say nothing about the estimate."""
    nodes = _flatten(phys)
    cut = _truncated_ids(phys, set())
    out = []
    for d, n in zip(nodes, result["nodes"]):
        est, act = d.get("est_rows", 1), n["tuples_out"]
        e, a = max(1, est), max(1, act)
        trunc = id(d) in cut
        out.append({"op": d["op"], "label": n["label"], "est_rows": est, "actual_rows": act,
                    "q_error": None if trunc else max(e, a) / min(e, a), "truncated": trunc,
                    "est_cost": d.get("est_cost", 0), "reads": n["reads"], "writes": n["writes"], "depth": n["depth"]})
    return out
