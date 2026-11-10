"""Plan tree <-> JSON. JSON is the contract between modules, so this is also where a plan is
validated: errors name the offending node and the problem."""
import json

from ..errors import DBError
from .condition import parse_condition, parse_attr, unparse
from .nodes import Node, attr_str

LOGICAL = {"scan", "select", "project", "join", "cross", "rename", "group_by", "distinct",
           "sort", "limit", "union", "union_all", "intersect", "except"}
PHYSICAL = {"seqscan", "filter", "project", "distinct", "sort", "group_by", "nested_loop_join",
            "sort_merge_join", "hash_join", "union", "union_all", "intersect", "except", "limit",
            "rename"}     # rename is not in the M2 list, but a self join needs it (or a seqscan alias)

# op -> (scalar fields, child slots)
SPEC = {
    "scan": ([("table", "str")], []), "seqscan": ([("table", "str")], []),
    "select": ([("condition", "cond")], ["child"]), "filter": ([("condition", "cond")], ["child"]),
    "project": ([("columns", "cols+")], ["child"]),
    "rename": ([("to", "str")], ["child"]),
    "join": ([("condition", "cond")], ["left", "right"]),
    "nested_loop_join": ([("condition", "cond")], ["left", "right"]),
    "sort_merge_join": ([("condition", "cond")], ["left", "right"]),
    "hash_join": ([("condition", "cond")], ["left", "right"]),
    "cross": ([], ["left", "right"]),
    "group_by": ([("keys", "cols"), ("aggregates", "aggs")], ["child"]),
    "distinct": ([], ["child"]),
    "sort": ([("keys", "sortkeys")], ["child"]),
    "limit": ([("n", "int")], ["child"]),
    "union": ([], ["left", "right"]), "union_all": ([], ["left", "right"]),
    "intersect": ([], ["left", "right"]), "except": ([], ["left", "right"]),
}
FUNCS = ("count", "sum", "avg", "min", "max")


def _plan_err(op, msg, path):
    return DBError("plan", f"Node '{op}': {msg} [at {path}]")


def to_dict(n):
    d = {"op": n.op}
    if n.op in ("scan", "seqscan"):
        d["table"] = n.table
        if getattr(n, "alias", None):
            d["alias"] = n.alias
    if hasattr(n, "cond") and n.op in ("select", "filter", "join", "nested_loop_join",
                                       "sort_merge_join", "hash_join"):
        d["condition"] = unparse(n.cond)
    if n.op == "project":
        d["columns"] = [attr_str(a) for a in n.columns]
        if getattr(n, "aliases", None):
            d["aliases"] = list(n.aliases)
    if n.op == "rename":
        d["to"] = n.to
    if n.op == "group_by":
        d["keys"] = [attr_str(a) for a in n.keys]
        d["aggregates"] = [[f, a if a == "*" else attr_str(a)] + ([al] if al else [])
                           for f, a, al in n.aggregates]
    if n.op == "sort":
        d["keys"] = [[attr_str(a), dr] for a, dr in n.keys]
    if n.op == "limit":
        d["n"] = n.n
    if getattr(n, "mode", None):
        d["mode"] = n.mode
    for slot in ("child", "left", "right"):
        if hasattr(n, slot):
            d[slot] = to_dict(getattr(n, slot))
    return d


def to_json(n, indent=2):
    return json.dumps(to_dict(n), indent=indent)


