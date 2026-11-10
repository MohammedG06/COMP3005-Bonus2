"""Cost based planning (spec M4 section 4): left-deep dynamic programming over join orders, a choice
of join algorithm per join, and physical plan generation (the JSON Module 2 executes).

COST MODEL (page I/O, B frames). M, N = pages of the outer (the intermediate result) and of the inner
(always a base relation, because the trees are left-deep):
  block nested loop  M + ceil(M / (B-2)) * N
  sort-merge         sort(M) + sort(N) + M + N          with  sort(X) = 2 X (passes - 1) + X,
                                                             passes = 1 + ceil(log_{B-1} ceil(X / B))
  hash               3 (M + N)
and the cost of a join is  cost(subplan for the outer) + that formula, where for a single base
relation the outer read is the formula's own M term. Filters, projections and limits cost nothing of
their own. Where a leaf has a pushed-down filter, its scan cost (pages actually read) replaces N in the
nested loop term and the extra scan pages are added to sort-merge and hash (they read more than they keep).
"""
import math

from ..errors import DBError
from ..ra.condition import unparse
from ..ra.nodes import Node, attr_str
from .condutil import conjuncts, and_all, refs, is_col_eq, ColsOf, clone
from .estimate import Estimator, Est, cond_sel
from .rewrite import Rewriter, ALL_RULES

ALGOS = ("hash_join", "sort_merge_join", "nested_loop_join")      # tie-break order
DP_MAX = 10


def passes(M, B):
    r0 = math.ceil(M / B)
    return 1 if r0 <= 1 else 1 + math.ceil(math.log(r0) / math.log(B - 1) - 1e-12)


def sort_total(M, B):
    return 2 * M * (passes(M, B) - 1) + M


def sort_extra(M, B):
    """I/O beyond the one read of the input (which the child already paid)."""
    return 2 * M * (passes(M, B) - 1)


class P:
    """A physical (sub)plan: its JSON dict, its estimate, and the cumulative cost of producing it."""

    def __init__(self, d, est, cum, cond=None, label=None):
        self.d, self.est, self.cum, self.cond, self.label = d, est, cum, cond, label
        self.cols = None        # physical column order when it may differ from the logical one (join blocks)


class State:
    def __init__(self, mask, cost, est, choice):
        self.mask, self.cost, self.est, self.choice = mask, cost, est, choice


class Atom:
    def __init__(self, cond, leaves, allow_eq=True):
        self.cond, self.leaves = cond, frozenset(leaves)
        self.eq = allow_eq and is_col_eq(cond) and len(self.leaves) == 2


