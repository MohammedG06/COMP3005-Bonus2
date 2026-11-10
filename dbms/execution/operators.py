"""Iterator operators (spec section 2): open() / next() / close() / counters().

Rule for consumers that hold working memory (sort, group_by, distinct, joins, set operators):
  * an input that is a *leaf pipeline* (scan, possibly under filter/project/rename/limit/union_all)
    is streamed straight in: it holds one pinned frame;
  * any other input (a join, a sort, a group_by below it) is first drained into a temporary file by
    ctx-level materialize(), running alone with the full budget minus one writer frame.
That guarantees two memory-hungry operators are never active at the same time, so every plan
fits in B frames, even B = 4 (see DESIGN_M2.md section 2).
"""
from itertools import chain

from ..errors import DBError
from ..schema import Column, Schema, INT, REAL, VARCHAR
from ..ra.condition import compile_condition, resolve
from ..ra.nodes import attr_str
from .context import (ExecContext, TempFile, TupleWriter, read_tuples, PageCounter, make_sizer,
                      codec_for, det_hash)
from .sorter import ExternalSorter

END = None


class Operator:
    name = "operator"

    def __init__(self, ctx, node, children):
        self.ctx, self.node, self.children = ctx, node, children
        self.schema = None
        self.tuples_out = self.comparisons = self.page_reads = self.page_writes = 0
        self.notes = []
        self.info = {}
        self._closed = False
        ctx.operators.append(self)

    # ---- interface ----------------------------------------------------------------------------
    def open(self):
        raise NotImplementedError

    def next(self):
        raise NotImplementedError

    def close(self):
        self._closed = True

    def counters(self):
        return {"tuples_out": self.tuples_out, "comparisons": self.comparisons,
                "page_reads": self.page_reads, "page_writes": self.page_writes}

    # ---- helpers --------------------------------------------------------------------------------
    def label(self):
        return self.name

    def is_leaf(self):
        return False

    def est_pages(self):
        """Upper bound on the pages this operator's output occupies (None if unknown)."""
        return None

    def cum_reads(self):
        return self.page_reads + sum(c.cum_reads() for c in self.children)

    def cum_writes(self):
        return self.page_writes + sum(c.cum_writes() for c in self.children)

    def _source(self, child):
        """How this operator consumes a child: stream a leaf pipeline, materialize anything else."""
        return StreamSource(child) if child.is_leaf() else materialize(self.ctx, self, child)


# ======================================================================================== sources
class StreamSource:
    def __init__(self, child):
        self.child = child
        self.schema = child.schema

    def est_pages(self):
        return self.child.est_pages()

    def begin(self):
        self.child.open()

    def next(self):
        return self.child.next()

    def end(self):
        self.child.close()

    def dispose(self):
        pass

    def __iter__(self):
        while True:
            t = self.child.next()
            if t is END:
                return
            yield t


class FileSource:
    """A drained child, read back from a temp file one page (one frame) at a time."""

    def __init__(self, ctx, owner, tf, schema):
        self.ctx, self.owner, self.tf, self.schema = ctx, owner, tf, schema
        self.codec = codec_for(schema)
        self.lease, self.gen = [], None

    def est_pages(self):
        return self.tf.num_pages

    def begin(self):
        self.lease = self.ctx.lease(1, self.owner.label())
        self.gen = read_tuples(self.tf, self.codec)

    def next(self):
        return next(self.gen, END)

    def end(self):
        if self.gen is not None:
            self.gen.close()
            self.gen = None
        self.ctx.release(self.lease)
        self.lease = []

    def dispose(self):
        self.tf.delete()

    def __iter__(self):
        while True:
            t = next(self.gen, END)
            if t is END:
                return
            yield t