def from_dict(d, level="any", path="root"):
    """level: 'logical' | 'physical' | 'any'. Raises DBError('plan', ...) on any problem."""
    if not isinstance(d, dict):
        raise DBError("plan", f"Node at {path}: expected a JSON object")
    if "op" not in d:
        raise DBError("plan", f"Node at {path}: missing required field 'op'.")
    op = d["op"]
    if not isinstance(op, str):
        raise DBError("plan", f"Node at {path}: field 'op' must be a string.")
    allowed = {"logical": LOGICAL, "physical": PHYSICAL, "any": LOGICAL | PHYSICAL}[level]
    if op not in allowed or op not in SPEC:
        raise _plan_err(op, f"unknown operator '{op}'.", path)
    scalars, slots = SPEC[op]
    fields = {}
    for name, kind in scalars:
        if name not in d:
            raise _plan_err(op, f"missing required field '{name}'.", path)
        fields[name] = _scalar(op, name, kind, d[name], path)
    if op in ("distinct", "group_by") and level == "physical" and "mode" not in d:
        raise _plan_err(op, "missing required field 'mode'.", path)
    if op == "project" and d.get("aliases") is not None:
        al = d["aliases"]
        if not (isinstance(al, list) and len(al) == len(d["columns"]) and all(a is None or isinstance(a, str) for a in al)):
            raise _plan_err(op, "field 'aliases' must be a list with one string or null per column.", path)
        fields["aliases"] = al
    if "mode" in d:
        if d["mode"] not in ("sort", "hash"):
            raise _plan_err(op, "field 'mode' must be \"sort\" or \"hash\".", path)
        if op in ("distinct", "group_by"):
            fields["mode"] = d["mode"]
    for slot in slots:
        if slot not in d:
            raise _plan_err(op, f"missing required field '{slot}'.", path)
        fields[slot] = from_dict(d[slot], level, f"{path}.{slot}")
    node = Node(op, None)
    if "condition" in fields:
        node.cond = fields.pop("condition")
    if op == "scan" or op == "seqscan":
        node.table = fields["table"]
        if isinstance(d.get("alias"), str) and d["alias"]:
            node.alias = d["alias"]
    for k, v in fields.items():
        setattr(node, k, v)
    return node


def _scalar(op, name, kind, v, path):
    if kind == "str":
        if not isinstance(v, str) or not v:
            raise _plan_err(op, f"field '{name}' must be a non-empty string.", path)
        return v
    if kind == "int":
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise _plan_err(op, f"field '{name}' must be a non-negative integer.", path)
        return v
    if kind == "cond":
        if not isinstance(v, str):
            raise _plan_err(op, f"field '{name}' must be a string.", path)
        try:
            return parse_condition(v)
        except DBError as e:
            raise _plan_err(op, f"field '{name}' is not a valid condition ({e.message}).", path)
    if kind in ("cols", "cols+"):
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise _plan_err(op, f"field '{name}' must be a list of column-name strings.", path)
        if kind == "cols+" and not v:
            raise _plan_err(op, f"field '{name}' must not be empty.", path)
        try:
            return [parse_attr(x) for x in v]
        except DBError as e:
            raise _plan_err(op, f"field '{name}' has a bad column name ({e.message}).", path)
    if kind == "sortkeys":
        if not isinstance(v, list) or not v:
            raise _plan_err(op, f"field '{name}' must be a non-empty list of [column, \"asc\"|\"desc\"] pairs.", path)
        out = []
        for item in v:
            if not (isinstance(item, list) and len(item) == 2 and isinstance(item[0], str)
                    and item[1] in ("asc", "desc")):
                raise _plan_err(op, f"field '{name}' must contain [column, \"asc\"|\"desc\"] pairs.", path)
            try:
                out.append((parse_attr(item[0]), item[1]))
            except DBError as e:
                raise _plan_err(op, f"field '{name}' has a bad column name ({e.message}).", path)
        return out
    if kind == "aggs":
        if not isinstance(v, list):
            raise _plan_err(op, f"field '{name}' must be a list of [function, column] or [function, column, alias].", path)
        out = []
        for item in v:
            if not (isinstance(item, list) and len(item) in (2, 3) and all(isinstance(x, str) for x in item)):
                raise _plan_err(op, f"field '{name}' must be a list of [function, column] or [function, column, alias].", path)
            f = item[0].lower()
            if f not in FUNCS:
                raise _plan_err(op, f"unknown aggregate function '{item[0]}' (use count, sum, avg, min, max).", path)
            if item[1] == "*":
                if f != "count":
                    raise _plan_err(op, f"'*' is only allowed with count, not {f}.", path)
                arg = "*"
            else:
                try:
                    arg = parse_attr(item[1])
                except DBError as e:
                    raise _plan_err(op, f"field '{name}' has a bad column name ({e.message}).", path)
            out.append((f, arg, item[2] if len(item) == 3 else None))
        return out
    raise AssertionError(kind)


def from_json(text, level="any"):
    try:
        d = json.loads(text)
    except json.JSONDecodeError as e:
        raise DBError("plan", f"malformed JSON: {e.msg} at line {e.lineno}, column {e.colno}")
    return from_dict(d, level)
