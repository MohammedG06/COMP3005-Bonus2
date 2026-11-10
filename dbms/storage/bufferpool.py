"""Buffer pool (spec section 5): B frames, pin counts, dirty flags, page table, pluggable policy.

Pages are addressed as (file_id, page_id) so one pool serves the catalog and every table file
inside one memory budget. file_id defaults to 0, which is all `storage trace` needs.

Counting rules (exact):
  hit      = fetch of a resident page
  miss     = fetch that needed a disk read           (every miss is one read)
  eviction = a page was replaced to make room        (first B loads on an empty pool are not)
  read/write = one call to the disk manager; a write happens on eviction of a dirty page or on flush
new_page() counts no hit and no miss and no read (nothing is read from disk); if it has to
replace a resident page, that is still an eviction.
"""
import heapq

from ..errors import DBError
from .constants import PAGE_SIZE
from .policies import make_policy


class Frame:
    __slots__ = ("key", "data", "pin", "dirty")

    def __init__(self):
        self.key = None
        self.data = bytearray(PAGE_SIZE)
        self.pin = 0
        self.dirty = False

    @property
    def page_id(self):
        return self.key[1]


class BufferPool:
    def __init__(self, nframes, policy="lru"):
        if nframes < 1:
            raise DBError("storage", "the buffer pool needs at least 1 frame")
        self.n = nframes
        self.frames = [Frame() for _ in range(nframes)]
        self.table = {}                      # (file_id, page_id) -> frame index
        self.free = list(range(nframes))     # min-heap: lowest free frame first (deterministic)
        heapq.heapify(self.free)
        self.policy = make_policy(policy, nframes)
        self.disks = {}
        self.hits = self.misses = self.evictions = self.reads = self.writes = 0
        self.leased = []                     # frame indexes taken out of circulation as working memory
        self.n_pinned = 0                    # frames with pin count > 0 (excluding leases)
        self.peak_held = 0                   # most frames pinned + leased at any instant

    # ---- setup -------------------------------------------------------------
    def register_disk(self, file_id, disk):
        self.disks[file_id] = disk

    def _disk(self, file_id):
        d = self.disks.get(file_id)
        if d is None:
            raise DBError("storage", f"no disk registered for file id {file_id}")
        return d

    # ---- core --------------------------------------------------------------
    def _acquire(self):
        """A frame to load a page into: a free one, else a victim (written back if dirty)."""
        if self.free:
            return heapq.heappop(self.free)
        idx = self.policy.choose_victim(self.frames)
        if idx is None:
            raise DBError("storage", "no unpinned frame available (every frame is pinned)")
        fr = self.frames[idx]
        if fr.dirty:
            self._disk(fr.key[0]).write_page(fr.key[1], fr.data)
            self.writes += 1
        del self.table[fr.key]
        fr.key, fr.dirty = None, False
        self.evictions += 1
        return idx

    def fetch_page(self, page_id, file_id=0):
        key = (file_id, page_id)
        idx = self.table.get(key)
        if idx is not None:
            fr = self.frames[idx]
            if fr.pin == 0:
                self._pinned_up()
            fr.pin += 1
            self.policy.on_access(idx)
            self.hits += 1
            return fr
        disk = self._disk(file_id)
        if not 0 <= page_id < disk.num_pages():
            raise DBError("storage", f"page {page_id} does not exist ({disk.num_pages()} pages)")
        idx = self._acquire()
        fr = self.frames[idx]
        try:
            data = disk.read_page(page_id)
        except Exception:
            heapq.heappush(self.free, idx)
            raise
        fr.data[:] = data
        fr.key, fr.pin, fr.dirty = key, 1, False
        self.table[key] = idx
        self.policy.on_load(idx)
        self._pinned_up()
        self.misses += 1
        self.reads += 1
        return fr

    def unpin_page(self, page_id, is_dirty=False, file_id=0):
        idx = self.table.get((file_id, page_id))
        if idx is None:
            raise DBError("storage", f"unpin of page {page_id} which is not in the buffer pool")
        fr = self.frames[idx]
        if fr.pin == 0:
            raise DBError("storage", f"unpin of page {page_id} whose pin count is already 0")
        fr.dirty = fr.dirty or is_dirty
        fr.pin -= 1
        if fr.pin == 0:
            self.n_pinned -= 1

    def new_page(self, file_id=0):
        disk = self._disk(file_id)
        idx = self._acquire()
        fr = self.frames[idx]
        pid = disk.allocate_page()
        fr.data[:] = bytes(PAGE_SIZE)
        fr.key, fr.pin, fr.dirty = (file_id, pid), 1, True
        self.table[fr.key] = idx
        self.policy.on_load(idx)
        self._pinned_up()
        return fr

    # ---- working memory for operators (Module 2) ---------------------------------------
    def _pinned_up(self):
        self.n_pinned += 1
        held = self.n_pinned + len(self.leased)
        if held > self.peak_held:
            self.peak_held = held

    def lease(self, n):
        """Take n frames out of circulation as private working memory (evicting if needed).
        Fails, and takes nothing, if fewer than n frames can be spared."""
        got = []
        try:
            for _ in range(n):
                idx = self._acquire()
                fr = self.frames[idx]
                fr.pin = 1                    # protects it from every policy until released
                self.policy.on_drop(idx)
                got.append(idx)
        except DBError:
            for idx in got:
                self.frames[idx].pin = 0
                heapq.heappush(self.free, idx)
            raise
        self.leased.extend(got)
        held = self.n_pinned + len(self.leased)
        if held > self.peak_held:
            self.peak_held = held
        return got

    def release(self, idxs):
        for idx in idxs:
            self.leased.remove(idx)
            self.frames[idx].pin = 0
            heapq.heappush(self.free, idx)

    # ---- maintenance -------------------------------------------------------
    def flush_all(self):
        for fr in self.frames:
            if fr.key is not None and fr.dirty:
                self._disk(fr.key[0]).write_page(fr.key[1], fr.data)
                self.writes += 1
                fr.dirty = False

    def drop_file(self, file_id):
        """Forget every resident page of a file without writing it (used for temp files)."""
        for idx, fr in enumerate(self.frames):
            if fr.key is not None and fr.key[0] == file_id:
                if fr.pin > 0:
                    raise DBError("storage", f"cannot drop file {file_id}: page {fr.key[1]} is still pinned")
                del self.table[fr.key]
                fr.key, fr.dirty = None, False
                self.policy.on_drop(idx)
                heapq.heappush(self.free, idx)

    def clear(self):
        """Write back dirty pages and empty the pool (a cold cache). Error if anything is pinned."""
        if self.n_pinned or self.leased:
            raise DBError("storage", "cannot clear the buffer pool while pages are pinned or leased")
        self.flush_all()
        for idx, fr in enumerate(self.frames):
            if fr.key is not None:
                del self.table[fr.key]
                fr.key = None
                self.policy.on_drop(idx)
                heapq.heappush(self.free, idx)

    def reset_stats(self):
        self.hits = self.misses = self.evictions = self.reads = self.writes = 0

    def stats(self):
        return {"hits": self.hits, "misses": self.misses, "evictions": self.evictions,
                "reads": self.reads, "writes": self.writes}

    def pinned_count(self):
        """Frames currently pinned by a fetch (leased working-memory frames are counted separately)."""
        return self.n_pinned

    def resident_count(self):
        return len(self.table)
