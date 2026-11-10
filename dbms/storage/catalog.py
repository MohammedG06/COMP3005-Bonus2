"""Catalog: stored in catalog.db as a heap file of raw records, read through the buffer pool.

record kind byte first, so Module 4 can add statistics records next to the table records:
  kind 1  TABLE : name, data file, first_page u32, num_pages u32 (data pages only),
                  num_tuples u64, ncols u16, then per column: name, type u8 (1 INT, 2 VARCHAR), length u16
  kind 2  reserved for column statistics (Module 4)
strings are  u8 length + UTF-8 bytes.
To change a table's counts the old record is deleted and a new one inserted (slots are never reused).
"""
import struct

from ..errors import DBError
from ..schema import Column, Schema, INT, VARCHAR

KIND_TABLE = 1
KIND_COL_STATS = 2      # one per (table, column): ndv, average width, min, max
KIND_TABLE_STATS = 3    # one per table: tuples, data pages, average record width
KIND_BUCKET = 4         # one per histogram bucket: (table, column, index, lo, hi, count, ndv)


def _put_str(out, s):
    b = s.encode("utf-8")
    out += struct.pack("<B", len(b)) + b


def _get_str(data, pos):
    n = data[pos]
    return data[pos + 1:pos + 1 + n].decode("utf-8"), pos + 1 + n


class TableInfo:
    def __init__(self, name, columns, file, first_page=0, num_pages=0, num_tuples=0):
        self.name = name
        self.columns = list(columns)
        self.file = file
        self.first_page = first_page
        self.num_pages = num_pages        # data pages, directory pages excluded
        self.num_tuples = num_tuples

    @property
    def schema(self):
        return Schema(self.columns)

    def to_dict(self):
        return {"name": self.name, "schema": [c.to_json(False) for c in self.columns],
                "first_page": self.first_page, "num_pages": self.num_pages,
                "num_tuples": self.num_tuples}


def encode_table(t):
    out = bytearray([KIND_TABLE])
    _put_str(out, t.name)
    _put_str(out, t.file)
    out += struct.pack("<IIQH", t.first_page, t.num_pages, t.num_tuples, len(t.columns))
    for c in t.columns:
        _put_str(out, c.name)
        out += struct.pack("<BH", 1 if c.type == INT else 2, c.length or 0)
    return bytes(out)


def decode_table(data):
    name, pos = _get_str(data, 1)
    file, pos = _get_str(data, pos)
    first, npages, ntuples, ncols = struct.unpack_from("<IIQH", data, pos)
    pos += struct.calcsize("<IIQH")
    cols = []
    for _ in range(ncols):
        cname, pos = _get_str(data, pos)
        t, length = struct.unpack_from("<BH", data, pos)
        pos += 3
        cols.append(Column(cname, INT) if t == 1 else Column(cname, VARCHAR, length))
    return TableInfo(name, cols, file, first, npages, ntuples)


def _put_val(out, v):
    if isinstance(v, str):
        b = v.encode("utf-8")
        out += struct.pack("<BH", 2, len(b)) + b
    else:
        out += struct.pack("<Bq", 1, v)


def _get_val(data, pos):
    if data[pos] == 1:
        return struct.unpack_from("<q", data, pos + 1)[0], pos + 9
    (n,) = struct.unpack_from("<H", data, pos + 1)
    return data[pos + 3:pos + 3 + n].decode("utf-8"), pos + 3 + n


