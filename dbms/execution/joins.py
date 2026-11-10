"""The three joins (spec 2.7 to 2.9) and intersect / except (2.10).

Frame budget F = ctx.frames at open time. In every join a leaf-pipeline input holds one pinned
frame and a materialized input holds one leased reader frame, so both cases have the same count.
"""
import math

from ..errors import DBError
from ..ra.condition import compile_condition, resolve
from ..ra.nodes import Node
from ..schema import comparable
from .context import (TempFile, TupleWriter, read_tuples, PageCounter, make_sizer, codec_for, det_hash)
from .operators import Operator, END, check_setop_compat
from .sorter import ExternalSorter


def _product_schema(left, right):
    schema = left.schema.concat(right.schema)
    dup = schema.first_duplicate()
    if dup is not None:
        raise DBError("schema", f"both inputs of the join have a column '{dup.qname}', so the qualified "
                      f"names collide; use rename on one side")
    return schema


def _conjuncts(c):
    if c.op == "and":
        return _conjuncts(c.left) + _conjuncts(c.right)
    return [c]


def equi_keys(cond, left_schema, right_schema, who):
    """cond must be an AND of 'left column = right column'. Returns (left idxs, right idxs)."""
    nl = len(left_schema)
    both = left_schema.concat(right_schema)
    lk, rk = [], []
    for c in _conjuncts(cond):
        if c.op != "cmp" or c.cmp != "=" or c.left.op != "attr" or c.right.op != "attr":
            raise DBError("plan", f"{who} accepts only equality conditions between a column of each input "
                          f"(got a different condition); use nested_loop_join for other predicates")
        i, j = resolve(both, c.left), resolve(both, c.right)
        if (i < nl) == (j < nl):
            raise DBError("plan", f"{who}: both sides of an equality must come from different inputs")
        if i >= nl:
            i, j = j, i
        if both[i].type != both[j].type and not comparable(both[i].type, both[j].type):
            raise DBError("type", f"cannot compare {both[i].type} with {both[j].type} in the join condition")
        lk.append(i)
        rk.append(j - nl)
    return lk, rk


# ======================================================================== block nested loop join
class NestedLoopJoin(Operator):
    name = "nested_loop_join"

    def __init__(self, ctx, node, left, right):
        super().__init__(ctx, node, [left, right])
        self.schema = _product_schema(left, right)
        self.pred = compile_condition(node.cond, self.schema, len(left.schema))
        self.outer = self.inner = None
        self.lease, self.gen = [], None

    def open(self):
        F = self.ctx.frames
        if F < 3:
            raise DBError("plan", f"nested_loop_join needs at least 3 frames (it has {F}); raise --frames")
        self.outer = self._source(self.children[0])
        self.inner = self._source(self.children[1])
        self.block_frames = F - 2                 # B - 2 outer pages; 1 outer input + 1 inner input
        self.outer.begin()
        self.lease = self.ctx.lease(self.block_frames, self.label())
        self.info = {"block_pages": self.block_frames, "blocks": 0}
        self.gen = self._produce()

    def _produce(self):
        sizer = make_sizer(self.children[0].schema)
        counter = PageCounter(sizer, self.block_frames)
        pending, outer_done, pred = None, False, self.pred
        while not outer_done:
            block = []
            counter.reset()
            if pending is not None:
                counter.try_add(pending)
                block.append(pending)
                pending = None
            while True:
                t = self.outer.next()
                if t is END:
                    outer_done = True
                    break
                if not counter.try_add(t):
                    pending = t
                    break
                block.append(t)
            if not block:
                break
            self.info["blocks"] += 1
            self.inner.begin()                    # rescan the inner once per outer block
            try:
                n = len(block)
                for s in self.inner:
                    self.comparisons += n         # every pair is compared
                    for a in block:
                        if pred(a, s):
                            yield a + s
            finally:
                self.inner.end()

    def next(self):
        t = next(self.gen, END)
        if t is not END:
            self.tuples_out += 1
        return t

    def close(self):
        if self.gen is not None:
            self.gen.close()
        for src in (self.outer, self.inner):          # end() and dispose() are safe to call twice
            if src is not None:
                src.end()
                src.dispose()
        self.ctx.release(self.lease)
        self.lease = []
        super().close()