class Planner:
    def __init__(self, catalog, stats, frames, rules=ALL_RULES):
        self.cat, self.stats, self.B = catalog, stats, max(3, frames)
        self.rules = frozenset(rules)
        self.est = Estimator(stats, catalog)
        self.cols_of = ColsOf(catalog)
        self.dp_tables = []
        self.override = {}                      # tests: {leaf-mask: (rows, pages)} reproduces the spec's worked example

    # ================================================================================== entry
    def optimize(self, logical):
        rw = Rewriter(self.cat, self.rules)
        rewritten = rw.run(logical)
        self.est = Estimator(self.stats, self.cat)
        p = self.plan(rewritten)
        return p, rewritten, rw.fired

    # ===================================================================================== helpers
    def _finish(self, d, est, cum, children, cond=None):
        own = cum - sum(c.cum for c in children)
        d["est_rows"], d["est_cost"] = est.rows, max(0, int(round(own)))
        return P(d, est, cum, cond)

    def agg_mode(self, child_pages, out_pages):
        B = self.B
        sort_x = sort_extra(child_pages, B)
        hash_x = 0 if out_pages <= B - 2 else 2 * child_pages
        return ("hash", hash_x) if hash_x <= sort_x else ("sort", sort_x)

    # ===================================================================================== recursion
    def plan(self, n):
        """Plan n so that its output columns are in n's logical order (what every parent assumes)."""
        p = self.plan_raw(n)
        want = list(self.cols_of(n))
        if p.cols is not None and list(p.cols) != want:
            pd = {"op": "project", "columns": [f"{q}.{c}" if q else c for q, c in want], "child": p.d,
                  "est_rows": p.est.rows, "est_cost": 0}
            return P(pd, p.est, p.cum)
        p.cols = None
        return p

    def plan_raw(self, n):
        op = n.op
        if op in ("join", "cross") or (op == "select" and self._leads_to_join(n)):
            return self.plan_block(n)
        if op == "scan":
            e = self.est.est(n)
            return self._finish({"op": "seqscan", "table": n.table}, e, e.pages, [])
        if op == "rename":
            if n.child.op == "scan":
                e = self.est.est(n)
                return self._finish({"op": "seqscan", "table": n.child.table, "alias": n.to}, e, e.pages, [])
            c = self.plan(n.child)
            return self._finish({"op": "rename", "to": n.to, "child": c.d}, self.est.est(n), c.cum, [c])
        if op == "select":
            c = self.plan(n.child)
            e = self.est.est(n)
            if 7 in self.rules and c.d["op"] == "filter" and c.cond is not None:
                cond = and_all([n.cond, c.cond])                   # rule 7: adjacent selections become one
                d = {"op": "filter", "condition": unparse(cond), "child": c.d["child"]}
                d["est_rows"], d["est_cost"] = e.rows, 0
                return P(d, e, c.cum, cond)
            d = {"op": "filter", "condition": unparse(n.cond), "child": c.d}
            return self._finish(d, e, c.cum, [c], cond=n.cond)
        if op == "project":
            c = self.plan_raw(n.child)                             # raw: this projection may restore the order itself
            have = list(c.cols) if c.cols is not None else list(self.cols_of(n.child))
            if 7 in self.rules and not getattr(n, "aliases", None) and [(a.qual, a.name) for a in n.columns] == have:
                return c                                           # rule 7: a projection that drops nothing and reorders nothing
            c.cols = None
            d = {"op": "project", "columns": [attr_str(a) for a in n.columns], "child": c.d}
            if getattr(n, "aliases", None):
                d["aliases"] = list(n.aliases)
            return self._finish(d, self.est.est(n), c.cum, [c])
        if op == "group_by":
            c = self.plan(n.child)
            e = self.est.est(n)
            mode, extra = self.agg_mode(c.est.pages, e.pages)
            d = {"op": "group_by", "mode": mode, "keys": [attr_str(k) for k in n.keys],
                 "aggregates": [[f, a if a == "*" else attr_str(a), nm] for f, a, nm in n.aggregates], "child": c.d}
            return self._finish(d, e, c.cum + extra, [c])
        if op == "distinct":
            c = self.plan(n.child)
            e = self.est.est(n)
            mode, extra = self.agg_mode(c.est.pages, e.pages)
            return self._finish({"op": "distinct", "mode": mode, "child": c.d}, e, c.cum + extra, [c])
        if op == "sort":
            c = self.plan(n.child)
            d = {"op": "sort", "keys": [[attr_str(a), dr] for a, dr in n.keys], "child": c.d}
            return self._finish(d, self.est.est(n), c.cum + sort_extra(c.est.pages, self.B), [c])
        if op == "limit":
            c = self.plan(n.child)
            return self._finish({"op": "limit", "n": n.n, "child": c.d}, self.est.est(n), c.cum, [c])
        l, r = self.plan(n.left), self.plan(n.right)
        e = self.est.est(n)
        extra = 0
        if op == "union":
            extra = sort_extra(l.est.pages + r.est.pages, self.B)
        elif op in ("intersect", "except"):
            extra = 2 * l.est.pages * passes(l.est.pages, self.B) + 2 * r.est.pages * passes(r.est.pages, self.B)
        d = {"op": op, "left": l.d, "right": r.d}
        return self._finish(d, e, l.cum + r.cum + extra, [l, r])

    # ====================================================================================== join blocks
    @staticmethod
    def _leads_to_join(n):
        while n.op == "select":
            n = n.child
        return n.op in ("join", "cross")

    def extract(self, n, leaves, conds):
        """Collect the leaves of a join block and its predicates as (condition, origin). The block search does
        what rules 1 to 3 do (split, attach to a relation, cross + equality -> join), so each of those steps is
        only done here when its rule is switched on; otherwise the rule's switch would change nothing."""
        split = (lambda c: conjuncts(c)) if 1 in self.rules else (lambda c: [c])
        if n.op in ("join", "cross"):
            if n.op == "join":
                conds.extend((c, "join") for c in split(n.cond))
            self.extract(n.left, leaves, conds)
            self.extract(n.right, leaves, conds)
        elif n.op == "select" and self._leads_to_join(n):
            conds.extend((c, "select") for c in split(n.cond))
            self.extract(n.child, leaves, conds)
        else:
            leaves.append(n)

    def plan_block(self, n):
        leaves, conds = [], []
        self.extract(n, leaves, conds)
        leaf_cols = [set(self.cols_of(l)) for l in leaves]
        owners = []
        for c, _origin in conds:
            idx = set()
            for col in refs(c):
                hit = [i for i, cs in enumerate(leaf_cols) if col in cs]
                if not hit:
                    raise DBError("plan", f"a join predicate refers to a column that no input provides: {col[0]}.{col[1]}")
                idx.add(hit[0])
            owners.append(idx)
        # a predicate over ONE relation (or none) belongs on that relation, not in the join order search:
        # the first relation of a left-deep chain is never an inner, so the search would never apply it.
        # (With rule 2 off it is applied by a filter above the finished join instead.)
        pending, multi, post = {}, [], []
        for (c, origin), idx in zip(conds, owners):
            if len(idx) <= 1:
                if 2 in self.rules:
                    pending.setdefault(min(idx) if idx else 0, []).append(c)
                else:
                    post.append(c)
            else:
                multi.append((c, idx, origin))
        for i, cs in pending.items():
            leaves[i] = Node("select", None, cond=and_all(cs), child=leaves[i])
        self.leaf_plans = [self.plan(l) for l in leaves]
        self.leaf_cols = [set(self.cols_of(l)) for l in leaves]
        self.leaf_cols_ord = [list(self.cols_of(l)) for l in leaves]
        for lp, l in zip(self.leaf_plans, leaves):
            lp.label = self._label(l)
        self.atoms = [Atom(c, idx, allow_eq=(origin == "join" or 3 in self.rules)) for c, idx, origin in multi]
        self._subset = {}
        n_leaves = len(leaves)
        table = {"leaves": [lp.label for lp in self.leaf_plans], "rows": []}
        best = {}
        for i, lp in enumerate(self.leaf_plans):
            best[1 << i] = State(1 << i, lp.cum, self.subset_est(1 << i), None)
        if n_leaves <= DP_MAX:
            for size in range(2, n_leaves + 1):
                for mask in range(1, 1 << n_leaves):
                    if bin(mask).count("1") != size:
                        continue
                    st = self._best_for(mask, best, table)
                    if st is not None:
                        best[mask] = st
            full = (1 << n_leaves) - 1
        else:                                                     # greedy left-deep beyond DP_MAX relations
            cur = min(range(n_leaves), key=lambda i: (best[1 << i].est.pages, i))
            mask = 1 << cur
            while mask != (1 << n_leaves) - 1:
                cands = []
                for r in range(n_leaves):
                    if mask & (1 << r):
                        continue
                    opt = self._options(mask, r, best, table, record=False)
                    if opt:
                        cands.append(min(opt, key=lambda o: o[0]))
                if not cands:
                    cands = [min(self._options(mask, r, best, table, record=False, allow_cross=True), key=lambda o: o[0])
                             for r in range(n_leaves) if not mask & (1 << r)]
                o = min(cands, key=lambda o: o[0])
                mask |= 1 << o[4]
                best[mask] = State(mask, o[0], self.subset_est(mask), o)
            full = mask
        self.dp_tables.append(table)
        if full not in best:
            raise DBError("plan", "the optimizer found no way to join all relations")
        self._applied = []
        built = self.build(full, best)
        if sorted(id(a) for a in self._applied) != sorted(id(a) for a in self.atoms):
            raise DBError("plan", "internal error: the join order search did not apply every predicate exactly once")
        if post:                                                   # rule 2 off: single-relation predicates run last
            rc = and_all(post)
            rows = max(1, math.ceil(built.est.rows * cond_sel(rc, built.est.cols)))
            fd = {"op": "filter", "condition": unparse(rc), "child": built.d, "est_rows": rows, "est_cost": 0}
            top = P(fd, Est(rows, built.est.cols), built.cum, rc)
            top.cols = built.cols
            return top
        return built

    def _label(self, leaf):
        cols = self.cols_of(leaf)
        quals = [q for q, _ in cols if q]
        return quals[0] if quals else "?"

    def subset_est(self, mask):
        if mask in self._subset:
            return self._subset[mask]
        idx = [i for i in range(len(self.leaf_plans)) if mask >> i & 1]
        if mask in self.override:
            rows, pages = self.override[mask]
            e = Est(rows, {}, pages=pages)
        else:
            cols, rows = {}, 1
            for i in idx:
                le = self.leaf_plans[i].est
                cols.update(le.cols)
                rows *= le.rows
            if len(idx) == 1:
                e = self.leaf_plans[idx[0]].est
            else:
                for a in self.atoms:
                    if all(mask >> j & 1 for j in a.leaves):
                        rows *= cond_sel(a.cond, cols)
                rows = max(1, math.ceil(rows))
                e = Est(rows, {k: type(ci)(min(ci.ndv, rows), ci.cs, ci.width) for k, ci in cols.items()})
        self._subset[mask] = e
        return e

    def _preds(self, prev, r):
        full = prev | (1 << r)
        return [a for a in self.atoms if all(full >> j & 1 for j in a.leaves) and r in a.leaves]

    def _options(self, prev, r, best, table, record=True, allow_cross=False):
        """-> list of (cost, algo, preds, ordering, r) for joining leaf r onto the subset prev."""
        if prev not in best:
            return []
        S, leaf = best[prev], best[1 << r]
        preds = self._preds(prev, r)
        if not preds and not allow_cross:
            return []
        B = self.B
        pS, pR = S.est.pages, leaf.est.pages
        base_outer = bin(prev).count("1") == 1
        extra_r = max(0, leaf.cost - pR)
        outer_cost = max(0, S.cost - pS) if base_outer else S.cost
        has_eq = any(a.eq and r in a.leaves for a in preds)
        out = []
        for algo in ALGOS:
            if algo != "nested_loop_join" and not has_eq:
                continue
            if algo == "nested_loop_join":
                blocks = math.ceil(pS / (B - 2))
                cost = (S.cost if base_outer else S.cost + pS) + blocks * leaf.cost
            elif algo == "sort_merge_join":
                cost = outer_cost + extra_r + sort_total(pS, B) + sort_total(pR, B) + pS + pR
            else:
                cost = outer_cost + extra_r + 3 * (pS + pR)
            out.append((int(cost), algo, preds, prev, r))
        return out

    def _best_for(self, mask, best, table):
        opts = []
        for r in range(len(self.leaf_plans)):
            if mask >> r & 1:
                opts += self._options(mask & ~(1 << r), r, best, table)
        if not opts:                                              # disconnected: a cross product is the only way
            for r in range(len(self.leaf_plans)):
                if mask >> r & 1:
                    opts += self._options(mask & ~(1 << r), r, best, table, allow_cross=True)
        if not opts:
            return None
        chosen = min(opts, key=lambda o: (o[0], ALGOS.index(o[1]), o[4]))
        e = self.subset_est(mask)
        for o in sorted(opts, key=lambda o: (o[0], ALGOS.index(o[1]), o[4])):
            table["rows"].append({"subset": self._names(mask), "outer": self._names(o[3]), "inner": self.leaf_plans[o[4]].label,
                                  "algo": o[1], "rows": e.rows, "pages": e.pages, "cost": o[0], "chosen": o is chosen})
        return State(mask, chosen[0], e, chosen)

    def _names(self, mask):
        return "{" + ",".join(self.leaf_plans[i].label for i in range(len(self.leaf_plans)) if mask >> i & 1) + "}"

    # ====================================================================================== build
    def build(self, mask, best):
        st = best[mask]
        if st.choice is None:
            i = mask.bit_length() - 1
            self.leaf_plans[i].cols = self.leaf_cols_ord[i]
            return self.leaf_plans[i]
        cost, algo, preds, prev, r = st.choice
        self._applied.extend(preds)
        S = self.build(prev, best)
        L = self.leaf_plans[r]
        L.cols = self.leaf_cols_ord[r]
        leaves_prev = [j for j in range(len(self.leaf_plans)) if prev >> j & 1]
        eq = [a for a in preds if a.eq and any(j in a.leaves for j in leaves_prev)]
        resid = [a for a in preds if a not in eq] if algo != "nested_loop_join" else []
        used = eq if algo != "nested_loop_join" else preds
        cond = and_all([a.cond for a in used]) if used else Node("cmp", None, cmp="=", left=Node("num", None, value=1),
                                                                 right=Node("num", None, value=1))
        left, right = S, L
        swapped = algo == "hash_join" and L.est.pages < S.est.pages      # the smaller input is the build side
        if swapped:
            left, right = L, S
        join_rows = self.est.join_rows(S.est, L.est, [a.cond for a in used]).rows
        d = {"op": algo, "condition": unparse(cond), "left": left.d, "right": right.d}
        node = self._finish(d, Est(join_rows, st.est.cols), st.cost, [left, right])
        logical_cols = list(S.cols) + list(L.cols)
        top = node
        if resid:
            rc = and_all([a.cond for a in resid])
            fd = {"op": "filter", "condition": unparse(rc), "child": top.d}
            fd["est_rows"], fd["est_cost"] = st.est.rows, 0
            top = P(fd, st.est, st.cost, rc)
        if swapped:
            # the physical join emits (leaf, outer) columns; restore the logical order with a free projection
            pd = {"op": "project", "columns": [f"{q}.{n}" if q else n for q, n in logical_cols], "child": top.d}
            pd["est_rows"], pd["est_cost"] = st.est.rows, 0
            top = P(pd, st.est, st.cost)
        top.est = st.est
        top.cols = logical_cols
        return top


