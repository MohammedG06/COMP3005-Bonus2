"""Semantic analysis + translation of SQL to a logical plan (spec section 3 to 5).

Canonical order, bottom to top:  FROM -> WHERE -> GROUP BY -> HAVING -> SELECT (project, distinct)
                                 -> ORDER BY -> LIMIT, set operators between whole blocks.
No optimisation is done here: a predicate stays exactly where the SQL put it.

Bag semantics (where duplicates are removed, and nowhere else):
  * project never removes duplicates; a distinct node is added only for SELECT DISTINCT, and not when the
    query is aggregating and its select list keeps every grouping column (one row per group already);
  * UNION is the `union` node (removes duplicates), UNION ALL is `union_all`, INTERSECT / EXCEPT are set
    operators by definition.
"""
from ..errors import DBError
from ..ra.nodes import Node
from ..schema import Column, Schema, INT, VARCHAR, REAL, comparable
from .parser import parse_sql


# ============================================================================================ catalog
class CatalogView:
    """What the translator needs from a catalog: table name -> list of Column."""

    def __init__(self, tables):
        self._t = tables

    def columns(self, name):
        return self._t.get(name)

    def names(self):
        return sorted(self._t)

    @classmethod
    def from_database(cls, db):
        return cls({n: db.catalog.get_table(n).columns for n in db.catalog.list_tables()})

    @classmethod
    def from_dict(cls, d):
        from ..storage.records import parse_schema
        return cls({n: parse_schema(s) for n, s in d.items()})


def _tname(t):
    return t


def _qattr(qual, name, pos):
    return Node("attr", pos, qual=qual, name=name)


class Scope:
    """One entry per FROM item, keyed by alias, else table name."""

    def __init__(self):
        self.entries = []                      # (name, [Column with qual = name])

    def add(self, name, cols, pos):
        if any(n == name for n, _ in self.entries):
            raise DBError("name", f"duplicate alias '{name}' in the FROM clause (give one of them an alias)", pos)
        self.entries.append((name, cols))

    def resolve(self, qual, name, pos):
        if qual is not None:
            for n, cols in self.entries:
                if n == qual:
                    for c in cols:
                        if c.name == name:
                            return c
                    raise DBError("name", f"column '{name}' does not exist in '{qual}'", pos)
            raise DBError("name", f"unknown table or alias '{qual}' in '{qual}.{name}'", pos)
        hits = [c for _, cols in self.entries for c in cols if c.name == name]
        if not hits:
            raise DBError("name", f"unknown column '{name}'", pos)
        if len(hits) > 1:
            raise DBError("ambiguity", f"column '{name}' is ambiguous: could be {' or '.join(c.qname for c in hits)}", pos)
        return hits[0]


class _Agg:
    def __init__(self, func, arg, name, ctype, length):
        self.func, self.arg, self.name, self.type, self.length = func, arg, name, ctype, length

    def key(self):
        return (self.func, "*" if self.arg == "*" else (self.arg.qual, self.arg.name))


