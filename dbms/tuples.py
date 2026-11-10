"""Our own definition of tuple equality (used by every duplicate-removing operator).
Two tuples are equal iff their keys are equal. A number never equals a string."""


def tuple_key(row):
    return tuple(("s", v) if isinstance(v, str) else ("n", v) for v in row)


def dedupe(rows):
    """Keep the first occurrence of every distinct tuple, in order."""
    seen = set()
    for r in rows:
        k = tuple_key(r)
        if k not in seen:
            seen.add(k)
            yield r
