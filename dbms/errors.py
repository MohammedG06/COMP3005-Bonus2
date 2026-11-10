"""One error type for the whole system, with the categories the specs ask for.

categories: lexical, syntax, name, ambiguity, type, aggregate, schema   (M3 list)
            data, plan, storage                                         (M1/M2)
pos is a 0-based character offset into the text being parsed (None if not applicable).
"""


class DBError(Exception):
    def __init__(self, category, message, pos=None):
        super().__init__(message)
        self.category = category
        self.message = message
        self.pos = pos

    def line_col(self, source):
        """1-based (line, column) of pos inside source."""
        pos = min(self.pos or 0, len(source))
        line = source.count("\n", 0, pos) + 1
        col = pos - (source.rfind("\n", 0, pos) + 1) + 1
        return line, col


def format_error(err, source=None):
    """M3 format:  error: <category>: <message> at line L, column C"""
    if err.pos is not None and source is not None:
        line, col = err.line_col(source)
        return f"error: {err.category}: {err.message} at line {line}, column {col}"
    return f"error: {err.category}: {err.message}"


def format_error_caret(err, source):
    """Bonus-1 style: message plus the offending line with a caret under the position."""
    if err.pos is None or source is None:
        return format_error(err)
    pos = min(err.pos, len(source))
    ls = source.rfind("\n", 0, pos) + 1
    le = source.find("\n", pos)
    if le == -1:
        le = len(source)
    line, col = err.line_col(source)
    return (f"error: {err.category}: {err.message} at line {line}, column {col}\n"
            f"  {source[ls:le]}\n  {' ' * (pos - ls)}^")
