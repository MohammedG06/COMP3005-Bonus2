"""In-memory tables and the Bonus-1 style relation-definition loader (text, no file access here).
A 'provider' is anything with has_table(name), table_schema(name) and table_rows(name);
the evaluator only talks to that interface, never to a dict of lists."""
from ..errors import DBError
from ..schema import Column, Schema, INT, VARCHAR, REAL
from ..tuples import dedupe


class InMemoryProvider:
    def __init__(self):
        self._t = {}

    def add(self, name, schema, rows):
        self._t[name] = (schema, rows)

    def names(self):
        return sorted(self._t)

    def has_table(self, name):
        return name in self._t

    def table_schema(self, name):
        return self._t[name][0]

    def table_rows(self, name):
        return iter(self._t[name][1])


def _err(source, lineno, msg):
    return DBError("data", f"{source}, line {lineno}: {msg}")


def _number_or_none(text):
    i, n = 0, len(text)
    if i < n and text[i] == "-":
        i += 1
    d0 = i
    while i < n and "0" <= text[i] <= "9":
        i += 1
    if i == d0:
        return None
    if i < n and text[i] == ".":
        i += 1
        d1 = i
        while i < n and "0" <= text[i] <= "9":
            i += 1
        if i == d1:
            return None
    if i != n:
        return None
    return float(text) if "." in text else int(text)


def _skip_ws(s, i):
    while i < len(s) and s[i] in " \t":
        i += 1
    return i


def _read_ident(s, i):
    j = i
    while j < len(s) and (s[j].isalpha() or s[j] == "_" or ("0" <= s[j] <= "9" and j > i)):
        j += 1
    return s[i:j], j


def _parse_header(s, lineno, source):
    name, i = _read_ident(s, 0)
    if not name:
        raise _err(source, lineno, "expected a relation name")
    i = _skip_ws(s, i)
    if i >= len(s) or s[i] != "(":
        raise _err(source, lineno, "expected '(' after relation name")
    i += 1
    attrs = []
    while True:
        i = _skip_ws(s, i)
        a, i = _read_ident(s, i)
        if not a:
            raise _err(source, lineno, "expected an attribute name")
        attrs.append(a)
        i = _skip_ws(s, i)
        if i < len(s) and s[i] == ",":
            i += 1
            continue
        if i < len(s) and s[i] == ")":
            i += 1
            break
        raise _err(source, lineno, "expected ',' or ')' in attribute list")
    if len(set(attrs)) != len(attrs):
        raise _err(source, lineno, f"relation {name} has a repeated attribute name")
    i = _skip_ws(s, i)
    if i >= len(s) or s[i] != "=":
        raise _err(source, lineno, "expected '=' after attribute list")
    i = _skip_ws(s, i + 1)
    if i >= len(s) or s[i] != "{":
        raise _err(source, lineno, "expected '{'")
    return name, attrs, s[i + 1:].strip()


def _split_values(s, lineno, source):
    vals, i, n = [], 0, len(s)
    while True:
        i = _skip_ws(s, i)
        if i < n and s[i] == "'":
            i += 1
            buf = []
            while True:
                if i >= n:
                    raise _err(source, lineno, "unterminated quoted string")
                if s[i] == "'":
                    if i + 1 < n and s[i + 1] == "'":
                        buf.append("'")
                        i += 2
                        continue
                    i += 1
                    break
                buf.append(s[i])
                i += 1
            vals.append("".join(buf))
        else:
            j = i
            while j < n and s[j] != ",":
                j += 1
            raw = s[i:j].strip()
            if raw == "":
                raise _err(source, lineno, "empty value")
            for ch in raw:
                if ch in " \t()'":
                    raise _err(source, lineno, f"value {raw!r} must be quoted (it contains a space, parenthesis or quote)")
            num = _number_or_none(raw)
            vals.append(num if num is not None else raw)
            i = j
        i = _skip_ws(s, i)
        if i >= n:
            return vals
        if s[i] == ",":
            i += 1
            continue
        raise _err(source, lineno, "expected ',' between values")


def _column_type(values, name, relname):
    kinds = {"VARCHAR" if isinstance(v, str) else ("REAL" if isinstance(v, float) else "INT") for v in values}
    if "VARCHAR" in kinds and len(kinds) > 1:
        raise DBError("data", f"column {name} of {relname} mixes numbers and strings")
    if not kinds:
        return INT
    return REAL if "REAL" in kinds else (VARCHAR if "VARCHAR" in kinds else INT)


def load_relations(text, source="<data>"):
    """Parse relation definitions into an InMemoryProvider. Bag semantics are NOT applied here:
    a relation written in a data file is a set, so duplicate tuples collapse (Bonus 1 rule)."""
    prov = InMemoryProvider()
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        lineno = i + 1
        i += 1
        if s == "" or s.startswith("//"):
            continue
        name, attrs, rest = _parse_header(s, lineno, source)
        if prov.has_table(name):
            raise _err(source, lineno, f"relation {name} is defined twice")
        rows = []
        if rest != "}":
            if rest != "":
                raise _err(source, lineno, "nothing may follow '{' on the header line")
            closed = False
            while i < len(lines):
                s2 = lines[i].strip()
                ln = i + 1
                i += 1
                if s2 == "" or s2.startswith("//"):
                    continue
                if s2 == "}":
                    closed = True
                    break
                vals = _split_values(s2, ln, source)
                if len(vals) != len(attrs):
                    raise _err(source, ln, f"expected {len(attrs)} values for {name} but found {len(vals)}")
                rows.append(tuple(vals))
            if not closed:
                raise _err(source, lineno, f"relation {name} is missing its closing '}}'")
        rows = list(dedupe(rows))
        cols = [Column(a, _column_type([r[k] for r in rows], a, name)) for k, a in enumerate(attrs)]
        prov.add(name, Schema(cols), rows)
    return prov
