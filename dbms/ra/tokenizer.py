"""Hand-written scanner (no regex). Same design as Bonus 1, plus the '<>' operator.

Token types: IDENT, NUM (value int or float), STR (value decoded), EOF, and one type per
symbol equal to its text:  ( ) [ ] , . = != <> < <= > >=
Maximal munch: after '<' look at the next char for '=' or '>'; after '>' look for '='.
"""
from ..errors import DBError


class Token:
    __slots__ = ("type", "text", "value", "pos")

    def __init__(self, type_, text, value, pos):
        self.type, self.text, self.value, self.pos = type_, text, value, pos

    def __repr__(self):
        return f"{self.type}({self.text!r}@{self.pos})"


def _is_letter(c):
    return c.isalpha() or c == "_"


def _is_digit(c):
    return "0" <= c <= "9"


def tokenize(src):
    toks, i, n = [], 0, len(src)
    while i < n:
        c = src[i]
        if c in " \t\r\n":
            i += 1
            continue
        start = i
        if _is_letter(c):
            i += 1
            while i < n and (_is_letter(src[i]) or _is_digit(src[i])):
                i += 1
            text = src[start:i]
            toks.append(Token("IDENT", text, text, start))
        elif _is_digit(c) or (c == "-" and i + 1 < n and _is_digit(src[i + 1])):
            if c == "-":
                i += 1
            while i < n and _is_digit(src[i]):
                i += 1
            if i + 1 < n and src[i] == "." and _is_digit(src[i + 1]):
                i += 1
                while i < n and _is_digit(src[i]):
                    i += 1
            if i < n and _is_letter(src[i]):
                raise DBError("lexical", f"malformed number: digits followed directly by {src[i]!r}", start)
            text = src[start:i]
            toks.append(Token("NUM", text, float(text) if "." in text else int(text), start))
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
        elif c == "!":
            if i + 1 < n and src[i + 1] == "=":
                i += 2
                toks.append(Token("!=", "!=", None, start))
            else:
                raise DBError("lexical", "'!' must be followed by '=' (the only operator starting with '!' is '!=')", start)
        elif c == "<":
            if i + 1 < n and src[i + 1] in "=>":
                i += 2
            else:
                i += 1
            toks.append(Token(src[start:i], src[start:i], None, start))
        elif c == ">":
            i += 2 if (i + 1 < n and src[i + 1] == "=") else 1
            toks.append(Token(src[start:i], src[start:i], None, start))
        elif c in "()[],.=":
            i += 1
            toks.append(Token(c, c, None, start))
        else:
            raise DBError("lexical", f"unexpected character {c!r}", start)
    toks.append(Token("EOF", "", None, n))
    return toks
