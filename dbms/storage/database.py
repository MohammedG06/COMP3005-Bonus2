"""Database: one directory = catalog.db + one <table>.db per table, all behind ONE buffer pool."""
import os

from ..errors import DBError
from .bufferpool import BufferPool
from .catalog import Catalog
from .disk import DiskManager
from .heap import HeapFile
from .records import RecordCodec


def _valid_name(name):
    return bool(name) and (name[0].isalpha() or name[0] == "_") and all(c.isalnum() or c == "_" for c in name)


class Database:
    def __init__(self, path, frames=16, policy="lru", create=False):
        self.path = path
        if create:
            os.makedirs(path, exist_ok=True)
        elif not os.path.isdir(path):
            raise DBError("storage", f"database directory '{path}' does not exist")
        self.pool = BufferPool(frames, policy)
        self._disks = {}
        self._file_ids = {}
        self._next_id = 1
        cat_path = os.path.join(path, "catalog.db")
        exists = os.path.exists(cat_path)
        if not exists and not create:
            raise DBError("storage", f"'{path}' is not a database directory (no catalog.db)")
        disk = DiskManager(cat_path, create=not exists)
        self._disks[0] = disk
        self.pool.register_disk(0, disk)
        self.catalog_heap = HeapFile(self.pool, 0, "catalog")
        if not exists:
            self.catalog_heap.create()
        self.catalog = Catalog(self.catalog_heap)

    # ---- tables ------------------------------------------------------------
    def create_table(self, name, columns):
        if not _valid_name(name):
            raise DBError("schema", f"bad table name '{name}'")
        info = self.catalog.create_table(name, columns)
        disk = DiskManager(os.path.join(self.path, info.file), create=True)
        fid = self._register(name, disk)
        HeapFile(self.pool, fid, name, RecordCodec(columns)).create()
        return info

    def _register(self, name, disk):
        fid = self._next_id
        self._next_id += 1
        self._file_ids[name] = fid
        self._disks[fid] = disk
        self.pool.register_disk(fid, disk)
        return fid

    def open_heap(self, name):
        info = self.catalog.get_table(name)
        fid = self._file_ids.get(name)
        if fid is None:
            fid = self._register(name, DiskManager(os.path.join(self.path, info.file)))
        return HeapFile(self.pool, fid, name, RecordCodec(info.columns)), info

    def refresh_counts(self, name, tuples_delta=0):
        """Recount data pages and update the catalog (tuples change by tuples_delta)."""
        heap, info = self.open_heap(name)
        self.catalog.update_counts(name, heap.count_data_pages(), max(0, info.num_tuples + tuples_delta))

    # ---- lifecycle -----------------------------------------------------------
    def close(self):
        self.pool.flush_all()
        for d in self._disks.values():
            d.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
