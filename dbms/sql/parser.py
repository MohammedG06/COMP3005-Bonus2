"""Recursive descent SQL parser. One method per rule of GRAMMAR_SQL.md. Panic mode: the first
error is reported and parsing stops.

AST (all dbms.ra.nodes.Node):
  create(table, columns=[(name, 'INT'|'VARCHAR', length)])      insert(table, values=[num|str])
  block(distinct, items, tables, join_conds, where, group_by, having, order_by, limit)
  setop(kind='union'|'intersect'|'except', all, left, right)
  item(kind='star'|'col'|'agg', attr, func, arg, alias)         tref(kind='table'|'sub', name, query, alias)
  conditions: cmp / and / or / not over operands attr / num / str / agg
"""
from ..errors import DBError
from ..ra.nodes import Node
from .lexer import lex, AGG_FUNCS

CMPS = ("=", "<>", "<", "<=", ">", ">=")


class SQLParser:
    def __init__(self, src):
        self.src = src
        self.toks = lex(src)
        self.i = 0

    # ---- helpers -------------------------------------------------------------------
    def peek(self, k=0):
        return self.toks[min(self.i + k, len(self.toks) - 1)]

    def advance(self):
        t = self.toks[self.i]
        if t.type != "EOF":
            self.i += 1
        return t

    def at(self, *types):
        return self.peek().type in types

    @staticmethod
    def describe(t):
        return "end of input" if t.type == "EOF" else (f"string {t.text}" if t.type == "STR" else f"'{t.text}'")

    def fail(self, msg, tok=None):
        raise DBError("syntax", msg, (tok or self.peek()).pos)

    def expect(self, ttype, what=None):
        t = self.peek()
        if t.type != ttype:
            self.fail(f"expected {what or ttype} but found {self.describe(t)}")
        return self.advance()

    def ident(self, what="a name"):
        t = self.peek()
        if t.type != "IDENT":
            hint = " (that word is a reserved keyword)" if t.type.isupper() and t.type.isalpha() and t.type != "EOF" else ""
            self.fail(f"expected {what} but found {self.describe(t)}{hint}")
        return self.advance()

    # ---- statements --------------------------------------------------------------------
    def parse_statements(self):
        out = []
        while True:
            while self.at(";"):
                self.advance()
            if self.at("EOF"):
                return out
            out.append(self.parse_statement())
            if not self.at(";", "EOF"):
                self.fail(f"unexpected {self.describe(self.peek())}; expected ';' or end of statement")

    def parse_statement(self):
        t = self.peek()
        if t.type == "CREATE":
            return self.parse_create()
        if t.type == "INSERT":
            return self.parse_insert()
        if t.type in ("SELECT", "("):
            return self.parse_query()
        self.fail(f"expected CREATE, INSERT or SELECT but found {self.describe(t)}")

    def parse_create(self):
        kw = self.advance()
        self.expect("TABLE")
        name = self.ident("a table name")
        self.expect("(")
        cols = []
        while True:
            cname = self.ident("a column name")
            if self.at("INT"):
                self.advance()
                cols.append((cname.text, "INT", None))
            elif self.at("VARCHAR"):
                self.advance()
                self.expect("(")
                n = self.expect("NUM", "a length")
                self.expect(")")
                cols.append((cname.text, "VARCHAR", n.value))
            else:
                self.fail(f"expected INT or VARCHAR(n) but found {self.describe(self.peek())}")
            if self.at(","):
                self.advance()
                continue
            break
        self.expect(")")
        return Node("create", kw.pos, table=name.text, columns=cols, name_pos=name.pos)

    def parse_insert(self):
        kw = self.advance()
        self.expect("INTO")
        name = self.ident("a table name")
        self.expect("VALUES")
        self.expect("(")
        vals = [self.parse_literal()]
        while self.at(","):
            self.advance()
            vals.append(self.parse_literal())
        self.expect(")")
        return Node("insert", kw.pos, table=name.text, values=vals, name_pos=name.pos)

    def parse_literal(self):
        t = self.peek()
        if t.type == "NUM":
            self.advance()
            return Node("num", t.pos, value=t.value)
        if t.type == "STR":
            self.advance()
            return Node("str", t.pos, value=t.value)
        self.fail(f"expected a number or a quoted string but found {self.describe(t)}")

    # ---- queries ---------------------------------------------------------------------------
    def parse_query(self):             # query ::= block { setop block }   (left associative, one level)
        left = self.parse_block()
        while self.at("UNION", "INTERSECT", "EXCEPT"):
            op = self.advance()
            all_ = False
            if op.type == "UNION" and self.at("ALL"):
                self.advance()
                all_ = True
            right = self.parse_block()
            left = Node("setop", op.pos, kind=op.type.lower(), all=all_, left=left, right=right)
        return left

    def parse_block(self):
        if self.at("("):
            self.advance()
            q = self.parse_query()
            self.expect(")")
            return q
        kw = self.expect("SELECT", "SELECT or '('")
        distinct = False
        if self.at("DISTINCT"):
            self.advance()
            distinct = True
        items = self.parse_select_list()
        self.expect("FROM")
        tables, join_conds = self.parse_from()
        where = group_by = having = None
        group_by = []
        if self.at("WHERE"):
            self.advance()
            where = self.parse_cond()
        if self.at("GROUP"):
            self.advance()
            self.expect("BY")
            group_by = [self.parse_column()]
            while self.at(","):
                self.advance()
                group_by.append(self.parse_column())
            if self.at("HAVING"):
                self.advance()
                having = self.parse_cond()
        order_by = []
        if self.at("ORDER"):
            self.advance()
            self.expect("BY")
            while True:
                col = self.parse_column()
                d = "asc"
                if self.at("ASC", "DESC"):
                    d = self.advance().type.lower()
                order_by.append((col, d))
                if self.at(","):
                    self.advance()
                    continue
                break
        limit = None
        if self.at("LIMIT"):
            self.advance()
            n = self.expect("NUM", "a row count")
            if n.value < 0:
                self.fail("LIMIT must not be negative", n)
            limit = n.value
        return Node("block", kw.pos, distinct=distinct, items=items, tables=tables, join_conds=join_conds,
                    where=where, group_by=group_by, having=having, order_by=order_by, limit=limit)

    def parse_select_list(self):
        if self.at("*"):
            t = self.advance()
            return [Node("item", t.pos, kind="star", alias=None)]
        items = [self.parse_item()]
        while self.at(","):
            self.advance()
            items.append(self.parse_item())
        return items

    def parse_item(self):
        t = self.peek()
        if t.type in AGG_FUNCS:
            node = self.parse_aggregate()
            item = Node("item", t.pos, kind="agg", func=node.func, arg=node.arg, alias=None)
        else:
            col = self.parse_column()
            item = Node("item", t.pos, kind="col", attr=col, alias=None)
        if self.at("AS"):
            self.advance()
            item.alias = self.ident("an alias").text
        return item

    def parse_aggregate(self):
        f = self.advance()
        self.expect("(")
        if self.at("*"):
            if f.type != "COUNT":
                self.fail(f"{f.type} cannot take '*' (only COUNT(*) can)", f)
            self.advance()
            arg = "*"
        else:
            arg = self.parse_column()
        self.expect(")")
        return Node("agg", f.pos, func=f.type.lower(), arg=arg)

    def parse_column(self):
        first = self.ident("a column name")
        if self.at("."):
            self.advance()
            second = self.ident("a column name after '.'")
            return Node("attr", first.pos, qual=first.text, name=second.text)
        return Node("attr", first.pos, qual=None, name=first.text)

    # ---- FROM ----------------------------------------------------------------------------------
    def parse_from(self):
        tables = [self.parse_table_ref()]
        conds = None
        if self.at("JOIN"):
            conds = []
            while self.at("JOIN"):
                self.advance()
                tables.append(self.parse_table_ref())
                self.expect("ON")
                conds.append(self.parse_cond())
        else:
            while self.at(","):
                self.advance()
                tables.append(self.parse_table_ref())
        return tables, conds

    def parse_table_ref(self):
        if self.at("("):
            lp = self.advance()
            q = self.parse_query()
            self.expect(")")
            if self.at("AS"):
                self.advance()
            if not self.at("IDENT"):
                self.fail("a subquery in FROM must have an alias", self.peek())
            alias = self.advance().text
            return Node("tref", lp.pos, kind="sub", query=q, name=None, alias=alias)
        t = self.ident("a table name")
        alias = None
        if self.at("AS"):
            self.advance()
            alias = self.ident("an alias").text
        elif self.at("IDENT"):                      # bare alias: FROM Emp e   (used by the spec's own examples)
            alias = self.advance().text
        return Node("tref", t.pos, kind="table", name=t.text, query=None, alias=alias)

    # ---- conditions: OR < AND < NOT, enforced by the grammar ------------------------------------------
    def parse_cond(self):
        left = self.parse_and()
        while self.at("OR"):
            op = self.advance()
            left = Node("or", op.pos, left=left, right=self.parse_and())
        return left

    def parse_and(self):
        left = self.parse_not()
        while self.at("AND"):
            op = self.advance()
            left = Node("and", op.pos, left=left, right=self.parse_not())
        return left

    def parse_not(self):
        if self.at("NOT"):
            t = self.advance()
            return Node("not", t.pos, child=self.parse_not())
        if self.at("("):
            lp = self.advance()
            c = self.parse_cond()
            self.expect(")")
            return c
        return self.parse_comparison()

    def parse_comparison(self):
        left = self.parse_operand()
        t = self.peek()
        if t.type not in CMPS:
            self.fail(f"expected a comparison operator (=, <>, <, <=, >, >=) but found {self.describe(t)}")
        self.advance()
        right = self.parse_operand()
        return Node("cmp", left.pos, cmp=t.type, left=left, right=right)

    def parse_operand(self):
        t = self.peek()
        if t.type == "NUM":
            self.advance()
            return Node("num", t.pos, value=t.value)
        if t.type == "STR":
            self.advance()
            return Node("str", t.pos, value=t.value)
        if t.type in AGG_FUNCS:
            return self.parse_aggregate()          # parsed so the analyzer can give a precise 'aggregate' error
        if t.type == "IDENT":
            return self.parse_column()
        self.fail(f"expected a column, number or quoted string but found {self.describe(t)}")


