"""Tree nodes (plans and conditions) and the tree printer.

One Node class for everything. node.op is one of
  plan ops (logical):  scan select project join cross rename group_by distinct sort limit
                       union union_all intersect except
  plan ops (physical): seqscan filter project distinct sort group_by nested_loop_join
                       sort_merge_join hash_join union union_all intersect except limit
  condition ops:       cmp and or not
  operand ops:         attr num str
Plan fields: table, cond, columns, to, keys, aggregates, n, mode, child / left / right.
"""

CMP_NAMES = {"=": "Eq", "<>": "Ne", "<": "Lt", "<=": "Le", ">": "Gt", ">=": "Ge"}
UNARY = ("select", "filter", "project", "rename", "group_by", "distinct", "sort", "limit")
BINARY = ("join", "cross", "union", "union_all", "intersect", "except",
          "nested_loop_join", "sort_merge_join", "hash_join")
HAS_COND = ("select", "filter", "join", "nested_loop_join", "sort_merge_join", "hash_join")


class Node:
    def __init__(self, op, pos=None, **fields):
        self.op = op
        self.pos = pos
        for k, v in fields.items():
            setattr(self, k, v)

    @property
    def condition(self):
        from .condition import unparse
        return unparse(self.cond)

    def children(self):
        if self.op in UNARY:
            return [self.child]
        if self.op in BINARY:
            return [self.left, self.right]
        return []

    def label(self):
        op = self.op
        if op in ("scan", "seqscan"):
            return f"{'Scan' if op == 'scan' else 'SeqScan'}({self.table})"
        if op in HAS_COND:
            name = {"select": "Select", "filter": "Filter", "join": "Join",
                    "nested_loop_join": "NestedLoopJoin", "sort_merge_join": "SortMergeJoin",
                    "hash_join": "HashJoin"}[op]
            return f"{name}(cond={cond_str(self.cond)})"
        if op == "project":
            return "Project(cols=[" + ", ".join(attr_str(a) for a in self.columns) + "])"
        if op == "rename":
            return f"Rename(as={self.to})"
        if op == "group_by":
            keys = ", ".join(attr_str(a) for a in self.keys)
            aggs = ", ".join(f"{f}({a if a == '*' else attr_str(a)})" + (f" AS {al}" if al else "")
                             for f, a, al in self.aggregates)
            m = f" mode={self.mode}" if getattr(self, "mode", None) else ""
            return f"GroupBy(keys=[{keys}], aggs=[{aggs}]{m})"
        if op == "distinct":
            m = getattr(self, "mode", None)
            return f"Distinct(mode={m})" if m else "Distinct"
        if op == "sort":
            return "Sort(" + ", ".join(f"{attr_str(a)} {d}" for a, d in self.keys) + ")"
        if op == "limit":
            return f"Limit({self.n})"
        return {"cross": "Cross", "union": "Union", "union_all": "UnionAll",
                "intersect": "Intersect", "except": "Except"}[op]


def attr_str(a):
    return f"{a.qual}.{a.name}" if a.qual else a.name


def operand_str(o):
    if o.op == "attr":
        return f"Attr({attr_str(o)})"
    if o.op == "num":
        return f"Num({o.value})"
    return "Str('" + o.value.replace("'", "''") + "')"


def cond_str(c):
    if c.op == "cmp":
        return f"{CMP_NAMES[c.cmp]}({operand_str(c.left)}, {operand_str(c.right)})"
    if c.op in ("and", "or"):
        return f"{c.op.capitalize()}({cond_str(c.left)}, {cond_str(c.right)})"
    if c.op == "not":
        return f"Not({cond_str(c.child)})"
    raise ValueError(c.op)


def render_tree(root):
    lines = [root.label()]

    def walk(node, prefix):
        kids = node.children()
        for i, k in enumerate(kids):
            last = i == len(kids) - 1
            lines.append(prefix + ("└── " if last else "├── ") + k.label())
            walk(k, prefix + ("    " if last else "│   "))

    walk(root, "")
    return "\n".join(lines)
