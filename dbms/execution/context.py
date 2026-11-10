"""Execution context: the memory budget, temporary files, page packing, deterministic hashing.

MEMORY MODEL (see DESIGN_M2.md)
  * The buffer pool has exactly B frames. That is the whole memory of the plan.
  * A scan pins one pool frame at a time (a real pinned page).
  * Every other operator takes its working memory with pool.lease(n): the frames are taken out
    of circulation, so pinned + leased can never exceed B (the pool refuses).
  * Tuples kept in memory are counted in pages with the same packing arithmetic as a slotted page
    (PageCounter), and an operator never keeps more tuples than its leased frames can hold.
  * Temporary files are written and read through DiskManager one page at a time, and every page
    transfer is added to the page_reads / page_writes of the operator that owns the file.
"""
import os
import zlib

from ..errors import DBError
from ..schema import INT, REAL, VARCHAR
from ..storage import page as pg
from ..storage.constants import PAGE_SIZE, PAGE_HEADER, SLOT_SIZE
from ..storage.disk import DiskManager
from ..storage.heap import HeapFile
from ..storage.records import RecordCodec

PAGE_ROOM = PAGE_SIZE - PAGE_HEADER          # 4088 bytes available for records + slots on a page


def make_sizer(columns):
    """Encoded size in bytes of one tuple (same arithmetic as RecordCodec, without encoding)."""
    fixed, var = 0, []
    for i, c in enumerate(columns):
        if c.type == INT:
            fixed += 8 if c.length == 8 else 4
        elif c.type == REAL:
            fixed += 8
        else:
            var.append(i)
    if not var:
        return lambda t: fixed
    return lambda t: fixed + sum(2 + (len(t[i]) if t[i].isascii() else len(t[i].encode("utf-8"))) for i in var)


class PageCounter:
    """Counts how many 4 KB slotted pages a growing list of tuples occupies (sequential packing)."""

    def __init__(self, sizer, capacity_pages):
        self.sizer = sizer
        self.capacity = capacity_pages
        self.pages = 0
        self.free = 0

    def try_add(self, t):
        need = self.sizer(t) + SLOT_SIZE
        if need > PAGE_ROOM:
            raise DBError("data", f"a tuple of {need - SLOT_SIZE} bytes does not fit on one page")
        if need > self.free:
            if self.pages >= self.capacity:
                return False
            self.pages += 1
            self.free = PAGE_ROOM
        self.free -= need
        return True

    def reset(self):
        self.pages = 0
        self.free = 0


def det_hash(key, seed=0):
    """Deterministic hash of a key tuple (Python's hash() of str changes between runs, which
    would change partition sizes and therefore the counters)."""
    parts = []
    for v in key:
        parts.append((str(v) + "\x00") if not isinstance(v, str) else (v + "\x01"))
    return zlib.crc32("".join(parts).encode("utf-8"), (seed * 2654435761 + 12345) & 0xFFFFFFFF)


class TempFile:
    """A temporary page file owned by one operator; every transfer is added to the owner's counters."""

    def __init__(self, ctx, owner):
        self.ctx = ctx
        self.owner = owner
        self.path = ctx.new_temp_path()
        self.disk = DiskManager(self.path, create=True)
        ctx.temps.add(self)

    @property
    def num_pages(self):
        return self.disk.num_pages()

    def append_page(self, data):
        pid = self.disk.allocate_page()
        self.disk.write_page(pid, data)
        self.owner.page_writes += 1
        return pid

    def read_page(self, pid):
        data = self.disk.read_page(pid)
        self.owner.page_reads += 1
        return data

    def delete(self):
        if self in self.ctx.temps:
            self.disk.delete()
            self.ctx.temps.discard(self)


class TupleWriter:
    """Packs tuples into slotted pages and appends full pages to a temp file."""

    def __init__(self, tf, codec):
        self.tf, self.codec = tf, codec
        self.recs, self.free = [], PAGE_ROOM
        self.count = 0

    def add(self, t):
        rec = self.codec.encode(t)
        need = len(rec) + SLOT_SIZE
        if need > PAGE_ROOM:
            raise DBError("data", f"a tuple of {len(rec)} bytes does not fit on one page")
        if need > self.free:
            self.flush()
        self.recs.append(rec)
        self.free -= need
        self.count += 1

    def flush(self):
        if not self.recs:
            return
        buf = bytearray(PAGE_SIZE)
        pg.init_page(buf, self.tf.num_pages)
        for rec in self.recs:
            pg.insert(buf, rec)
        self.tf.append_page(bytes(buf))
        self.recs, self.free = [], PAGE_ROOM

    def close(self):
        self.flush()


def read_tuples(tf, codec):
    """Generator over a temp file, one page at a time (one frame)."""
    dec = codec.decode
    for pid in range(tf.num_pages):
        buf = bytearray(tf.read_page(pid))
        for _, rec in pg.live_slots(buf):
            yield dec(rec)


class ExecContext:
    def __init__(self, db, frames):
        self.db = db
        self.pool = db.pool
        self.pool.flush_all()                 # scans read through private page namespaces, i.e. from disk
        self.B = frames
        self.reserve = 0                      # 1 while a consumer is draining a child into a temp file
        self.temps = set()
        self._n = 0
        self._fid = 1000
        self._tmp_dir = os.path.join(db.path, "tmp")
        self.scans = {}                       # fid -> file name, for cleanup
        self.operators = []

    @property
    def frames(self):
        """Frames the operator currently being opened may use for its whole subtree."""
        return self.B - self.reserve

    # ---- leases -------------------------------------------------------------------------------
    def lease(self, n, who):
        if n <= 0:
            return []
        try:
            return self.pool.lease(n)
        except DBError as e:
            raise DBError("plan", f"{who} needs {n} more frames but the budget B = {self.B} is exhausted "
                          f"(raise --frames)") from e

    def release(self, idxs):
        if idxs:
            self.pool.release(idxs)

    # ---- temp files -----------------------------------------------------------------------------
    def new_temp_path(self):
        os.makedirs(self._tmp_dir, exist_ok=True)
        self._n += 1
        return os.path.join(self._tmp_dir, f"t{self._n}.tmp")

    def cleanup(self):
        for tf in list(self.temps):
            tf.delete()
        try:
            if os.path.isdir(self._tmp_dir) and not os.listdir(self._tmp_dir):
                os.rmdir(self._tmp_dir)
        except OSError:
            pass

    # ---- scans get a private page namespace so no two scans ever share cached pages ---------------
    def open_scan_heap(self, table):
        heap, info = self.db.open_heap(table)
        disk = self.db._disks[self.db._file_ids[table]]
        self._fid += 1
        fid = self._fid
        self.pool.register_disk(fid, disk)
        self.scans[fid] = table
        return HeapFile(self.pool, fid, table, RecordCodec(info.columns)), info

    def close_scan_heap(self, heap):
        self.pool.drop_file(heap.file_id)
        self.pool.disks.pop(heap.file_id, None)
        self.scans.pop(heap.file_id, None)


def codec_for(schema):
    return RecordCodec(list(schema))