# ============================================================================== sort-merge join
class SortMergeJoin(Operator):
    name = "sort_merge_join"

    def __init__(self, ctx, node, left, right):
        super().__init__(ctx, node, [left, right])
        self.schema = _product_schema(left, right)
        self.lk, self.rk = equi_keys(node.cond, left.schema, right.schema, "sort_merge_join")
        self.lfile = self.rfile = None
        self.lease, self.gen = [], None
        self.sorts = []

    def _sort_to_file(self, child, keys):
        F = self.ctx.frames
        src = self._source(child)
        sorter = ExternalSorter(self.ctx, self, child.schema, [(k, "asc") for k in keys], F)
        src.begin()
        try:
            sorter.consume(iter(src))
        finally:
            src.end()
            src.dispose()
        tf = sorter.to_file()
        self.sorts.append({"initial_runs": sorter.initial_runs, "passes": sorter.passes})
        return tf

    def open(self):
        if self.ctx.frames < 3:
            raise DBError("plan", f"sort_merge_join needs at least 3 frames (it has {self.ctx.frames})")
        self.lfile = self._sort_to_file(self.children[0], self.lk)
        self.rfile = self._sort_to_file(self.children[1], self.rk)
        self.lease = self.ctx.lease(3, self.label())     # left reader, right reader, equal-key run buffer
        self.info = {"sorts": self.sorts}
        self.gen = self._merge()

    def _merge(self):
        lcodec, rcodec = codec_for(self.children[0].schema), codec_for(self.children[1].schema)
        lk, rk = self.lk, self.rk
        lkey = lambda t: tuple(t[i] for i in lk)
        rkey = lambda t: tuple(t[i] for i in rk)
        rsizer = make_sizer(self.children[1].schema)
        lit, rit = read_tuples(self.lfile, lcodec), read_tuples(self.rfile, rcodec)
        l, r = next(lit, END), next(rit, END)
        while l is not END and r is not END:
            kl, kr = lkey(l), rkey(r)
            self.comparisons += 1
            if kl < kr:
                l = next(lit, END)
            elif kl > kr:
                r = next(rit, END)
            else:
                # collect the whole run of equal keys on the right: one frame in memory,
                # and if it does not fit, the whole run goes to a temp file
                mem, counter, spill, writer = [], PageCounter(rsizer, 1), None, None
                while r is not END:
                    self.comparisons += 1
                    if rkey(r) != kr:
                        break
                    if spill is None and counter.try_add(r):
                        mem.append(r)
                    else:
                        if spill is None:
                            spill = TempFile(self.ctx, self)
                            writer = TupleWriter(spill, rcodec)
                            for s in mem:
                                writer.add(s)
                            mem = None
                            self.notes.append("equal-key run larger than one frame: spilled to a temp file")
                        writer.add(r)
                    r = next(rit, END)
                if writer is not None:
                    writer.close()
                while l is not END:               # every left tuple with this key pairs with the whole run
                    self.comparisons += 1
                    if lkey(l) != kr:
                        break
                    if mem is not None:
                        for s in mem:
                            yield l + s
                    else:
                        for s in read_tuples(spill, rcodec):
                            yield l + s
                    l = next(lit, END)
                if spill is not None:
                    spill.delete()

    def next(self):
        t = next(self.gen, END)
        if t is not END:
            self.tuples_out += 1
        return t

    def close(self):
        if self.gen is not None:
            self.gen.close()
        self.ctx.release(self.lease)
        self.lease = []
        for tf in (self.lfile, self.rfile):
            if tf is not None:
                tf.delete()
        super().close()


