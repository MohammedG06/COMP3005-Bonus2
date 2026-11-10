"""Rule based rewrites on the logical plan (spec M4 section 3). Rule numbers (switchable for the
"turn one rule off" measurement):

  1 split a conjunction into stacked selections        4 push a selection through rename / project / union
  2 push a selection below a join or cross             5 push a selection below group_by (grouping columns only)
  3 cross + equality selection -> join                 6 push projections down (prune columns below joins / group_by)
  7 remove a projection that drops nothing, merge adjacent selections   (done when the physical plan is built)
"""
from ..ra.nodes import Node
from .condutil import conjuncts, and_all, refs, is_col_eq, map_cols, clone, ColsOf

ALL_RULES = frozenset(range(1, 8))


class Rewriter:
    def __init__(self, catalog, rules=ALL_RULES):
        self.cols = ColsOf(catalog)
        self.rules = frozenset(rules)
        self.fired = []

    def run(self, node):
        node = self.rec(node)
        if 6 in self.rules:
            node = self.prune(node, set(self.cols(node)))
        return node

    # ------------------------------------------------------------------------ rules 1 to 5
    def rec(self, n):
        kids = {}
        for slot in ("child", "left", "right"):
            if hasattr(n, slot):
                kids[slot] = self.rec(getattr(n, slot))
        n = clone(n, **kids) if kids else n
        if n.op == "select":
            conjs = conjuncts(n.cond) if 1 in self.rules else [n.cond]
            if len(conjs) > 1:
                self.fired.append(1)
            res = n.child
            for p in reversed(conjs):
                res = self.push(p, res)
            return res
        if n.op == "join" and 2 in self.rules:
            lc, rc = set(self.cols(n.left)), set(self.cols(n.right))
            keep, left, right = [], n.left, n.right
            for a in conjuncts(n.cond):
                rs = refs(a)
                if rs and rs <= lc:
                    left = self.push(a, left)
                    self.fired.append(2)
                elif rs and rs <= rc:
                    right = self.push(a, right)
                    self.fired.append(2)
                else:
                    keep.append(a)
            if not keep:
                return Node("cross", n.pos, left=left, right=right)
            return clone(n, cond=and_all(keep), left=left, right=right)
        return n

    def push(self, p, child):
        rs = refs(p)
        op = child.op
        if op in ("join", "cross") and 2 in self.rules and rs:
            lc, rc = set(self.cols(child.left)), set(self.cols(child.right))
            if rs <= lc:
                self.fired.append(2)
                return clone(child, left=self.push(p, child.left))
            if rs <= rc:
                self.fired.append(2)
                return clone(child, right=self.push(p, child.right))
            if 3 in self.rules and op == "cross" and is_col_eq(p) and rs <= lc | rc:
                self.fired.append(3)
                return Node("join", child.pos, cond=p, left=child.left, right=child.right)
        elif op == "select" and 2 in self.rules:
            # selections commute: let p travel through this one, so it can still reach a join below it
            return clone(child, child=self.push(p, child.child))
        elif op == "project" and 4 in self.rules:
            al = getattr(child, "aliases", None) or [None] * len(child.columns)
            plain = {(a.qual, a.name) for a, x in zip(child.columns, al) if x is None}
            if rs and rs <= plain:                   # a projection that keeps these columns unchanged commutes with p
                self.fired.append(4)
                return clone(child, child=self.push(p, child.child))
        elif op == "rename" and 4 in self.rules and child.child.op != "scan":
            below = self.cols(child.child)
            byname = {name: (q, name) for q, name in below}
            if all(name in byname for _, name in rs) and all(q == child.to for q, _ in rs):
                self.fired.append(4)
                return clone(child, child=self.push(map_cols(p, lambda k: byname[k[1]]), child.child))
        elif op in ("union", "union_all") and 4 in self.rules:
            lcols, rcols = self.cols(child.left), self.cols(child.right)
            pos = {k: i for i, k in enumerate(lcols)}
            if rs and rs <= set(lcols):
                self.fired.append(4)
                pr = map_cols(p, lambda k: rcols[pos[k]])
                return clone(child, left=self.push(p, child.left), right=self.push(pr, child.right))
        elif op == "group_by" and 5 in self.rules and child.keys:
            if rs and rs <= {(k.qual, k.name) for k in child.keys}:
                self.fired.append(5)
                return clone(child, child=self.push(p, child.child))
        return Node("select", p.pos, cond=p, child=child)

    # ------------------------------------------------------------------------ rule 6
    def prune(self, n, needed):
        """needed: columns the ancestors still use. Inserts a projection above a join input or a
        group_by input that carries columns nobody needs."""
        op = n.op
        if op == "project":
            need = set()
            for a in n.columns:
                need.add((a.qual, a.name))
            return clone(n, child=self.prune(n.child, need))
        if op == "select":
            return clone(n, child=self.prune(n.child, needed | refs(n.cond)))
        if op == "sort":
            return clone(n, child=self.prune(n.child, needed | {(a.qual, a.name) for a, _ in n.keys}))
        if op == "limit":
            return clone(n, child=self.prune(n.child, needed))
        if op == "distinct":
            return clone(n, child=self.prune(n.child, set(self.cols(n.child))))
        if op == "group_by":
            need = {(k.qual, k.name) for k in n.keys}
            need |= {(a.qual, a.name) for _, a, _ in n.aggregates if a != "*"}
            return clone(n, child=self.wrap(self.prune(n.child, need), self.cols(n.child), need))
        if op == "rename":
            below = self.cols(n.child)
            need = {(q, name) for q, name in below if (n.to, name) in needed}
            return clone(n, child=self.prune(n.child, need))
        if op in ("join", "cross"):
            need = needed | (refs(n.cond) if op == "join" else set())
            kids = {}
            for slot in ("left", "right"):
                c = getattr(n, slot)
                cc = self.cols(c)
                p = self.prune(c, need & set(cc))
                kids[slot] = p if c.op in ("join", "cross") else self.wrap(p, cc, need)
            return clone(n, **kids)
        if op in ("union", "union_all", "intersect", "except"):
            return clone(n, left=self.prune(n.left, set(self.cols(n.left))), right=self.prune(n.right, set(self.cols(n.right))))
        return n

    def wrap(self, node, cols, needed):
        keep = [c for c in cols if c in needed]
        if keep and len(keep) < len(cols):
            self.fired.append(6)
            return Node("project", None, columns=[Node("attr", None, qual=q, name=n) for q, n in keep], aliases=None, child=node)
        return node
