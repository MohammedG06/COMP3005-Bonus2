# DESIGN_LOG.md

Per the handout, this log lists what I tried, what broke, and where AI help gave me something wrong, slow or
quietly incorrect, and how I found it. Only things that really happened are here.

## 2026-10-17: Engine refactor and Module 1

**Goal:** turn my Bonus 1 engine into a library and build the storage engine (pages, heap files, buffer pool,
catalog, loader, `storage` CLI), with a test for every checklist item.

**Result:** 65 tests pass, including the spec's worked examples (page 3.2, buffer 5.3).

**Where the AI was wrong:**

1. *Wrong belief about CLOCK.* The first flooding test assumed CLOCK would get hits on the repeated 80 page scan at
   B = 64 while LRU gets none. The test failed: CLOCK also gets 0 hits (800 misses). On a cycle bigger than the pool,
   both policies evict the page needed soonest, so CLOCK acts like FIFO. I confirmed it with B = 96, where both get
   720 hits. So neither policy fixes sequential flooding, and the "which would I ship" answer has to come from the
   other traces.
2. *Pinned page leak.* `HeapFile.scan()` wrapped `scan_bytes()` in a second generator. Closing the outer one (what a
   LIMIT does) left one frame pinned. `test_abandoned_scan_releases_its_pin` caught it. I printed the pinned frames
   after each step to narrow it down. Fixed with `try/finally: inner.close()`. It would have broken Module 2.
3. *Wrong test assumption about heap order.* A persistence test assumed scans return rows in insertion order. With
   first fit and variable length records, a small record lands on an earlier page (first difference at row 419).
   The code was right and the test was wrong. The test now compares sorted lists.
4. *Bad code caught in review.* The first `HeapFile.insert_bytes` set the dirty flag using `dir()`. It worked by
   accident, so I replaced it with an explicit variable before running it.

**Still open:** the spec says the largest record is 4088 bytes, but an insert also needs a 4 byte slot, so the real
limit is 4084. The code uses 4084. This is my assumption.

## 2026-10-25: Module 2, execution engine

**Goal:** iterator operators, external sort, three join algorithms, hash and sort based group_by and distinct, set
operators, all within a frame budget, plus the `exec` CLI and 34 tests.

**Where the AI was wrong:**

1. *Blocking work hidden in a generator.* The first hash group_by did its partition phase inside a generator, so it
   ran on the first `next()` instead of in `open()`, which is where the spec puts blocking work. I found it by
   re-reading the code against the spec. Fixed by moving the partitioning into a normal function called from `open`.
2. *Temp file leak.* The same code only deleted a partition's temp file when it overflowed, so a normal run left
   files behind. Found in review. `test_nothing_left_behind_after_any_run` now checks for leftover temp files, pinned
   frames and leased frames.
3. *Hash join wrote hundreds of empty pages.* I followed "B - 1 partitions" literally. A 20 page join at B = 256 cost
   716 I/Os against about 120 for the textbook `3(M+N)`, because 255 partitions for 480 tuples means one nearly empty
   page each. I found it by reading the measurement table. Fixed by choosing the partition count from the build size.
4. *The reference evaluator ignored aliases.* It did not support `alias` on `seqscan`, so the self join test failed
   in the reference and not in the engine.
5. *Wrong test arithmetic.* I assumed 19 rows per page for a table padded with 150 characters (it is 24), and a
   "type mismatch" test case was actually type compatible. The engine was right both times.

**Still open:** the spec says a scan costs M reads, but mine also reads the directory page, so every measured number
is one higher per scan. Documented in DESIGN_M2.md section 6.

## 2026-10-26: Modules 3 and 4 and the shell

**Goal:** SQL front end, statistics, rewrites, cost based join ordering, physical plans, and one shell that wires
everything together. I tested against the handout's examples, against SQLite for the SQL front end, and against my
reference evaluator for the whole pipeline at B = 4, 6 and 16.

**Where the AI was wrong:**

1. *A predicate silently disappeared.* In the join order search, a one-relation predicate was only applied when that
   relation was added as the inner side of a join. The first relation of a left-deep chain is never an inner, so its
   predicate (`b.x > 10`) was dropped and a query returned 1,855 rows instead of 261. The hand written tests all
   passed. The random whole-pipeline test found it. Fix: attach one-relation predicates to their relation before the
   search, and have the planner check every predicate is applied exactly once.
2. *Wrong column order after reordering joins.* The search can start from a different relation than the SQL did, so
   the physical join output its columns in another order, and rule 7 then deleted the final projection because it
   matched the logical order. Same row count, different rows. The random test found it as a mismatch with equal
   counts. My first fix only covered the swapped hash build side, and the fuzz found the other case, so
   `Planner.plan` now restores the logical order in one place.