def materialize(ctx, owner, child):
    """Run `child` to completion into a temp file owned by `owner`. While it runs it may use
    ctx.frames - 1 frames: one frame is held back for the page we write."""
    saved = ctx.reserve
    ctx.reserve = 1
    tf = TempFile(ctx, owner)
    lease = []
    try:
        child.open()
        lease = ctx.lease(1, owner.label())
        w = TupleWriter(tf, codec_for(child.schema))
        while True:
            t = child.next()
            if t is END:
                break
            w.add(t)
        w.close()
        child.close()
    finally:
        ctx.release(lease)
        ctx.reserve = saved
    return FileSource(ctx, owner, tf, child.schema)


# ===================================================================================== pipelined
class SeqScan(Operator):
    name = "seqscan"

    def __init__(self, ctx, node):
        super().__init__(ctx, node, [])
        self.table = node.table
        self.alias = getattr(node, "alias", None) or node.table
        if self.table not in ctx.db.catalog.list_tables():
            raise DBError("name", f"unknown table '{self.table}'")
        info = ctx.db.catalog.get_table(self.table)
        self.schema = Schema([Column(c.name, c.type, c.length, self.alias) for c in info.columns])
        self.pages_est = info.num_pages
        self.heap, self.gen = None, None

    def est_pages(self):
        return self.pages_est

    def label(self):
        return f"seqscan {self.table}" + (f" AS {self.alias}" if self.alias != self.table else "")

    def is_leaf(self):
        return True

    def open(self):
        self.heap, _ = self.ctx.open_scan_heap(self.table)
        self.gen = self.heap.scan_bytes()
        self._closed = False

    def next(self):
        pool = self.ctx.pool
        r0 = pool.reads
        try:
            item = next(self.gen, None)
        except DBError as e:
            if "no unpinned frame" in e.message:
                raise DBError("plan", f"{self.label()}: no frame left to read a page (B = {self.ctx.B} is too small "
                              f"for this plan)") from e
            raise
        self.page_reads += pool.reads - r0
        if item is None:
            return END
        self.tuples_out += 1
        return self.heap.codec.decode(item[1])

    def close(self):
        if self.gen is not None:
            self.gen.close()
            self.gen = None
        if self.heap is not None:
            self.ctx.close_scan_heap(self.heap)
            self.heap = None
        self._closed = True


class Filter(Operator):
    name = "filter"

    def __init__(self, ctx, node, child):
        super().__init__(ctx, node, [child])
        self.child = child
        self.schema = child.schema
        self.pred = compile_condition(node.cond, self.schema)

    def is_leaf(self):
        return self.child.is_leaf()

    def est_pages(self):
        return self.child.est_pages()

    def open(self):
        self.child.open()

    def next(self):
        while True:
            t = self.child.next()
            if t is END:
                return END
            self.comparisons += 1               # one per tuple examined
            if self.pred(t, None):
                self.tuples_out += 1
                return t

    def close(self):
        self.child.close()


class Project(Operator):
    """Keeps the listed columns in order. Does NOT remove duplicates (bag semantics)."""
    name = "project"

    def __init__(self, ctx, node, child):
        super().__init__(ctx, node, [child])
        self.child = child
        self.idx = [resolve(child.schema, a) for a in node.columns]
        aliases = getattr(node, "aliases", None) or [None] * len(self.idx)
        self.schema = Schema([Column(al, child.schema[i].type, child.schema[i].length) if al else child.schema[i]
                              for i, al in zip(self.idx, aliases)])
        dup = self.schema.first_duplicate()
        if dup is not None:
            raise DBError("schema", f"project would produce two columns named '{dup.qname}'")

    def is_leaf(self):
        return self.child.is_leaf()

    def est_pages(self):
        return self.child.est_pages()

    def open(self):
        self.child.open()

    def next(self):
        t = self.child.next()
        if t is END:
            return END
        self.tuples_out += 1
        return tuple(t[i] for i in self.idx)

    def close(self):
        self.child.close()


