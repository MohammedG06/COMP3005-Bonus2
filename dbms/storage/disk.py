"""Disk managers. Every page read and write goes through one of these and is counted."""
import os

from ..errors import DBError
from .constants import PAGE_SIZE


class DiskManager:
    def __init__(self, path, create=False):
        self.path = path
        self.f = open(path, "w+b" if create else "r+b")
        self.f.seek(0, 2)
        self._num_pages = self.f.tell() // PAGE_SIZE
        self.reads = 0
        self.writes = 0

    def num_pages(self):
        return self._num_pages

    def allocate_page(self):
        pid = self._num_pages
        self._num_pages += 1
        return pid

    def read_page(self, page_id):
        if not 0 <= page_id < self._num_pages:
            raise DBError("storage", f"page {page_id} does not exist in {os.path.basename(self.path)} "
                          f"({self._num_pages} pages)")
        self.f.seek(page_id * PAGE_SIZE)
        data = self.f.read(PAGE_SIZE)
        if len(data) < PAGE_SIZE:                  # allocated but never written
            data = data.ljust(PAGE_SIZE, b"\0")
        self.reads += 1
        return data

    def write_page(self, page_id, data):
        if len(data) != PAGE_SIZE:
            raise DBError("storage", f"write_page needs exactly {PAGE_SIZE} bytes")
        self.f.seek(page_id * PAGE_SIZE)
        self.f.write(data)
        self.writes += 1

    def close(self):
        if not self.f.closed:
            self.f.flush()
            self.f.close()

    def delete(self):
        self.close()
        if os.path.exists(self.path):
            os.remove(self.path)


class SimulatedDisk:
    """A disk with no file behind it: every page reads as zeros. Used by `storage trace`,
    where only the counters matter."""

    def __init__(self, num_pages=2 ** 32):
        self._n = num_pages
        self.reads = 0
        self.writes = 0

    def num_pages(self):
        return self._n

    def allocate_page(self):
        self._n += 1
        return self._n - 1

    def read_page(self, page_id):
        self.reads += 1
        return bytes(PAGE_SIZE)

    def write_page(self, page_id, data):
        self.writes += 1

    def close(self):
        pass
