"""The condition parser, usable on its own:

    cond = parse_condition("R.a > 500 and R.b = 'cs'")      # text -> tree
    text = unparse(cond)                                    # tree -> text (round-trips)
    fn   = compile_condition(cond, schema)                  # tree -> fn(left_row, right_row)

Grammar (also in GRAMMAR.md):
  cond    ::= andcond { "or" andcond }
  andcond ::= notcond { "and" notcond }
  notcond ::= "not" notcond | "(" cond ")" | comparison
  comparison ::= operand CMP operand       CMP ::= = <> != < <= > >=
  operand ::= NUMBER | STRING | attrref     attrref ::= IDENT [ "." IDENT ]
and / or / not are case-insensitive keywords, recognised only where the grammar expects one.
'!=' is stored as '<>'.
"""
import operator

from ..errors import DBError
from ..schema import INT, VARCHAR, REAL, comparable
from .nodes import Node, attr_str
from .tokenizer import tokenize

CMP_TOKENS = ("=", "!=", "<>", "<", "<=", ">", ">=")


class TokenStream:
    def __init__(self, source):
        self.src = source
        self.toks = tokenize(source)
        self.i = 0

    def peek(self, k=0):
        return self.toks[min(self.i + k, len(self.toks) - 1)]

    def advance(self):
        t = self.toks[self.i]
        if t.type != "EOF":
            self.i += 1
        return t

    @staticmethod
    def describe(t):
        if t.type == "EOF":
            return "end of input"
        if t.type == "STR":
            return f"string {t.text}"
        return f"'{t.text}'"

    def fail(self, msg, tok=None):
        raise DBError("syntax", msg, (tok or self.peek()).pos)

    def is_word(self, *words):
        t = self.peek()
        return t.type == "IDENT" and t.text in words

    def is_ci(self, *words):
        t = self.peek()
        return t.type == "IDENT" and t.text.lower() in words

    def expect(self, ttype, opener=None):
        t = self.peek()
        if t.type == ttype:
            return self.advance()
        if opener is not None:
            self.fail(f"missing '{ttype}': the '{opener.text}' at position {opener.pos} is never closed "
                      f"(found {self.describe(t)})")
        self.fail(f"expected '{ttype}' but found {self.describe(t)}")

    def expect_ident(self, what):
        t = self.peek()
        if t.type != "IDENT":
            self.fail(f"expected {what} but found {self.describe(t)}")
        return self.advance()


class ConditionParser(TokenStream):
    def parse_cond(self):
        left = self.parse_and()
        while self.is_ci("or"):
            op = self.advance()
            left = Node("or", op.pos, left=left, right=self.parse_and())
        return left

    def parse_and(self):
        left = self.parse_not()
        while self.is_ci("and"):
            op = self.advance()
            left = Node("and", op.pos, left=left, right=self.parse_not())
        return left

    def parse_not(self):
        t = self.peek()
        # 'not' is the operator unless used as an attribute name: followed by a comparison or '.'
        if t.type == "IDENT" and t.text.lower() == "not" and self.peek(1).type not in CMP_TOKENS + (".",):
            self.advance()
            return Node("not", t.pos, child=self.parse_not())
        if t.type == "(":
            lp = self.advance()
            c = self.parse_cond()
            self.expect(")", lp)
            return c
        return self.parse_comparison()

    def parse_comparison(self):
        left = self.parse_operand()
        t = self.peek()
        if t.type not in CMP_TOKENS:
            self.fail(f"expected a comparison operator (=, <>, <, <=, >, >=) but found {self.describe(t)}")
        self.advance()
        right = self.parse_operand()
        return Node("cmp", left.pos, cmp="<>" if t.type == "!=" else t.type, left=left, right=right)

    def parse_operand(self):
        t = self.peek()
        if t.type == "NUM":
            self.advance()
            return Node("num", t.pos, value=t.value)
        if t.type == "STR":
            self.advance()
            return Node("str", t.pos, value=t.value)
        if t.type == "IDENT":
            return self.parse_attrref()
        self.fail(f"expected an operand (number, quoted string or attribute name) but found {self.describe(t)}")

    def parse_attrref(self):
        first = self.expect_ident("an attribute name")
        if self.peek().type == ".":
            self.advance()
            second = self.expect_ident("an attribute name after '.'")
            return Node("attr", first.pos, qual=first.text, name=second.text)
        return Node("attr", first.pos, qual=None, name=first.text)


