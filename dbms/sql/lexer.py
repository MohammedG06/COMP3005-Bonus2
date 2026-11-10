"""SQL scanner (hand written, no regex). Keywords are case insensitive, identifiers are not.
Tokens: keyword (type = the upper case keyword), IDENT, NUM (integer, optional sign), STR, and the
symbols ( ) , . * ; = <> < <= > >=   ('!=' is accepted as another spelling of '<>')."""
from ..errors import DBError
from ..ra.tokenizer import Token

KEYWORDS = {"CREATE", "TABLE", "INSERT", "INTO", "VALUES", "SELECT", "DISTINCT", "FROM", "WHERE", "GROUP",
            "BY", "HAVING", "ORDER", "LIMIT", "UNION", "ALL", "INTERSECT", "EXCEPT", "JOIN", "ON", "AS",
            "AND", "OR", "NOT", "ASC", "DESC", "INT", "VARCHAR", "COUNT", "SUM", "AVG", "MIN", "MAX"}
AGG_FUNCS = ("COUNT", "SUM", "AVG", "MIN", "MAX")


def _letter(c):
    return c.isalpha() or c == "_"


def _digit(c):
    return "0" <= c <= "9"


def lex(src):
    toks, i, n = [], 0, len(src)
    while i < n:
        c = src[i]
        if c in " \t\r\n":
            i += 1
            continue
        if c == "-" and i + 1 < n and src[i + 1] == "-":          # comment to end of line
            while i < n and src[i] != "\n":
                i += 1
            continue
        start = i
        if _letter(c):
            i += 1
            while i < n and (_letter(src[i]) or _digit(src[i])):
                i += 1
            word = src[start:i]
            if word.upper() in KEYWORDS:
                toks.append(Token(word.upper(), word, word.upper(), start))
            else:
                toks.append(Token("IDENT", word, word, start))
        elif _digit(c) or (c in "+-" and i + 1 < n and _digit(src[i + 1])):
            i += 1
            while i < n and _digit(src[i]):
                i += 1
            if i < n and (_letter(src[i]) or src[i] == "."):
                raise DBError("lexical", "malformed number (only integers are supported)", start)
            toks.append(Token("NUM", src[start:i], int(src[start:i]), start))
        elif c == "'":
            i += 1
            buf = []
            while True:
                if i >= n:
                    raise DBError("lexical", "unterminated string (missing closing quote)", start)
                if src[i] == "'":
                    if i + 1 < n and src[i + 1] == "'":
                        buf.append("'")
                        i += 2
                        continue
                    i += 1
                    break
                buf.append(src[i])
                i += 1
            toks.append(Token("STR", src[start:i], "".join(buf), start))
        elif c == "<":
            if i + 1 < n and src[i + 1] in "=>":
                i += 2
            else:
                i += 1
            toks.append(Token(src[start:i], src[start:i], None, start))
        elif c == ">":
            i += 2 if (i + 1 < n and src[i + 1] == "=") else 1
            toks.append(Token(src[start:i], src[start:i], None, start))
        elif c == "!":
            if i + 1 < n and src[i + 1] == "=":
                i += 2
                toks.append(Token("<>", "!=", None, start))
            else:
                raise DBError("lexical", "'!' must be followed by '='", start)
        elif c in "(),.*;=":
            i += 1
            toks.append(Token(c, c, None, start))
        else:
            raise DBError("lexical", f"unexpected character {c!r}", start)
    toks.append(Token("EOF", "", None, n))
    return toks
