# DESIGN_M2.md: execution engine

Assumption: the plan JSON schema and sample plans were not provided, so the validator and the test plans follow the operator JSON shown in Module 2 section 2, and every plan used in the reports was written by me.

Matches the code in `dbms/execution/`. Run `python exec.py trace --db DIR --plan plan.json --frames B`
to see the numbers described here on a real plan.

## 1. Memory model (how B is enforced)

- The buffer pool has exactly B frames. That is the whole memory of a plan.
- A scan pins one pool frame at a time (a real pinned page).
- Every other operator takes working memory with `pool.lease(n)`. Leased frames are taken out of
  circulation, so pinned + leased can never exceed B: the pool refuses and the plan fails with
  "needs N more frames" (a `plan` error, never a crash).
- Tuples kept in memory are counted in pages with the slotted-page packing arithmetic
  (`PageCounter`, 4088 bytes per page, record + 4 byte slot). An operator never keeps more tuples than
  its leased frames can hold.
- Temp files go through `DiskManager` one page at a time. Every transfer is added to the page counters of
  the operator that owns the file.
- How I know the largest number of frames held: `pool.peak_held` is updated on every pin and every lease.
  `exec run --json` prints it (`peak_frames`) and the tests assert `peak_frames <= B` for every plan.
- Each scan uses its own page namespace (its own file id on the same DiskManager) and drops its cached
  pages when it closes, so two scans never share pages and counters do not depend on the pool policy.

## 2. How B is divided (the rule)

- Pipelined operators (filter, project, rename, limit, union_all) hold no frames of their own.
  They hold one tuple in flight. The frame belongs to the scan under them.
- An input is a **leaf pipeline** if it is a scan, possibly under filter / project / rename / limit /
  union_all. A leaf pipeline holds exactly one frame while it streams.
- A memory-hungry consumer (sort, group_by, distinct, any join, intersect, except, set union) reads each
  input like this:
  - leaf pipeline: stream it, 1 pinned frame;
  - anything else (a join, a sort, a group_by below it): **materialize it first**. The child runs alone
    with B - 1 frames (one frame is held back for the page we write), is drained into a temp file owned by
    the consumer, and is closed. The consumer then reads the file back with 1 leased frame.
- Result: two memory-hungry operators are never active at the same moment, so every plan fits in B frames,
  including B = 4 (tests run three-way joins in all 9 algorithm combinations at B = 4).
- The price of materializing is extra page writes and reads. They are visible in the consumer's counters
  and the optimizer (Module 4) has to count them.

| operator | frames while it works |
|---|---|
| seqscan | 1 pinned |
| filter, project, rename, limit, union_all | 0 |
| sort | run generation: F-1 leased + 1 input. Merge passes: fan-in F-1 readers + 1 writer page. Final pass: one frame per run still being merged |
| group_by / distinct, sort mode | same as sort, then groups are combined as tuples stream out |
| group_by / distinct, hash mode | set of F-2 pages + 1 input + 1 spill writer. After a spill: F-1 partition pages + 1 reader. Then each partition: F-1 set pages + 1 reader |
| nested_loop_join | block of F-2 pages + 1 outer input + 1 inner input |
| sort_merge_join | each sort uses F in turn. Merge: 3 leased (left reader, right reader, equal-key run buffer) |
| hash_join | partition phase: P leased + 1 input. Probe: build table (at most F-1 pages) + 1 probe reader |
| intersect, except | each sort uses F in turn. Merge: 2 leased readers |
| set union | distinct (sort mode) over the concatenation of both inputs |