def parse_condition(text):
    p = ConditionParser(text)
    c = p.parse_cond()
    if p.peek().type != "EOF":
        p.fail(f"unexpected {p.describe(p.peek())} after the condition")
    return c


def parse_attr(text):
    """'R.a' or 'a' -> attr node. Used for column lists in plan JSON."""
    p = ConditionParser(text)
    a = p.parse_attrref()
    if p.peek().type != "EOF":
        p.fail(f"unexpected {p.describe(p.peek())} in column name")
    return a


# ---- unparse ---------------------------------------------------------------
def _operand_text(o):
    if o.op == "attr":
        return attr_str(o)
    if o.op == "num":
        return repr(o.value)
    return "'" + o.value.replace("'", "''") + "'"


def _u(c):
    """-> (text, precedence)  or=1 and=2 not=3 cmp=4"""
    if c.op == "cmp":
        return f"{_operand_text(c.left)} {c.cmp} {_operand_text(c.right)}", 4
    if c.op == "not":
        t, p = _u(c.child)
        return "not " + (t if p >= 3 else f"({t})"), 3
    prec = 1 if c.op == "or" else 2
    lt, lp = _u(c.left)
    rt, rp = _u(c.right)
    if lp < prec:
        lt = f"({lt})"
    if rp <= prec:
        rt = f"({rt})"
    return f"{lt} {c.op} {rt}", prec


def unparse(c):
    return _u(c)[0]


# ---- compile ---------------------------------------------------------------
OPS = {"=": operator.eq, "<>": operator.ne, "<": operator.lt,
       "<=": operator.le, ">": operator.gt, ">=": operator.ge}


def resolve(schema, attr):
    """Index of the one column the reference names. Errors: unknown (name), ambiguous (ambiguity)."""
    label = attr_str(attr)
    hits = schema.find(attr.qual, attr.name)
    if not hits:
        raise DBError("name", f"unknown column '{label}' (available: {', '.join(schema.qnames())})", attr.pos)
    if len(hits) > 1:
        raise DBError("ambiguity", f"column '{label}' is ambiguous; candidates: "
                      f"{', '.join(schema[i].qname for i in hits)}", attr.pos)
    return hits[0]


def _tname(t):
    return {INT: "INT", VARCHAR: "VARCHAR", REAL: "REAL"}[t]


def compile_condition(cond, schema, nleft=None):
    """-> fn(a, b). Rows are tuples. For a join, a is the left row and b the right row and
    nleft is the number of left columns; for a filter b is None and nleft is len(schema).
    Attribute positions are resolved once, here, not per tuple."""
    nl = len(schema) if nleft is None else nleft
    op = cond.op
    if op in ("and", "or"):
        f, g = compile_condition(cond.left, schema, nl), compile_condition(cond.right, schema, nl)
        if op == "and":
            return lambda a, b: f(a, b) and g(a, b)
        return lambda a, b: f(a, b) or g(a, b)
    if op == "not":
        f = compile_condition(cond.child, schema, nl)
        return lambda a, b: not f(a, b)

    def operand(o):
        if o.op == "attr":
            i = resolve(schema, o)
            return ("attr", i, schema[i].type)
        if o.op == "num":
            return ("const", o.value, INT if isinstance(o.value, int) else REAL)
        return ("const", o.value, VARCHAR)

    L, R = operand(cond.left), operand(cond.right)
    if not comparable(L[2], R[2]):
        raise DBError("type", f"cannot compare {_tname(L[2])} with {_tname(R[2])} "
                      f"({_operand_text(cond.left)} {cond.cmp} {_operand_text(cond.right)})", cond.pos)
    opf = OPS[cond.cmp]
    if L[0] == "attr" and R[0] == "attr":
        i, j = L[1], R[1]
        if i < nl and j >= nl:
            j -= nl
            return lambda a, b: opf(a[i], b[j])
        if i >= nl and j < nl:
            i -= nl
            return lambda a, b: opf(b[i], a[j])
        if i < nl:
            return lambda a, b: opf(a[i], a[j])
        i -= nl
        j -= nl
        return lambda a, b: opf(b[i], b[j])
    if L[0] == "attr":
        i, v = L[1], R[1]
        if i < nl:
            return lambda a, b: opf(a[i], v)
        i -= nl
        return lambda a, b: opf(b[i], v)
    if R[0] == "attr":
        i, v = R[1], L[1]
        if i < nl:
            return lambda a, b: opf(v, a[i])
        i -= nl
        return lambda a, b: opf(v, b[i])
    const = opf(L[1], R[1])
    return lambda a, b: const