class Compiler:
    def __init__(self, catalog):
        self.cat = catalog

    # ---------------------------------------------------------------------------- queries
    def compile_query(self, q):
        if q.op == "setop":
            lp, ls = self.compile_query(q.left)
            rp, rs = self.compile_query(q.right)
            if len(ls) != len(rs):
                raise DBError("schema", f"{q.kind.upper()} needs the same number of columns on both sides "
                              f"({len(ls)} vs {len(rs)})", q.pos)
            for i, (a, b) in enumerate(zip(ls, rs), 1):
                if a.type != b.type:
                    raise DBError("schema", f"{q.kind.upper()}: column {i} is {a.type} on the left but {b.type} on the right", q.pos)
            op = "union_all" if (q.kind == "union" and q.all) else q.kind
            return Node(op, q.pos, left=lp, right=rp), ls
        return self.compile_block(q)

    def table_ref(self, ref):
        if ref.kind == "table":
            cols = self.cat.columns(ref.name)
            if cols is None:
                raise DBError("name", f"unknown table '{ref.name}'", ref.pos)
            entry = ref.alias or ref.name
            plan = Node("scan", ref.pos, table=ref.name)
            if ref.alias:
                plan = Node("rename", ref.pos, to=ref.alias, child=plan)
            return plan, entry, [Column(c.name, c.type, c.length, entry) for c in cols]
        plan, out = self.compile_query(ref.query)
        names = [c.name for c in out]
        for n in names:
            if names.count(n) > 1:
                raise DBError("schema", f"subquery '{ref.alias}' has two output columns named '{n}'; "
                              f"give one of them an alias with AS", ref.pos)
        return (Node("rename", ref.pos, to=ref.alias, child=plan), ref.alias,
                [Column(c.name, c.type, c.length, ref.alias) for c in out])

    # ---------------------------------------------------------------------------- one block
    def compile_block(self, b):
        scope, plan = Scope(), None
        for idx, ref in enumerate(b.tables):
            rplan, entry, cols = self.table_ref(ref)
            scope.add(entry, cols, ref.pos)
            if plan is None:
                plan = rplan
            elif b.join_conds is not None:
                cond = self.cond(b.join_conds[idx - 1], scope, "JOIN ... ON")
                plan = Node("join", ref.pos, cond=cond, left=plan, right=rplan)
            else:
                plan = Node("cross", ref.pos, left=plan, right=rplan)
        if b.where is not None:
            plan = Node("select", b.where.pos, cond=self.cond(b.where, scope, "WHERE"), child=plan)

        aggregating = bool(b.group_by) or any(it.kind == "agg" for it in b.items)
        if aggregating:
            return self.aggregating(b, scope, plan)
        return self.plain(b, scope, plan)

    # ---------------------------------------------------------------------------- non aggregating
    def plain(self, b, scope, plan):
        cols, aliases, out, projected = [], [], [], set()
        for it in b.items:
            if it.kind == "star":
                for entry, ecols in scope.entries:
                    for c in ecols:
                        cols.append(_qattr(entry, c.name, it.pos))
                        aliases.append(None)
                        out.append(Column(c.name, c.type, c.length))
                        projected.add((entry, c.name))
            else:
                c = scope.resolve(it.attr.qual, it.attr.name, it.attr.pos)
                cols.append(_qattr(c.qual, c.name, it.pos))
                aliases.append(it.alias)
                out.append(Column(it.alias or c.name, c.type, c.length))
                if not it.alias:
                    projected.add((c.qual, c.name))
        out_names = {it.alias for it in b.items if it.alias}
        keys, above = self.order_keys(b, scope, out_names, projected, None)
        if keys and not above:
            plan = Node("sort", b.pos, keys=keys, child=plan)
        plan = Node("project", b.pos, columns=cols, aliases=aliases if any(aliases) else None, child=plan)
        if b.distinct:
            plan = Node("distinct", b.pos, child=plan)
        if keys and above:
            plan = Node("sort", b.pos, keys=keys, child=plan)
        if b.limit is not None:
            plan = Node("limit", b.pos, n=b.limit, child=plan)
        return plan, Schema(out)

    # ---------------------------------------------------------------------------- aggregating
    def aggregating(self, b, scope, plan):
        keys, key_set = [], set()
        for g in b.group_by:
            c = scope.resolve(g.qual, g.name, g.pos)
            keys.append(_qattr(c.qual, c.name, g.pos))
            key_set.add((c.qual, c.name))
        aggs = []

        def add_agg(func, arg, pos, alias, reuse):
            if arg == "*":
                a, t, length = "*", INT, None
            else:
                c = scope.resolve(arg.qual, arg.name, arg.pos)
                a, t, length = _qattr(c.qual, c.name, arg.pos), c.type, c.length
                if func in ("sum", "avg") and t != INT:
                    raise DBError("type", f"{func.upper()} needs an INT column, but {c.qname} is {t}", pos)
            rtype = INT if func in ("count", "sum") else (REAL if func == "avg" else t)
            rlen = length if func in ("min", "max") else None
            probe = _Agg(func, a, None, rtype, rlen)
            if reuse:
                for e in aggs:
                    if e.key() == probe.key():
                        return e
            name = alias or (f"{func}_star" if a == "*" else f"{func}_{a.qual}_{a.name}")
            probe.name = name
            aggs.append(probe)
            return probe

        cols, aliases, out = [], [], []
        for it in b.items:
            if it.kind == "star":
                raise DBError("aggregate", "SELECT * cannot be used with GROUP BY or aggregate functions", it.pos)
            if it.kind == "col":
                c = scope.resolve(it.attr.qual, it.attr.name, it.attr.pos)
                if (c.qual, c.name) not in key_set:
                    raise DBError("aggregate", f"column '{c.qname}' must appear in the GROUP BY clause or be "
                                  f"used in an aggregate function", it.pos)
                cols.append(_qattr(c.qual, c.name, it.pos))
                aliases.append(it.alias)
                out.append(Column(it.alias or c.name, c.type, c.length))
            else:
                e = add_agg(it.func, it.arg, it.pos, it.alias, reuse=False)
                cols.append(_qattr(None, e.name, it.pos))
                aliases.append(None)
                out.append(Column(e.name, e.type, e.length))
        names = [e.name for e in aggs]
        for n in names:
            if names.count(n) > 1:
                raise DBError("schema", f"two aggregates are both named '{n}'; give one an alias with AS", b.pos)

        having = None
        if b.having is not None:
            having = self.having_cond(b.having, scope, key_set, add_agg)
        plan = Node("group_by", b.pos, keys=keys, aggregates=[(e.func, e.arg, e.name) for e in aggs], child=plan)
        if having is not None:
            plan = Node("select", b.having.pos, cond=having, child=plan)

        out_names = {c.name for c in out if any(a is None for a in [None])}   # every output name is usable
        out_names = {it.alias for it in b.items if it.alias} | {e.name for e in aggs}
        projected = {(c.qual, c.name) for c, al in zip(cols, aliases) if c.qual is not None and al is None}
        okeys, above = self.order_keys(b, scope, out_names, projected, key_set)
        if okeys and not above:
            plan = Node("sort", b.pos, keys=okeys, child=plan)
        plan = Node("project", b.pos, columns=cols, aliases=aliases if any(aliases) else None, child=plan)
        covers_groups = bool(keys) and all(k in {(c.qual, c.name) for c in cols if c.qual} for k in key_set)
        if b.distinct and keys and not covers_groups:
            plan = Node("distinct", b.pos, child=plan)       # a grouping column was left out: duplicates possible
        if okeys and above:
            plan = Node("sort", b.pos, keys=okeys, child=plan)
        if b.limit is not None:
            plan = Node("limit", b.pos, n=b.limit, child=plan)
        return plan, Schema(out)

    # ---------------------------------------------------------------------------- order by
    def order_keys(self, b, scope, out_names, projected, key_set):
        if not b.order_by:
            return [], True
        keys, kinds = [], []
        for col, d in b.order_by:
            if col.qual is None and col.name in out_names:           # output alias first
                keys.append((_qattr(None, col.name, col.pos), d))
                kinds.append("out")
                continue
            c = scope.resolve(col.qual, col.name, col.pos)
            if key_set is not None and (c.qual, c.name) not in key_set:
                raise DBError("aggregate", f"ORDER BY column '{c.qname}' must be a grouping column or an output name "
                              f"in a query with aggregates", col.pos)
            keys.append((_qattr(c.qual, c.name, col.pos), d))
            kinds.append("in" if (c.qual, c.name) in projected else "hidden")
        if "hidden" not in kinds:
            return keys, True
        if "out" in kinds:
            raise DBError("schema", "ORDER BY mixes an output alias with a column that is not in the select list", b.pos)
        if b.distinct:
            raise DBError("schema", "for SELECT DISTINCT, ORDER BY columns must appear in the select list", b.pos)
        return keys, False               # sort below the projection, where the column still exists

    # ---------------------------------------------------------------------------- conditions
    def cond(self, c, scope, where):
        if c.op in ("and", "or"):
            return Node(c.op, c.pos, left=self.cond(c.left, scope, where), right=self.cond(c.right, scope, where))
        if c.op == "not":
            return Node("not", c.pos, child=self.cond(c.child, scope, where))
        l, lt = self.operand(c.left, scope, where)
        r, rt = self.operand(c.right, scope, where)
        return self.finish_cmp(c, l, lt, r, rt)

    def finish_cmp(self, c, l, lt, r, rt):
        if not comparable(lt, rt):
            raise DBError("type", f"cannot compare {lt} with {rt} in this condition", c.pos)
        return Node("cmp", c.pos, cmp=c.cmp, left=l, right=r)

    def operand(self, o, scope, where):
        if o.op == "num":
            return o, INT
        if o.op == "str":
            return o, VARCHAR
        if o.op == "agg":
            raise DBError("aggregate", f"aggregate function {o.func.upper()} is not allowed in {where} "
                          f"(it runs before grouping)", o.pos)
        c = scope.resolve(o.qual, o.name, o.pos)
        return _qattr(c.qual, c.name, o.pos), c.type

    def having_cond(self, c, scope, key_set, add_agg):
        if c.op in ("and", "or"):
            return Node(c.op, c.pos, left=self.having_cond(c.left, scope, key_set, add_agg),
                        right=self.having_cond(c.right, scope, key_set, add_agg))
        if c.op == "not":
            return Node("not", c.pos, child=self.having_cond(c.child, scope, key_set, add_agg))

        def operand(o):
            if o.op == "num":
                return o, INT
            if o.op == "str":
                return o, VARCHAR
            if o.op == "agg":
                e = add_agg(o.func, o.arg, o.pos, None, reuse=True)
                return _qattr(None, e.name, o.pos), e.type
            c = scope.resolve(o.qual, o.name, o.pos)
            if (c.qual, c.name) not in key_set:
                raise DBError("aggregate", f"HAVING may only use grouping columns and aggregates; "
                              f"'{c.qname}' is neither", o.pos)
            return _qattr(c.qual, c.name, o.pos), c.type

        l, lt = operand(c.left)
        r, rt = operand(c.right)
        return self.finish_cmp(c, l, lt, r, rt)