# ========================================================================================= explain
def explain_lines(d, depth=0):
    op = d["op"]
    if op == "seqscan":
        detail = d["table"] + (f" AS {d['alias']}" if d.get("alias") else "")
    elif op in ("filter", "nested_loop_join", "sort_merge_join", "hash_join"):
        detail = d["condition"]
    elif op == "project":
        detail = ", ".join(d["columns"])
    elif op == "sort":
        detail = ", ".join(f"{c} {x}" for c, x in d["keys"])
    elif op == "group_by":
        detail = f"{d['mode']}; keys: {', '.join(d['keys']) or '-'}; " + ", ".join(
            f"{a[0]}({a[1]})" for a in d["aggregates"])
    elif op == "distinct":
        detail = d["mode"]
    elif op == "limit":
        detail = str(d["n"])
    elif op == "rename":
        detail = d["to"]
    else:
        detail = ""
    head = f"{'  ' * depth}{op}" + (f" ({detail})" if detail else "")
    lines = [f"{head:<62} est_rows={d.get('est_rows', '?'):<8} est_cost={d.get('est_cost', '?')}"]
    for slot in ("child", "left", "right"):
        if slot in d:
            lines += explain_lines(d[slot], depth + 1)
    return lines


def dp_table_lines(table):
    lines = [f"relations: {', '.join(table['leaves'])}",
             f"{'subset':<22}{'outer':<20}{'inner':<10}{'algorithm':<18}{'rows':>9}{'pages':>8}{'cost':>10}  chosen"]
    last = None
    for r in table["rows"]:
        if last is not None and r["subset"] != last:
            lines.append("")
        last = r["subset"]
        lines.append(f"{r['subset']:<22}{r['outer']:<20}{r['inner']:<10}{r['algo']:<18}{r['rows']:>9}{r['pages']:>8}"
                     f"{r['cost']:>10}  {'<== best' if r['chosen'] else ''}")
    return lines