# ===================================================================================== hash join
class HashJoin(Operator):
    """Grace hash join. Left is the build side. P = F - 1 partitions. Overflow: repartition once with
    a different hash seed; a partition that still does not fit falls back to block nested loop."""
    name = "hash_join"
    MAX_REPARTITION_DEPTH = 1

    def __init__(self, ctx, node, left, right):
        super().__init__(ctx, node, [left, right])
        self.schema = _product_schema(left, right)
        self.lk, self.rk = equi_keys(node.cond, left.schema, right.schema, "hash_join")
        self.lcodec, self.rcodec = codec_for(left.schema), codec_for(right.schema)
        self.lsizer = make_sizer(left.schema)
        self.gen, self.lparts, self.rparts = None, [], []
        self.extra_files = []

    def _lkey(self, t):
        return tuple(t[i] for i in self.lk)

    def _rkey(self, t):
        return tuple(t[i] for i in self.rk)

    def open(self):
        F = self.ctx.frames
        if F < 3:
            raise DBError("plan", f"hash_join needs at least 3 frames (it has {F}); raise --frames")
        lsrc, rsrc = self._source(self.children[0]), self._source(self.children[1])
        self.P = self._choose_p(lsrc.est_pages(), F)
        self.lparts = self._partition(lsrc, self._lkey, self.lcodec)
        self.rparts = self._partition(rsrc, self._rkey, self.rcodec)
        self.info = {"partitions": self.P,
                     "left_partition_pages": [t.num_pages for t in self.lparts],
                     "right_partition_pages": [t.num_pages for t in self.rparts],
                     "overflow": []}
        self.gen = self._probe()

    @staticmethod
    def _choose_p(build_pages, F):
        """Number of partitions. At most F-1 (one output page each, plus the input frame). Using all
        F-1 when the build side is small would write hundreds of nearly empty pages, so use just
        enough partitions that each build partition is expected to fit in F-2 pages (at least 2)."""
        if build_pages is None:
            return F - 1
        return max(2, min(F - 1, math.ceil(build_pages / (F - 2))))

    def _partition(self, src, keyfn, codec):
        P = self.P
        src.begin()
        lease = self.ctx.lease(P, self.label())            # P output pages (+ the input frame)
        parts = [TempFile(self.ctx, self) for _ in range(P)]
        try:
            writers = [TupleWriter(tf, codec) for tf in parts]
            for t in src:
                writers[det_hash(keyfn(t), 0) % P].add(t)
            for w in writers:
                w.close()
        finally:
            self.ctx.release(lease)
            src.end()
            src.dispose()
        return parts

    def _probe(self):
        for lt, rt in zip(self.lparts, self.rparts):
            yield from self._join_pair(lt, rt, 0)

    def _join_pair(self, rt, st, depth):
        """rt = build partition, st = probe partition."""
        rp, sp = rt.num_pages, st.num_pages
        cap = self.ctx.frames - 1
        if rp == 0 or sp == 0:
            rt.delete()
            st.delete()
            return
        if rp <= cap:
            yield from self._in_memory(rt, st, rp)
        elif depth < self.MAX_REPARTITION_DEPTH:
            self.info["overflow"].append({"action": "repartition", "build_pages": rp, "limit": cap, "depth": depth})
            P2 = self._choose_p(rp, self.ctx.frames)
            rsubs = self._split(rt, self._lkey, self.lcodec, depth + 1, P2)
            ssubs = self._split(st, self._rkey, self.rcodec, depth + 1, P2)
            for a, b in zip(rsubs, ssubs):
                yield from self._join_pair(a, b, depth + 1)
        else:
            self.info["overflow"].append({"action": "block nested loop fallback", "build_pages": rp,
                                          "limit": cap, "depth": depth})
            yield from self._fallback(rt, st)

    def _split(self, tf, keyfn, codec, seed, P):
        lease = self.ctx.lease(P + 1, self.label())        # P output pages + one input page
        subs = [TempFile(self.ctx, self) for _ in range(P)]
        try:
            writers = [TupleWriter(s, codec) for s in subs]
            for t in read_tuples(tf, codec):
                writers[det_hash(keyfn(t), seed) % P].add(t)
            for w in writers:
                w.close()
        finally:
            self.ctx.release(lease)
        tf.delete()
        return subs

    def _in_memory(self, rt, st, rp):
        lease = self.ctx.lease(rp + 1, self.label())       # the table (rp pages) + the probe page
        try:
            table = {}
            for t in read_tuples(rt, self.lcodec):
                table.setdefault(self._lkey(t), []).append(t)
            rt.delete()
            for s in read_tuples(st, self.rcodec):
                bucket = table.get(self._rkey(s))
                self.comparisons += len(bucket) if bucket else 1
                if bucket:
                    for r in bucket:
                        yield r + s
        finally:
            self.ctx.release(lease)
            rt.delete()
            st.delete()

    def _fallback(self, rt, st):
        F = self.ctx.frames
        block_frames = F - 2
        lease = self.ctx.lease(block_frames + 2, self.label())
        try:
            counter = PageCounter(self.lsizer, block_frames)
            it = read_tuples(rt, self.lcodec)
            pending, done = None, False
            while not done:
                block = []
                counter.reset()
                if pending is not None:
                    counter.try_add(pending)
                    block.append(pending)
                    pending = None
                while True:
                    t = next(it, END)
                    if t is END:
                        done = True
                        break
                    if not counter.try_add(t):
                        pending = t
                        break
                    block.append(t)
                if not block:
                    break
                bkeys = [self._lkey(a) for a in block]
                for s in read_tuples(st, self.rcodec):
                    self.comparisons += len(block)
                    ks = self._rkey(s)
                    for a, ka in zip(block, bkeys):
                        if ka == ks:
                            yield a + s
        finally:
            self.ctx.release(lease)
            rt.delete()
            st.delete()

    def next(self):
        t = next(self.gen, END)
        if t is not END:
            self.tuples_out += 1
        return t

    def close(self):
        if self.gen is not None:
            self.gen.close()
        for tf in self.lparts + self.rparts:
            tf.delete()
        super().close()