class Rename(Operator):
    name = "rename"

    def __init__(self, ctx, node, child):
        super().__init__(ctx, node, [child])
        self.child = child
        self.schema = child.schema.qualified(node.to)
        dup = self.schema.first_duplicate()
        if dup is not None:
            raise DBError("schema", f"rename[{node.to}] would give two columns the name '{dup.name}'")

    def is_leaf(self):
        return self.child.is_leaf()

    def est_pages(self):
        return self.child.est_pages()

    def open(self):
        self.child.open()

    def next(self):
        t = self.child.next()
        if t is not END:
            self.tuples_out += 1
        return t

    def close(self):
        self.child.close()


class Limit(Operator):
    """Returns the first n tuples, then stops pulling from its child."""
    name = "limit"

    def __init__(self, ctx, node, child):
        super().__init__(ctx, node, [child])
        self.child = child
        self.schema = child.schema
        self.n = node.n

    def is_leaf(self):
        return self.child.is_leaf()

    def est_pages(self):
        return self.child.est_pages()

    def open(self):
        self.child.open()

    def next(self):
        if self.tuples_out >= self.n:
            return END                           # never asks the child for tuple n + 1
        t = self.child.next()
        if t is not END:
            self.tuples_out += 1
        return t

    def close(self):
        self.child.close()


def check_setop_compat(op, left, right):
    if len(left) != len(right):
        raise DBError("schema", f"{op} needs the same number of columns on both sides "
                      f"({len(left)} vs {len(right)})")
    for i, (a, b) in enumerate(zip(left, right)):
        if a.type != b.type:
            raise DBError("schema", f"{op}: column {i + 1} is {a.type} on the left but {b.type} on the right")


class UnionAll(Operator):
    name = "union_all"

    def __init__(self, ctx, node, left, right):
        super().__init__(ctx, node, [left, right])
        check_setop_compat("union_all", left.schema, right.schema)
        self.schema = left.schema
        self.cur = 0

    def is_leaf(self):
        return all(c.is_leaf() for c in self.children)

    def est_pages(self):
        a, b = self.children[0].est_pages(), self.children[1].est_pages()
        return None if a is None or b is None else a + b

    def open(self):
        self.cur = 0
        self.children[0].open()

    def next(self):
        while self.cur < 2:
            t = self.children[self.cur].next()
            if t is not END:
                self.tuples_out += 1
                return t
            self.children[self.cur].close()      # one frame at a time: left is closed before right opens
            self.cur += 1
            if self.cur < 2:
                self.children[1].open()
        return END

    def close(self):
        for c in self.children:
            c.close()


# =========================================================================================== sort
class Sort(Operator):
    name = "sort"

    def __init__(self, ctx, node, child):
        super().__init__(ctx, node, [child])
        self.child = child
        self.schema = child.schema
        self.spec = [(resolve(self.schema, a), d) for a, d in node.keys]
        self.sorter, self.it = None, None

    def open(self):
        frames = self.ctx.frames
        src = self._source(self.child)
        self.sorter = ExternalSorter(self.ctx, self, self.schema, self.spec, frames)
        src.begin()
        try:
            self.sorter.consume(iter(src))
        finally:
            src.end()
            src.dispose()
        self.sorter.reduce()
        self.it = self.sorter.stream()
        self.info = {"frames": frames, "initial_runs": self.sorter.initial_runs}

    def next(self):
        t = next(self.it, END)
        if t is not END:
            self.tuples_out += 1
        return t

    def close(self):
        if self.it is not None:
            self.it.close()
        if self.sorter is not None:
            self.info["passes"] = self.sorter.passes
            self.sorter.close()
        super().close()


