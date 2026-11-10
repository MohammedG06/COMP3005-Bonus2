"""Record format (spec section 2): concatenated field encodings, no header, no null bitmap.
INT = 4 bytes signed LE. VARCHAR(n) = u16 byte length L (L <= n) then L UTF-8 bytes."""
import struct

from ..errors import DBError
from ..schema import INT, VARCHAR, REAL

INT_MIN, INT_MAX = -2 ** 31, 2 ** 31 - 1


class RecordCodec:
    def __init__(self, columns):
        self.columns = list(columns)

    def encode(self, values):
        if len(values) != len(self.columns):
            raise DBError("data", f"expected {len(self.columns)} values but got {len(values)}")
        out = bytearray()
        for col, v in zip(self.columns, values):
            if col.type == INT:
                if isinstance(v, bool) or not isinstance(v, int):
                    raise DBError("data", f"column {col.name} is INT but got {v!r}")
                if col.length == 8:          # derived column (COUNT, SUM): stored in 8 bytes
                    out += struct.pack("<q", v)
                    continue
                if not INT_MIN <= v <= INT_MAX:
                    raise DBError("data", f"column {col.name}: {v} does not fit in 4 signed bytes")
                out += struct.pack("<i", v)
            elif col.type == REAL:           # AVG output, only in temporary files
                out += struct.pack("<d", float(v))
            else:
                if not isinstance(v, str):
                    raise DBError("data", f"column {col.name} is VARCHAR but got {v!r}")
                b = v.encode("utf-8")
                if len(b) > col.length:
                    raise DBError("data", f"column {col.name}: value is {len(b)} bytes, longer than VARCHAR({col.length})")
                out += struct.pack("<H", len(b)) + b
        return bytes(out)

    def decode(self, data):
        vals, pos = [], 0
        for col in self.columns:
            if col.type == INT:
                if col.length == 8:
                    vals.append(struct.unpack_from("<q", data, pos)[0])
                    pos += 8
                else:
                    vals.append(struct.unpack_from("<i", data, pos)[0])
                    pos += 4
            elif col.type == REAL:
                vals.append(struct.unpack_from("<d", data, pos)[0])
                pos += 8
            else:
                (n,) = struct.unpack_from("<H", data, pos)
                vals.append(data[pos + 2:pos + 2 + n].decode("utf-8"))
                pos += 2 + n
        return tuple(vals)


def parse_schema(text):
    """'a:INT,b:VARCHAR(20)' -> [Column, ...]"""
    from ..schema import Column
    cols, seen = [], set()
    for part in text.split(","):
        part = part.strip()
        if ":" not in part:
            raise DBError("schema", f"bad column definition '{part}' (expected name:TYPE)")
        name, typ = (x.strip() for x in part.split(":", 1))
        if not name or not (name[0].isalpha() or name[0] == "_") or not all(c.isalnum() or c == "_" for c in name):
            raise DBError("schema", f"bad column name '{name}'")
        if name in seen:
            raise DBError("schema", f"duplicate column name '{name}'")
        seen.add(name)
        t = typ.upper()
        if t == "INT":
            cols.append(Column(name, INT))
        elif t.startswith("VARCHAR(") and t.endswith(")") and t[8:-1].isdigit():
            n = int(t[8:-1])
            if not 1 <= n <= 255:
                raise DBError("schema", f"VARCHAR length must be between 1 and 255, got {n}")
            cols.append(Column(name, VARCHAR, n))
        else:
            raise DBError("schema", f"unknown type '{typ}' (use INT or VARCHAR(n))")
    return cols