3. *Stale estimates from `id()` reuse.* The estimator cached by `id(node)`, and Python reuses the id of a garbage
   collected object, so a new query got an old query's estimate (10,000 rows instead of 40). A unit test only failed
   after an unrelated change shifted memory allocation. Fix: keep the node alive inside the cache.
4. *`ANALYZE` hung.* Executor scans read through a private page namespace, so they read from disk, but the table's
   new pages were still dirty in the pool. The scan read an unwritten zero page whose "next directory page" is 0, and
   looped on page 0. Found by running it. Fix: flush before executing, and make the heap refuse a directory chain that
   loops.
5. *A script lost all its output.* `Engine.run` returned results at the end, so when the last statement had an error,
   nothing printed. Found in the first scripted shell session. It is now a generator.
6. *The q-error report blamed the estimator for LIMIT.* A node under `LIMIT 5` stops early, so actual rows is 5
   against an estimate of 468 (q-error 94), which is not an estimation error. Found by reading the output. Such nodes
   are now flagged and excluded.
7. *Selections could not move through a projection*, so a predicate on a subquery column stayed above the subquery
   instead of reaching the group_by. Found by a test on rule 5.
8. *Arithmetic slips in my own tests* (an off-by-one column in an expected error position, twice). The engine was
   right both times.
9. *The rule switches did nothing.* Turning rules 1, 2 and 3 off changed nothing, because my join order search quietly
   did the same work (splitting conjunctions, attaching single table predicates, treating cross product equalities as
   join keys). I found it by reading the measurement table and asking why three rules were worth exactly zero. Fix: the
   search now does each step only when the matching rule is on. Re-checked with 1,040 random queries with random
   subsets of rules off (`scripts/fuzz_pipeline.py`).

**Module 3 note:** it passed 1,500 random queries against SQLite on the first run, so most of its real problems were
design questions rather than bugs: where ORDER BY goes when the column is not selected, accepting bare aliases, and
accepting aggregates in WHERE only to report them.

**Still open:**

* The cost model ignores materialization and directory pages, so some estimated I/O is lower than the actual.
* The spec's worked example says 550 where the formula gives 650 (see DESIGN_M4.md section 7).

## 2026-11-10: Full re-check and reports

**Goal:** run everything again from the start, fix what was wrong, and write the M1 and M2 reports. I ran the unit
tests, the fuzz script, every CLI against the spec text, and a university dataset (792 query runs at B = 4, 5, 8 and
32 with each rewrite rule off in turn, all equal to the reference evaluator), on Python 3.14.6.

**Where the AI was wrong:**

1. *ANALYZE was not idempotent.* The M4 checklist says "Running ANALYZE twice changes nothing". The existing test only
   compared the logical statistics, which were identical, so it passed. Running `opt analyze` three times and comparing
   the bytes of `catalog.db` showed the file growing each time (40,960, then 49,152, then 53,248 bytes): ANALYZE
   deleted its old records and inserted new ones, and dead slots are never reclaimed. Fix: the catalog rewrites itself
   from its live records into the pages it already has (`HeapFile.rewrite_all`). Regression tests compare the file
   bytes after repeated runs and bound the catalog's growth under repeated count updates.
2. *A measurement that could not show what it claimed.* `m2_measure.py skew` ran uniform and skewed data at B = 4
   only. At B = 4 even uniform data overflows its hash partitions (10 page partitions against 3 frames), so both rows
   said "block nested loop fallback" and the table could not show a cost of skew. I found it when I tried to write the
   analysis from the table. Fix: the script now runs B = 4 and B = 16 (at 16, uniform fits and skew overflows).
3. *A wrong explanation in a first report draft.* I explained LRU on the repeated 80 page scan with a muddled sentence
   about which pages sit "k+1 to k+64 behind". It did not say what actually happens. I found it on re-reading against
   the numbers and replaced it with: 79 other pages are read before page k is needed again, which is more than the 64
   frames, so page k was evicted.

**Result:** the optimizer and executor did not change, only the catalog rewrite. 170 unit tests pass, and 360 fuzz
queries had 0 mismatches. I also ran the 170 tests and the 20 query workload on Windows (Python 3.14.7) and got the
same numbers as on Linux.

## 2026-11-10: M4 measurements on my own data

The course dataset was not published, so REPORT_M4 uses data and 20 queries I generated (`scripts/make_demo_db.py`).

* The city/zip correlation gave a q-error of 3.0 (339 estimated, 1008 actual).
* One query at B = 4 picked nested loop (145 I/O) when sort-merge cost 88. I did not prove the correlation caused it,
  and the report says so.
* Query 9 has an estimate of 80 against an actual of 342. This is a cost model gap. It is documented and not fixed.