# ========================================================================= group_by and distinct
class AggSet:
    """Aggregate functions over one input schema. A state is a list with one entry per aggregate."""

    def __init__(self, specs):
        self.specs = specs                       # [(func, column index or None)]

    def init(self):
        out = []
        for f, _ in self.specs:
            out.append([0, 0] if f in ("count", "sum", "avg") else [None])
        return out

    def update(self, st, t):
        for s, (f, i) in zip(st, self.specs):
            if f == "count":
                s[0] += 1
            elif f in ("sum", "avg"):
                s[0] += t[i]
                s[1] += 1
            elif f == "min":
                v = t[i]
                if s[0] is None or v < s[0]:
                    s[0] = v
            else:
                v = t[i]
                if s[0] is None or v > s[0]:
                    s[0] = v

    def merge(self, st, other):
        for s, o, (f, _) in zip(st, other, self.specs):
            if f == "count":
                s[0] += o[0]
            elif f in ("sum", "avg"):
                s[0] += o[0]
                s[1] += o[1]
            elif f == "min":
                if s[0] is None or (o[0] is not None and o[0] < s[0]):
                    s[0] = o[0]
            else:
                if s[0] is None or (o[0] is not None and o[0] > s[0]):
                    s[0] = o[0]

    def finish(self, st):
        out = []
        for s, (f, _) in zip(st, self.specs):
            if f == "count" or f == "sum" or f in ("min", "max"):
                out.append(s[0])
            else:
                out.append(s[0] / s[1])
        return tuple(out)

    # partial states travel through temp files as flat tuples
    def flat(self, st):
        out = []
        for s, (f, _) in zip(st, self.specs):
            out.extend(s[:2] if f in ("count", "sum", "avg") else s[:1])
        return tuple(out)

    def unflat(self, vals):
        st, p = [], 0
        for f, _ in self.specs:
            if f in ("count", "sum", "avg"):
                st.append([vals[p], vals[p + 1]])
                p += 2
            else:
                st.append([vals[p]])
                p += 1
        return st