# ================================================================================================ statements
def compile_query_text(sql, catalog):
    """-> list of (statement node, plan or None, output Schema or None) for every statement."""
    out = []
    comp = Compiler(catalog)
    for st in parse_sql(sql):
        if st.op in ("create", "insert"):
            check_ddl(st, catalog)
            out.append((st, None, None))
        else:
            plan, schema = comp.compile_query(st)
            out.append((st, plan, schema))
    return out


def check_ddl(st, catalog):
    if st.op == "create":
        if catalog.columns(st.table) is not None:
            raise DBError("schema", f"table '{st.table}' already exists", st.name_pos)
        seen = set()
        for name, typ, length in st.columns:
            if name in seen:
                raise DBError("schema", f"duplicate column name '{name}'", st.pos)
            seen.add(name)
            if typ == "VARCHAR" and not 1 <= length <= 255:
                raise DBError("schema", f"VARCHAR length must be between 1 and 255, got {length}", st.pos)
        return
    cols = catalog.columns(st.table)
    if cols is None:
        raise DBError("name", f"unknown table '{st.table}'", st.name_pos)
    if len(cols) != len(st.values):
        raise DBError("schema", f"table '{st.table}' has {len(cols)} columns but {len(st.values)} values were given", st.pos)
    for c, v in zip(cols, st.values):
        if c.type == INT:
            if v.op != "num":
                raise DBError("type", f"column '{c.name}' is INT but the value is a string", v.pos)
            if not -2 ** 31 <= v.value <= 2 ** 31 - 1:
                raise DBError("type", f"column '{c.name}': {v.value} does not fit in 4 signed bytes", v.pos)
        else:
            if v.op != "str":
                raise DBError("type", f"column '{c.name}' is VARCHAR but the value is a number", v.pos)
            if len(v.value.encode("utf-8")) > c.length:
                raise DBError("type", f"column '{c.name}': value is longer than VARCHAR({c.length})", v.pos)