def parse_sql(src):
    return SQLParser(src).parse_statements()


def ast_lines(node, depth=0):
    """Indented tree for `sql parse --explain`."""
    pad = "  " * depth
    k = node.op
    if k == "create":
        return [f"{pad}CreateTable {node.table} ({', '.join(c[0] + ' ' + c[1] + (f'({c[2]})' if c[2] else '') for c in node.columns)})"]
    if k == "insert":
        return [f"{pad}Insert {node.table} VALUES ({', '.join(repr(v.value) for v in node.values)})"]
    if k == "setop":
        out = [f"{pad}SetOp {node.kind.upper()}{' ALL' if node.all else ''}"]
        return out + ast_lines(node.left, depth + 1) + ast_lines(node.right, depth + 1)
    out = [f"{pad}Select{' DISTINCT' if node.distinct else ''}"]
    from ..ra.nodes import cond_str, attr_str
    items = []
    for it in node.items:
        if it.kind == "star":
            items.append("*")
        elif it.kind == "col":
            items.append(attr_str(it.attr) + (f" AS {it.alias}" if it.alias else ""))
        else:
            items.append(f"{it.func.upper()}({it.arg if it.arg == '*' else attr_str(it.arg)})" + (f" AS {it.alias}" if it.alias else ""))
    out.append(f"{pad}  items: {', '.join(items)}")
    out.append(f"{pad}  from:" + (" (JOIN chain)" if node.join_conds is not None else ""))
    for j, t in enumerate(node.tables):
        if t.kind == "table":
            out.append(f"{pad}    {t.name}" + (f" AS {t.alias}" if t.alias else "")
                       + (f" ON {_cond(node.join_conds[j - 1])}" if node.join_conds is not None and j > 0 else ""))
        else:
            out.append(f"{pad}    subquery AS {t.alias}" + (f" ON {_cond(node.join_conds[j - 1])}" if node.join_conds is not None and j > 0 else ""))
            out += ast_lines(t.query, depth + 3)
    if node.where is not None:
        out.append(f"{pad}  where: {_cond(node.where)}")
    if node.group_by:
        out.append(f"{pad}  group by: {', '.join(attr_str(a) for a in node.group_by)}")
    if node.having is not None:
        out.append(f"{pad}  having: {_cond(node.having)}")
    if node.order_by:
        out.append(f"{pad}  order by: {', '.join(attr_str(a) + ' ' + d for a, d in node.order_by)}")
    if node.limit is not None:
        out.append(f"{pad}  limit: {node.limit}")
    return out


def _cond(c):
    from ..ra.nodes import attr_str

    def operand(o):
        if o.op == "attr":
            return attr_str(o)
        if o.op == "num":
            return str(o.value)
        if o.op == "str":
            return "'" + o.value.replace("'", "''") + "'"
        return f"{o.func.upper()}({o.arg if o.arg == '*' else attr_str(o.arg)})"

    if c.op == "cmp":
        return f"{operand(c.left)} {c.cmp} {operand(c.right)}"
    if c.op == "not":
        return f"NOT ({_cond(c.child)})"
    return f"({_cond(c.left)} {c.op.upper()} {_cond(c.right)})"
