"""Heap file (spec section 4): page 0 is the first directory page; directory pages are a linked
list, each listing data pages with their free bytes. Insert is first fit over the directory.

directory page:  page_id u32 | next_dir_page u32 | entry_count u16 | entries of (data_page u32, free_bytes u16)

Decisions (documented in DESIGN_M1.md):
 * no compaction, so a delete never changes a page's free space and the directory entry needs no update
 * scan copies a directory page's entries and unpins it at once, so a scan pins ONE page at a time
"""
import struct

from ..errors import DBError
from . import page as pg
from .constants import (PAGE_SIZE, SLOT_SIZE, NO_PAGE, NO_SPACE, DIR_HEADER, DIR_ENTRY,
                        DIR_CAPACITY, MAX_RECORD)

_DIR_HDR = struct.Struct("<IIH")


class RID(tuple):
    """(page_id, slot_no)"""
    def __new__(cls, page_id, slot):
        return super().__new__(cls, (page_id, slot))

    def __str__(self):
        return f"({self[0]},{self[1]})"


def init_dir_page(buf, page_id):
    buf[:] = bytes(PAGE_SIZE)
    _DIR_HDR.pack_into(buf, 0, page_id, NO_PAGE, 0)


def dir_entries(buf):
    """-> (next_dir_page, [(data_page_id, free_bytes), ...])"""
    _, nxt, cnt = _DIR_HDR.unpack_from(buf, 0)
    if cnt == 0:
        return nxt, []
    vals = struct.unpack_from("<" + "IH" * cnt, buf, DIR_HEADER)
    return nxt, [(vals[2 * i], vals[2 * i + 1]) for i in range(cnt)]


