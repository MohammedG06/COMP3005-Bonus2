"""CSV bulk loader: no header, one record per line, double quotes around a field that contains
a comma (a doubled quote inside means one literal quote). The first bad field stops the load and
is reported with its line and column; rows before it stay loaded."""
from ..errors import DBError
from ..schema import INT


def split_csv_line(line):
    """-> list of field strings. Hand-written; handles "quoted, fields" and "" inside quotes."""
    fields, buf, i, n = [], [], 0, len(line)
    in_q = False
    while i < n:
        c = line[i]
        if in_q:
            if c == '"':
                if i + 1 < n and line[i + 1] == '"':
                    buf.append('"')
                    i += 2
                    continue
                in_q = False
            else:
                buf.append(c)
        elif c == '"' and not buf:
            in_q = True
        elif c == ",":
            fields.append("".join(buf))
            buf = []
        else:
            buf.append(c)
        i += 1
    if in_q:
        raise ValueError("unterminated quoted field")
    fields.append("".join(buf))
    return fields


def _parse_int(text):
    t = text.strip()
    body = t[1:] if t[:1] in "+-" else t
    if not body or not all("0" <= ch <= "9" for ch in body):
        return None
    return int(t)


def load_csv(db, table, path):
    heap, info = db.open_heap(table)
    cols = info.columns
    loaded = 0
    try:
        with open(path, encoding="utf-8-sig") as fh:
            for lineno, raw in enumerate(fh, 1):
                line = raw.rstrip("\r\n")
                if line == "":
                    continue
                try:
                    fields = split_csv_line(line)
                except ValueError as e:
                    raise DBError("data", f"{path}, line {lineno}: {e}")
                if len(fields) != len(cols):
                    raise DBError("data", f"{path}, line {lineno}: expected {len(cols)} fields "
                                  f"but found {len(fields)}")
                row = []
                for k, (col, text) in enumerate(zip(cols, fields), 1):
                    if col.type == INT:
                        v = _parse_int(text)
                        if v is None:
                            raise DBError("data", f"{path}, line {lineno}, column {k} ({col.name}): "
                                          f"'{text}' is not an INT")
                        row.append(v)
                    else:
                        if len(text.encode("utf-8")) > col.length:
                            raise DBError("data", f"{path}, line {lineno}, column {k} ({col.name}): value is "
                                          f"{len(text.encode('utf-8'))} bytes, longer than VARCHAR({col.length})")
                        row.append(text)
                try:
                    heap.insert(tuple(row))
                except DBError as e:
                    raise DBError(e.category, f"{path}, line {lineno}: {e.message}")
                loaded += 1
    except OSError as e:
        raise DBError("storage", f"cannot read '{path}': {e.strerror}")
    finally:
        db.refresh_counts(table, loaded)
    return loaded