class GroupBy(Operator):
    """group_by in two modes. distinct is the same operator with keys = every column and no
    aggregates, so duplicate elimination and aggregation share one tested implementation."""
    name = "group_by"

    def __init__(self, ctx, node, children, mode, keys, aggs, schema, partial_cols, key_cols):
        super().__init__(ctx, node, children)
        self.mode = mode
        self.keys = keys                         # indexes into the input schema
        self.aggset = AggSet(aggs)
        self.schema = schema
        self.in_schema = children[0].schema
        self.partial_cols = partial_cols         # columns of a spilled partial record: key + flat states
        self.key_cols = key_cols
        self.it, self.sorter = None, None
        self.leases = []
        self.gen = None
        self.nk = len(keys)

    def label(self):
        return f"{self.name}({self.mode})"

    def _tuples(self, sources):
        for s in sources:
            s.begin()
            try:
                yield from s
            finally:
                s.end()
                s.dispose()

    def _key(self, t):
        return tuple(t[i] for i in self.keys)

    def open(self):
        frames = self.ctx.frames
        sources = [self._source(c) for c in self.children]
        if self.nk == 0:
            self.it = self._global(sources)
        elif self.mode == "sort":
            self.it = self._open_sort(sources, frames)
        else:
            self.it = self._open_hash(sources, frames)

    # ---- no grouping columns ---------------------------------------------------------------------
    def _global(self, sources):
        st, n = self.aggset.init(), 0
        for t in self._tuples(sources):
            self.aggset.update(st, t)
            n += 1
        if n == 0:
            for f, _ in self.aggset.specs:
                if f != "count":
                    raise DBError("aggregate", f"{f.upper()} of an empty input is undefined")
        return iter([self.aggset.finish(st)])

    # ---- sort based -------------------------------------------------------------------------------
    def _open_sort(self, sources, frames):
        self.sorter = ExternalSorter(self.ctx, self, self.in_schema, [(k, "asc") for k in self.keys], frames)
        self.sorter.consume(self._tuples(sources))
        self.sorter.reduce()
        return self._group_sorted(self.sorter.stream())

    def _group_sorted(self, stream):
        cur, st = None, None
        for t in stream:
            k = self._key(t)
            self.comparisons += 1                # key vs previous key
            if cur is None or k != cur:
                if cur is not None:
                    yield cur + self.aggset.finish(st)
                cur, st = k, self.aggset.init()
            self.aggset.update(st, t)
        if cur is not None:
            yield cur + self.aggset.finish(st)

    # ---- hash based --------------------------------------------------------------------------------
    def _gsize(self):
        ksize = make_sizer(self.key_cols)
        extra = 0
        for (f, _), c in zip(self.aggset.specs, self.schema.cols[self.nk:]):
            extra += (2 + (c.length or 0)) if c.type == VARCHAR else 8
        return lambda key: ksize(key) + extra

    def _open_hash(self, sources, frames):
        ctx = self.ctx
        cap = frames - 2                          # set pages; plus the input frame and one spill-writer frame
        if cap < 1:
            raise DBError("plan", f"{self.label()} needs at least 3 frames (it has {frames}); raise --frames")
        gsize = self._gsize()
        pcodec = codec_for(Schema(self.partial_cols))
        groups, counter = {}, PageCounter(gsize, cap)
        set_lease = ctx.lease(cap, self.label())
        spills = []

        def spill():
            wl = ctx.lease(1, self.label())
            tf = TempFile(ctx, self)
            w = TupleWriter(tf, pcodec)
            for k, st in groups.items():
                w.add(k + self.aggset.flat(st))
            w.close()
            ctx.release(wl)
            groups.clear()
            counter.reset()
            spills.append(tf)

        for t in self._tuples(sources):
            k = self._key(t)
            self.comparisons += 1                 # hash table lookup
            st = groups.get(k)
            if st is None:
                if not counter.try_add(k):
                    spill()
                    counter.try_add(k)
                st = groups[k] = self.aggset.init()
            self.aggset.update(st, t)

        if not spills:                             # everything fit: no I/O at all
            self.leases = set_lease
            self.info["spilled"] = False
            return self._emit_memory(groups)
        if groups:
            spill()
        ctx.release(set_lease)
        self.info["spilled"] = True
        self.info["spill_files"] = len(spills)
        parts = self._partition_spills(spills, frames, pcodec)       # blocking work stays inside open()
        return self._merge_partitions(parts, frames, pcodec)

    def _emit_memory(self, groups):
        try:
            for k, st in groups.items():
                yield k + self.aggset.finish(st)
        finally:
            self.ctx.release(self.leases)
            self.leases = []

    def _partition_spills(self, spills, frames, pcodec):
        """Phase 2: partition the spilled partial records by key hash (P = F - 1 partitions)."""
        ctx = self.ctx
        P = frames - 1
        parts = [TempFile(ctx, self) for _ in range(P)]
        lease = ctx.lease(P + 1, self.label())     # P partition pages + the page being read
        try:
            writers = [TupleWriter(tf, pcodec) for tf in parts]
            for sp in spills:
                for rec in read_tuples(sp, pcodec):
                    writers[det_hash(rec[:self.nk], 1) % P].add(rec)
                sp.delete()
            for w in writers:
                w.close()
        finally:
            ctx.release(lease)
        self.info["partition_pages"] = [tf.num_pages for tf in parts]
        return parts

    def _merge_partitions(self, parts, frames, pcodec):
        """Phase 3: merge each partition's partial states in memory (sort-based fallback on overflow)."""
        for tf in parts:
            yield from self._merge_partition(tf, frames, pcodec)

    def _merge_partition(self, tf, frames, pcodec):
        ctx = self.ctx
        if tf.num_pages == 0:
            tf.delete()
            return
        cap = frames - 1
        gsize = self._gsize()
        lease = ctx.lease(cap + 1, self.label())
        try:
            groups, counter, overflow = {}, PageCounter(gsize, cap), False
            for rec in read_tuples(tf, pcodec):
                k, st_new = rec[:self.nk], self.aggset.unflat(rec[self.nk:])
                self.comparisons += 1
                st = groups.get(k)
                if st is None:
                    if not counter.try_add(k):
                        overflow = True
                        break
                    groups[k] = st_new
                else:
                    self.aggset.merge(st, st_new)
            if not overflow:
                ctx.release(lease)
                lease = []
                tf.delete()
                for k, st in groups.items():
                    yield k + self.aggset.finish(st)
                return
        finally:
            ctx.release(lease)
        # the partition has more distinct groups than memory: sort it and combine neighbours instead
        self.notes.append("partition overflow: sort-based merge")
        groups.clear()
        sorter = ExternalSorter(ctx, self, Schema(self.partial_cols),
                                [(i, "asc") for i in range(self.nk)], frames)
        sorter.consume(read_tuples(tf, pcodec))
        sorter.reduce()
        cur, st = None, None
        for rec in sorter.stream():
            k = rec[:self.nk]
            self.comparisons += 1
            if cur is None or k != cur:
                if cur is not None:
                    yield cur + self.aggset.finish(st)
                cur, st = k, self.aggset.unflat(rec[self.nk:])
            else:
                self.aggset.merge(st, self.aggset.unflat(rec[self.nk:]))
        if cur is not None:
            yield cur + self.aggset.finish(st)
        tf.delete()

    def next(self):
        t = next(self.it, END)
        if t is not END:
            self.tuples_out += 1
        return t

    def close(self):
        if self.it is not None and hasattr(self.it, "close"):
            self.it.close()
        if self.sorter is not None:
            self.info["passes"] = self.sorter.passes
            self.sorter.close()
        self.ctx.release(self.leases)
        self.leases = []
        super().close()