class Catalog:
    def __init__(self, heap):
        self.heap = heap

    def _tables(self):
        for rid, rec in self.heap.scan_bytes():
            if rec[0] == KIND_TABLE:
                yield rid, decode_table(rec)

    def _find(self, name):
        for rid, info in self._tables():
            if info.name == name:
                return rid, info
        return None, None

    def create_table(self, name, columns, file=None):
        if self._find(name)[1] is not None:
            raise DBError("schema", f"table '{name}' already exists")
        info = TableInfo(name, columns, file or f"{name}.db")
        self.heap.insert_bytes(encode_table(info))
        return info

    def get_table(self, name):
        info = self._find(name)[1]
        if info is None:
            raise DBError("name", f"unknown table '{name}'")
        return info

    def list_tables(self):
        return sorted(info.name for _, info in self._tables())

    def update_counts(self, name, num_pages, num_tuples):
        rid, info = self._find(name)
        if info is None:
            raise DBError("name", f"unknown table '{name}'")
        info.num_pages, info.num_tuples = num_pages, num_tuples
        self._replace(lambda r, rec: encode_table(info) if r == rid else rec)

    # ---- statistics (Module 4) ---------------------------------------------------------------
    def _replace(self, fn, extra=()):
        """Rewrite the catalog file from its live records. fn(rid, rec) returns the new record, or None to drop it.
        Reusing the same pages keeps catalog.db from growing every time statistics or counts change."""
        live = [(rid, rec) for rid, rec in self.heap.scan_bytes()]
        out = [new for new in (fn(rid, rec) for rid, rec in live) if new is not None]
        self.heap.rewrite_all(out + list(extra))

    def clear_stats(self, table):
        self._replace(lambda rid, rec: None if rec[0] in (KIND_COL_STATS, KIND_TABLE_STATS, KIND_BUCKET)
                      and _get_str(rec, 1)[0] == table else rec)

    def put_stats(self, ts):
        """ts: dbms.optimizer.stats.TableStats. Replaces any earlier statistics of the table."""
        recs = []
        rec = bytearray([KIND_TABLE_STATS])
        _put_str(rec, ts.table)
        rec += struct.pack("<QId", ts.n_tuples, ts.n_pages, ts.width)
        recs.append(bytes(rec))
        for cs in ts.cols.values():
            rec = bytearray([KIND_COL_STATS])
            _put_str(rec, ts.table)
            _put_str(rec, cs.name)
            rec += struct.pack("<QdB", cs.ndv, cs.avg_width, 1 if cs.min is not None else 0)
            if cs.min is not None:
                _put_val(rec, cs.min)
                _put_val(rec, cs.max)
            recs.append(bytes(rec))
            for i, b in enumerate(cs.buckets):
                rec = bytearray([KIND_BUCKET])
                _put_str(rec, ts.table)
                _put_str(rec, cs.name)
                rec += struct.pack("<H", i)
                _put_val(rec, b.lo)
                _put_val(rec, b.hi)
                rec += struct.pack("<QQ", b.count, b.ndv)
                recs.append(bytes(rec))
        mine = (KIND_COL_STATS, KIND_TABLE_STATS, KIND_BUCKET)
        self._replace(lambda rid, r: None if r[0] in mine and _get_str(r, 1)[0] == ts.table else r, extra=recs)

    def get_stats(self, table):
        from ..optimizer.stats import TableStats, ColStats, Bucket
        info = self._find(table)[1]
        if info is None:
            return None
        ts, cols, buckets = None, {}, {}
        for _, rec in self.heap.scan_bytes():
            k = rec[0]
            if k not in (KIND_COL_STATS, KIND_TABLE_STATS, KIND_BUCKET):
                continue
            name, pos = _get_str(rec, 1)
            if name != table:
                continue
            if k == KIND_TABLE_STATS:
                n, pages, width = struct.unpack_from("<QId", rec, pos)
                ts = TableStats(table, n, pages, width, {})
            elif k == KIND_COL_STATS:
                cname, pos = _get_str(rec, pos)
                ndv, avgw, has = struct.unpack_from("<QdB", rec, pos)
                pos += struct.calcsize("<QdB")
                lo = hi = None
                if has:
                    lo, pos = _get_val(rec, pos)
                    hi, pos = _get_val(rec, pos)
                col = next((c for c in info.columns if c.name == cname), None)
                cols[cname] = ColStats(cname, col.type if col else "INT", ndv, lo, hi, avgw, [])
            else:
                cname, pos = _get_str(rec, pos)
                (idx,) = struct.unpack_from("<H", rec, pos)
                pos += 2
                lo, pos = _get_val(rec, pos)
                hi, pos = _get_val(rec, pos)
                count, ndv = struct.unpack_from("<QQ", rec, pos)
                buckets.setdefault(cname, []).append((idx, Bucket(lo, hi, count, ndv)))
        if ts is None:
            return None
        for cname, cs in cols.items():
            cs.buckets = [b for _, b in sorted(buckets.get(cname, []), key=lambda x: x[0])]
        ts.cols = {c.name: cols[c.name] for c in info.columns if c.name in cols}
        return ts
