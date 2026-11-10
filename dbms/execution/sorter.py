"""External merge sort (spec 2.5), used by sort, distinct, group_by, sort_merge_join, intersect, except.

Memory (F = frames the sort may use; the input scan/reader holds one of them):
  run generation : F-1 frames of tuples (counted with PageCounter), sort them, write one run
  merge passes   : fan-in k = F-1 runs, one frame per run being read; output tuples go to a
                   TupleWriter whose page is built from freed frames, so no extra frame
  final pass     : streams to the parent, writes nothing
If the whole input fits in the F-1 run frames, nothing is ever written (one pass, no I/O).
A run that is left over in a pass is carried to the next pass untouched, not rewritten.
passes = 1 (run generation) + number of merge passes, the last of which is the streaming one.
"""
import heapq
from collections import deque
from functools import cmp_to_key

from ..errors import DBError
from .context import TempFile, TupleWriter, read_tuples, PageCounter, make_sizer, codec_for


class Rev:
    """Wrapper that reverses ordering (for DESC keys, including VARCHAR)."""
    __slots__ = ("v",)

    def __init__(self, v):
        self.v = v

    def __lt__(self, o):
        return o.v < self.v

    def __eq__(self, o):
        return self.v == o.v

    def __hash__(self):
        return hash(self.v)


def make_keyfn(spec):
    """spec: list of (column index, 'asc'|'desc') -> function tuple -> comparable key."""
    idxs = [i for i, _ in spec]
    if all(d == "asc" for _, d in spec):
        if len(idxs) == 1:
            i = idxs[0]
            return lambda t: (t[i],)
        return lambda t: tuple(t[i] for i in idxs)
    return lambda t: tuple(t[i] if d == "asc" else Rev(t[i]) for i, d in spec)


class _Entry:
    __slots__ = ("key", "t", "src", "owner", "it")

    def __init__(self, key, t, src, owner, it):
        self.key, self.t, self.src, self.owner, self.it = key, t, src, owner, it

    def __lt__(self, o):
        self.owner.comparisons += 1             # one key comparison per heap comparison
        if self.key != o.key:
            return self.key < o.key
        return self.src < o.src                  # equal keys: earlier run first (stable)


class ExternalSorter:
    def __init__(self, ctx, owner, schema, spec, frames):
        if frames < 3:
            raise DBError("plan", f"{owner.label()} needs at least 3 frames to sort (it has {frames}); raise --frames")
        self.ctx, self.owner, self.schema = ctx, owner, schema
        self.run_frames = frames - 1
        self.fan_in = frames - 1
        self.keyfn = make_keyfn(spec)
        self.sizer = make_sizer(schema)
        self.codec = codec_for(schema)
        self.runs = []                 # TempFile list (after run generation)
        self.mem = None                # the single in-memory run, if the input fit
        self.mem_lease = []
        self.passes = 0
        self.initial_runs = 0

    # ---- pass 0 ---------------------------------------------------------------------------
    def _sort_in_memory(self, tuples):
        owner, keyfn = self.owner, self.keyfn

        def cmp(x, y):
            owner.comparisons += 1
            return (x[0] > y[0]) - (x[0] < y[0])

        dec = [(keyfn(t), t) for t in tuples]
        dec.sort(key=cmp_to_key(cmp))
        return deque(t for _, t in dec)

    def _write_run(self, sorted_dq):
        tf = TempFile(self.ctx, self.owner)
        w = TupleWriter(tf, self.codec)
        while sorted_dq:                         # consume as we write so memory never doubles
            w.add(sorted_dq.popleft())
        w.close()
        return tf

    def consume(self, it):
        lease = self.ctx.lease(self.run_frames, self.owner.label())
        keep = False
        try:
            counter = PageCounter(self.sizer, self.run_frames)
            buf = []
            for t in it:
                if not counter.try_add(t):
                    self.runs.append(self._write_run(self._sort_in_memory(buf)))
                    buf = []
                    counter.reset()
                    counter.try_add(t)
                buf.append(t)
            if not self.runs:                    # everything fit: keep it, never touch the disk
                self.mem = self._sort_in_memory(buf)
                self.mem_lease = lease
                keep = True
            elif buf:
                self.runs.append(self._write_run(self._sort_in_memory(buf)))
            self.initial_runs = len(self.runs) if self.runs else 1
            self.passes = 1
        finally:
            if not keep:
                self.ctx.release(lease)

    # ---- merging --------------------------------------------------------------------------
    def _merge(self, files):
        owner, keyfn = self.owner, self.keyfn
        heap = []
        for i, tf in enumerate(files):
            it = read_tuples(tf, self.codec)
            t = next(it, None)
            if t is not None:
                heap.append(_Entry(keyfn(t), t, i, owner, it))
        heapq.heapify(heap)
        while heap:
            e = heap[0]
            yield e.t
            nt = next(e.it, None)
            if nt is None:
                heapq.heappop(heap)
            else:
                heapq.heapreplace(heap, _Entry(keyfn(nt), nt, e.src, owner, e.it))

    def _merge_to_file(self, files):
        lease = self.ctx.lease(len(files) + 1, self.owner.label())
        try:
            tf = TempFile(self.ctx, self.owner)
            w = TupleWriter(tf, self.codec)
            for t in self._merge(files):
                w.add(t)
            w.close()
        finally:
            self.ctx.release(lease)
        for f in files:
            f.delete()
        return tf

    def reduce(self):
        """Intermediate merge passes: until at most fan_in runs remain."""
        while len(self.runs) > self.fan_in:
            new = []
            for i in range(0, len(self.runs), self.fan_in):
                group = self.runs[i:i + self.fan_in]
                new.append(group[0] if len(group) == 1 else self._merge_to_file(group))
            self.runs = new
            self.passes += 1

    def stream(self):
        """Final pass: sorted tuples streamed to the caller (writes nothing)."""
        try:
            if self.mem is not None:
                while self.mem:
                    yield self.mem.popleft()
                return
            self.passes += 1
            lease = self.ctx.lease(len(self.runs), self.owner.label()) if self.runs else []
            try:
                yield from self._merge(self.runs)
            finally:
                self.ctx.release(lease)
        finally:
            self.close()

    def to_file(self):
        """Final pass written to ONE sorted temp file (used by sort-merge join and set operators)."""
        self.reduce()
        tf = TempFile(self.ctx, self.owner)
        w = TupleWriter(tf, self.codec)
        if self.mem is not None:
            while self.mem:
                w.add(self.mem.popleft())
        else:
            self.passes += 1
            lease = self.ctx.lease(len(self.runs) + 1, self.owner.label()) if self.runs else []
            try:
                for t in self._merge(self.runs):
                    w.add(t)
            finally:
                self.ctx.release(lease)
        w.close()
        self.close()
        return tf

    def close(self):
        self.ctx.release(self.mem_lease)
        self.mem_lease = []
        self.mem = None
        for tf in self.runs:
            tf.delete()
        self.runs = []