def build_group_by(ctx, node, child_ops, mode, distinct):
    """Constructs GroupBy for a group_by node, a distinct node, or a set-union node."""
    first = child_ops[0]
    in_schema = first.schema
    if distinct:
        keys = list(range(len(in_schema)))
        aggs, out_cols = [], list(in_schema.cols)
        partial = list(in_schema.cols)
        key_cols = list(in_schema.cols)
        if len(child_ops) == 2:
            check_setop_compat("union", first.schema, child_ops[1].schema)
    else:
        keys = [resolve(in_schema, a) for a in node.keys]
        out_cols = [in_schema[i] for i in keys]
        key_cols = list(out_cols)
        partial = list(out_cols)
        aggs = []
        for func, arg, alias in node.aggregates:
            if arg == "*":
                idx, ctype = None, INT
            else:
                idx = resolve(in_schema, arg)
                ctype = in_schema[idx].type
                if func in ("sum", "avg") and ctype != INT:
                    raise DBError("type", f"{func.upper()} needs an INT column, but {attr_str(arg)} is {ctype}")
            name = alias or (f"{func}_star" if arg == "*" else f"{func}_{attr_str(arg).replace('.', '_')}")
            if func in ("count", "sum"):
                out_cols.append(Column(name, INT, 8))
            elif func == "avg":
                out_cols.append(Column(name, REAL))
            else:
                src = in_schema[idx]
                out_cols.append(Column(name, src.type, src.length))
            aggs.append((func, idx))
            if func in ("count", "sum", "avg"):
                partial.append(Column(f"_s{len(partial)}", INT, 8))
                partial.append(Column(f"_s{len(partial)}", INT, 8))
            else:
                src = in_schema[idx]
                partial.append(Column(f"_s{len(partial)}", src.type, src.length))
    out_schema = Schema(out_cols)
    dup = out_schema.first_duplicate()
    if dup is not None:
        raise DBError("schema", f"group_by output would have two columns named '{dup.qname}'")
    return GroupBy(ctx, node, child_ops, mode, keys, aggs, out_schema, partial, key_cols)