F = B - 1 while an operator is being materialized by its parent (the parent's writer page), else B.

## 3. External sort

- Run generation: F-1 pages of tuples are collected, sorted with the language sort (one run that fits in
  memory, allowed by the spec) using a comparator that counts every key comparison, and written as a run.
- If the whole input fits in F-1 pages nothing is written (one pass, zero writes).
- Merge: fan-in k = F-1, heap based, ties broken by run order (stable). A leftover single run is carried to
  the next pass untouched.
- Final pass streams to the parent and writes nothing. `to_file()` (used by sort_merge_join and the set
  operators) writes the last pass to one sorted file instead.
- Page accounting for the spec's example (N = 8 pages, B = 3, a 1 page scan under the sort):
  - run size F-1 = 2 pages, so 4 runs; pass 0 writes 8 pages (the scan reads 8 + 1 directory page);
  - pass 1 merges 4 runs into 2 (2 at a time): 8 reads, 8 writes;
  - pass 2 merges the last 2 and streams: 8 reads, 0 writes.
  - Sort alone: 16 reads, 16 writes, 3 passes. A test asserts exactly these numbers.
- Variant: the spec fills B frames per run (3 runs for this example, 22 reads / 14 writes). I keep one
  frame for the incoming scan page, so runs are B-1 pages. That is the "own documented variant" the
  spec allows, and the passes I measure follow `1 + ceil(log_{B-1}(ceil(N/(B-1))))`.

## 4. Joins

- **Block nested loop**: block = F-2 pages of the outer, the inner is rescanned once per block (a leaf
  inner is closed and reopened, a materialized inner is read again from its file). Comparisons are exactly
  |outer| x |inner|. Page reads match `M + ceil(M/(B-2)) N` plus one directory page per scan opened.
- **Sort-merge**: both inputs are sorted into sorted temp files (every pass writes), then two cursors advance.
  The run of equal keys on the right goes into one frame; if it does not fit, the whole run is written to a
  temp file and re-read for each left tuple with that key (a note appears in `exec trace`). Output is
  left columns then right columns.
- **Grace hash join** (left = build side, right = probe side, equality only):
  - P partitions = `min(B-1, max(2, ceil(build_pages / (B-2))))`. The spec's allotment is B-1 partitions, but
    using all B-1 when the build side is small wrote hundreds of nearly empty pages (found in my first
    measurement, see DESIGN_LOG.md).
  - Both inputs are partitioned with a deterministic hash (crc32 of the key, not Python's `hash`, which
    changes between runs).
  - A partition pair is joined in memory if the build partition has at most B-1 pages.
  - **Overflow**: a build partition with more than B-1 pages is repartitioned once with a different hash
    seed. A sub-partition that still overflows falls back to block nested loop for that pair. A single hot
    key can never be split by hashing, so it always ends in the fallback. `exec trace` prints each overflow
    action and the partition sizes.

## 5. group_by, distinct and the set operators

- distinct is group_by with every column as key and no aggregates, so both share one implementation.
- Sort mode: external sort on the keys, then one pass combining neighbours (1 comparison per tuple).
- Hash mode: an in-memory table of F-2 pages. If a new group does not fit, all current partial states are
  written to a spill file and the table is emptied. After the input ends, the spill files are partitioned by
  key hash, and each partition is merged in memory. A partition with more groups than fit is merged by an
  external sort instead (always terminates). COUNT, SUM, AVG (kept as sum and count), MIN and MAX all merge.
- No grouping columns: one state, one pass. Empty input gives one row for COUNT only; MIN, MAX, SUM, AVG of
  nothing is an `aggregate` error. With grouping columns, empty input gives no rows.
- intersect / except: both inputs sorted on all columns, then one merge, each result tuple once (set
  semantics). union_all concatenates; union is distinct over the concatenation.

## 6. Counters

- `tuples_out`: tuples returned by `next()`.
- `comparisons`: filter = 1 per tuple examined; sort = every key comparison (run sort + merge heap);
  group_by sort = 1 per tuple; group_by hash = 1 per hash lookup (+1 per partial record merged);
  nested loop = one per pair; sort-merge = key comparisons in the merge plus those in its sorts;
  hash join = for each probe tuple the size of its bucket (1 if empty), pairs compared in a fallback.
- `page_reads`, `page_writes`: disk manager calls this operator caused, including its own temp files.
  `exec trace` prints each node's counters **including its children** (that is what makes a join row read
  like the textbook formula); `--json` also has `own_reads` / `own_writes`. The `totals` in `exec run
  --json` are the sum of the own counters, so no page is counted twice.
- A scan opened once reads its data pages plus the directory page(s). The textbook formulas count only
  data pages, so measured reads are higher by one per scan open.

## 7. Validation and errors

- The JSON is checked by `plan_json.from_dict(..., "physical")`: the message names the node, the field
  and the problem, for example `Node 'sort': missing required field 'keys'. [at root]`. Malformed JSON
  reports the line and column.
- `build_operator` then builds every operator before anything is opened: unknown tables or columns, type
  errors, duplicate output names and set-operation mismatches all raise here, so a bad plan does no I/O.
- Running out of frames is a `plan` error that says how many frames are missing.
- Temp files live in `<db>/tmp`, are deleted when their operator closes, and the runner checks at the end
  that no frame is pinned or leased and no temp file is left.

## 8. What is not counted (honest limits)

- Python object overhead (a tuple is far bigger than its encoded bytes). I count encoded bytes.
- The in-memory sort of one run builds a second list of (key, tuple) pairs for a moment.
- `execute()` and `exec run` collect the final result in a list for printing. That is the caller's memory.
- Catalog lookups while building the plan go through the pool but belong to no operator.