# ========================================================================== intersect and except
class SetOp(Operator):
    """intersect / except with set semantics, by sorting both inputs on all columns and merging."""

    def __init__(self, ctx, node, left, right):
        super().__init__(ctx, node, [left, right])
        self.name = node.op
        check_setop_compat(node.op, left.schema, right.schema)
        self.schema = left.schema
        self.lfile = self.rfile = None
        self.lease, self.gen = [], None
        self.sorts = []

    def _sorted_file(self, child):
        src = self._source(child)
        n = len(child.schema)
        sorter = ExternalSorter(self.ctx, self, child.schema, [(i, "asc") for i in range(n)], self.ctx.frames)
        src.begin()
        try:
            sorter.consume(iter(src))
        finally:
            src.end()
            src.dispose()
        tf = sorter.to_file()
        self.sorts.append({"initial_runs": sorter.initial_runs, "passes": sorter.passes})
        return tf

    def open(self):
        if self.ctx.frames < 3:
            raise DBError("plan", f"{self.name} needs at least 3 frames (it has {self.ctx.frames})")
        self.lfile = self._sorted_file(self.children[0])
        self.rfile = self._sorted_file(self.children[1])
        self.lease = self.ctx.lease(2, self.label())
        self.info = {"sorts": self.sorts}
        self.gen = self._merge()

    def _merge(self):
        keep_if_present = self.name == "intersect"
        lit = read_tuples(self.lfile, codec_for(self.children[0].schema))
        rit = read_tuples(self.rfile, codec_for(self.children[1].schema))
        l, r, last = next(lit, END), next(rit, END), None
        while l is not END:
            while r is not END:
                self.comparisons += 1
                if r < l:
                    r = next(rit, END)
                else:
                    break
            present = False
            if r is not END:
                self.comparisons += 1
                present = (r == l)
            if present == keep_if_present and l != last:     # set semantics: each tuple at most once
                yield l
            last = l
            l = next(lit, END)

    def next(self):
        t = next(self.gen, END)
        if t is not END:
            self.tuples_out += 1
        return t

    def close(self):
        if self.gen is not None:
            self.gen.close()
        self.ctx.release(self.lease)
        self.lease = []
        for tf in (self.lfile, self.rfile):
            if tf is not None:
                tf.delete()
        super().close()