class HeapFile:
    def __init__(self, pool, file_id, name, codec=None):
        self.pool = pool
        self.file_id = file_id
        self.name = name
        self.codec = codec          # RecordCodec, or None for raw-bytes use (the catalog)

    def create(self):
        fr = self.pool.new_page(self.file_id)
        if fr.page_id != 0:
            self.pool.unpin_page(fr.page_id, True, self.file_id)
            raise DBError("storage", f"{self.name}: a new heap file must start at page 0")
        init_dir_page(fr.data, 0)
        self.pool.unpin_page(0, True, self.file_id)

    # ---- insert --------------------------------------------------------------
    def insert(self, values):
        return self.insert_bytes(self.codec.encode(values), row=values)

    def insert_bytes(self, rec, row=None):
        need = len(rec) + SLOT_SIZE
        if len(rec) > MAX_RECORD:
            raise DBError("data", f"table {self.name}: record of {len(rec)} bytes cannot be stored "
                          f"(a page holds at most {MAX_RECORD}); row = {row!r}")
        pool, fid = self.pool, self.file_id
        dir_pid, last_dir = 0, 0
        while dir_pid != NO_PAGE:
            d = pool.fetch_page(dir_pid, fid)
            dir_dirty = False
            try:
                nxt, entries = dir_entries(d.data)
                hit = next((i for i, (_, free) in enumerate(entries) if free >= need), None)
                if hit is not None:
                    data_pid = entries[hit][0]
                    dp = pool.fetch_page(data_pid, fid)
                    slot = NO_SPACE
                    try:
                        slot = pg.insert(dp.data, rec)
                        free = pg.free_space(dp.data)
                    finally:
                        pool.unpin_page(data_pid, slot != NO_SPACE, fid)
                    if slot == NO_SPACE:
                        raise DBError("storage", f"{self.name}: directory said page {data_pid} had room but it did not")
                    struct.pack_into("<H", d.data, DIR_HEADER + DIR_ENTRY * hit + 4, free)
                    dir_dirty = True
                    return RID(data_pid, slot)
            finally:
                pool.unpin_page(dir_pid, dir_dirty, fid)
            last_dir, dir_pid = dir_pid, nxt
        return self._append_page(last_dir, rec)

    def _append_page(self, last_dir, rec):
        pool, fid = self.pool, self.file_id
        d = pool.fetch_page(last_dir, fid)
        dir_pid = last_dir
        _, cnt = struct.unpack_from("<IH", d.data, 4)
        if cnt >= DIR_CAPACITY:                       # directory page full: chain a new one
            nd = pool.new_page(fid)
            init_dir_page(nd.data, nd.page_id)
            struct.pack_into("<I", d.data, 4, nd.page_id)
            pool.unpin_page(last_dir, True, fid)
            d, dir_pid, cnt = nd, nd.page_id, 0
        dp = pool.new_page(fid)
        pg.init_page(dp.data, dp.page_id)
        slot = pg.insert(dp.data, rec)
        free = pg.free_space(dp.data)
        data_pid = dp.page_id
        struct.pack_into("<IH", d.data, DIR_HEADER + DIR_ENTRY * cnt, data_pid, free)
        struct.pack_into("<H", d.data, 8, cnt + 1)
        pool.unpin_page(data_pid, True, fid)
        pool.unpin_page(dir_pid, True, fid)
        return RID(data_pid, slot)

    def rewrite_all(self, records):
        """Catalog use only. Replace the whole content with `records` (list of bytes) and reuse the pages the
        file already has, so repeated delete+insert cycles do not make the file grow. Every existing data page
        is emptied (slot numbers restart at 0, so this is NOT allowed for user tables, whose rids must stay
        valid) and the records are inserted again first-fit. Deterministic: same records, same bytes."""
        pool, fid = self.pool, self.file_id
        for dir_pid in self.dir_chain():
            d = pool.fetch_page(dir_pid, fid)
            try:
                _, entries = dir_entries(d.data)
                for i, (data_pid, _) in enumerate(entries):
                    dp = pool.fetch_page(data_pid, fid)
                    pg.init_page(dp.data, data_pid)
                    free = pg.free_space(dp.data)
                    pool.unpin_page(data_pid, True, fid)
                    struct.pack_into("<H", d.data, DIR_HEADER + DIR_ENTRY * i + 4, free)
            finally:
                pool.unpin_page(dir_pid, True, fid)
        for rec in records:
            self.insert_bytes(rec)

    # ---- get / delete ----------------------------------------------------------
    def get_bytes(self, rid):
        fr = self.pool.fetch_page(rid[0], self.file_id)
        try:
            return pg.get(fr.data, rid[1], rid=RID(*rid))
        finally:
            self.pool.unpin_page(rid[0], False, self.file_id)

    def get(self, rid):
        return self.codec.decode(self.get_bytes(rid))

    def delete(self, rid):
        fr = self.pool.fetch_page(rid[0], self.file_id)
        ok = False
        try:
            pg.delete(fr.data, rid[1], rid=RID(*rid))
            ok = True
        finally:
            self.pool.unpin_page(rid[0], ok, self.file_id)

    # ---- scan ------------------------------------------------------------------
    def scan_bytes(self):
        """Generator of (rid, record bytes). Holds at most one page pinned at any instant.
        Close the generator early and the pinned page is released (finally block)."""
        pool, fid = self.pool, self.file_id
        dir_pid, seen = 0, set()
        while dir_pid != NO_PAGE:
            self._no_cycle(dir_pid, seen)
            d = pool.fetch_page(dir_pid, fid)
            try:
                nxt, entries = dir_entries(d.data)
            finally:
                pool.unpin_page(dir_pid, False, fid)
            for data_pid, _ in entries:
                fr = pool.fetch_page(data_pid, fid)
                try:
                    for slot, rec in pg.live_slots(fr.data):
                        yield RID(data_pid, slot), rec
                finally:
                    pool.unpin_page(data_pid, False, fid)
            dir_pid = nxt

    def scan(self):
        dec = self.codec.decode
        inner = self.scan_bytes()
        try:
            for rid, rec in inner:
                yield rid, dec(rec)
        finally:
            inner.close()          # do not rely on garbage collection to release the pinned page

    def _no_cycle(self, dir_pid, seen):
        if dir_pid in seen:
            raise DBError("storage", f"{self.name}: the directory page chain loops back to page {dir_pid} "
                          f"(corrupt file, or pages were never written to disk)")
        seen.add(dir_pid)

    # ---- bookkeeping -------------------------------------------------------------
    def dir_chain(self):
        """Page ids of every directory page, in order."""
        out, dir_pid = [], 0
        while dir_pid != NO_PAGE:
            self._no_cycle(dir_pid, set(out))
            out.append(dir_pid)
            d = self.pool.fetch_page(dir_pid, self.file_id)
            try:
                dir_pid = struct.unpack_from("<I", d.data, 4)[0]
            finally:
                self.pool.unpin_page(out[-1], False, self.file_id)
        return out

    def count_data_pages(self):
        total, dir_pid, seen = 0, 0, set()
        while dir_pid != NO_PAGE:
            self._no_cycle(dir_pid, seen)
            d = self.pool.fetch_page(dir_pid, self.file_id)
            try:
                nxt, entries = dir_entries(d.data)
                total += len(entries)
            finally:
                self.pool.unpin_page(dir_pid, False, self.file_id)
            dir_pid = nxt
        return total
