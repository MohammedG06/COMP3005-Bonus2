# DESIGN_LOG.md

Rule from the handout: each checkpoint needs at least two places per module where AI gave
something wrong, slow or quietly incorrect, and how it was found. Only things that really
happened go here. Add your own entries as you run, read and change this code.

## 2026-10-17: engine refactor and Module 1 (first working session with Claude)

**Goal:** turn the Bonus 1 engine into a library (handout 1.3) and build Module 1 (pages, heap
files, buffer pool, catalog, loader, `storage` CLI), with a test for every checklist item.

**What was done:** new `dbms` package; tokenizer, condition parser, tree + JSON, reference
evaluator with bag semantics; slotted pages, directory based heap files, LRU and CLOCK pool,
catalog, CSV loader. 65 tests pass. The spec's worked examples (page 3.2, buffer 5.3) are tests.

**Where the AI was wrong (real):**

1. *Wrong belief about CLOCK.* The first version of the flooding test asserted that CLOCK would
   get hits on the repeated 80 page scan at B = 64 while LRU gets none. The test failed: CLOCK also
   gets 0 hits (800 misses). On a cycle bigger than the pool both policies evict the page that will
   be needed soonest, so CLOCK behaves like FIFO there. Found by running the test, then confirmed by
   running the same trace with B = 96, where both policies get 720 hits. Consequence for the report:
   neither policy fixes sequential flooding, so "which would you ship" must be argued from the
   other traces.
2. *Pinned page leak.* `HeapFile.scan()` wrapped `scan_bytes()` in a second generator. Closing the
   outer generator (what a LIMIT does) did not close the inner one, so one frame stayed pinned, even
   after `gc.collect()`. Found by `test_abandoned_scan_releases_its_pin`. I narrowed it down by printing
   the pinned frames after each step: `scan_bytes()` alone was fine, the wrapper was not. Fixed with
   `try/finally: inner.close()`. This would have broken Module 2, where operators need all their frames back after `close`.
3. *Wrong test assumption about heap order.* A persistence test assumed scan returns rows in
   insertion order. With first fit and variable length records, a small record goes into an earlier
   page that still has room (first difference was at row 419). The code was right and the test was
   wrong, which matches the spec ("heap order"). The test now compares sorted lists.
4. *Smell caught on review.* The first draft of `HeapFile.insert_bytes` decided the dirty flag with
   an expression using `dir()`. It worked by accident. I replaced it with an explicit variable
   before it was ever run.

**What broke / still open:**

* The spec says the largest storable record is 4088 bytes, but an insert needs the record plus a 4 byte
  slot, so the real limit is 4084. The code uses 4084. Worth confirming with the professor.
* (add anything that breaks when you run it on your machine)

## 2026-10-25: Module 2 (execution engine) with Claude

**Goal:** iterator operators, external sort, three joins, hash and sort based group_by/distinct,
set operators, all inside a frame budget, plus the `exec` command line and 34 tests.

**Where the AI was wrong (real):**

1. *Blocking work hidden in a generator.* The first hash group_by wrote its partition phase inside a
   generator, so it ran on the first `next()` instead of in `open()` (the spec puts all blocking work in
   `open`). Found by re-reading the code against the spec before running anything. Fixed by splitting
   the partitioning into a normal function called from `open`.
2. *Temp file leak on the success path.* The same code deleted a partition's temp file only when it
   overflowed, so a normal run left files behind. Also found in review. The test
   `test_nothing_left_behind_after_any_run` now checks that no temp directory, pinned frame or leased
   frame survives a run.
3. *Hash join wrote hundreds of empty pages.* I followed the spec's "B - 1 partitions" literally. In the
   first measurement run, a 20 page join at B = 256 cost 716 page I/Os against about 120 for the textbook
   `3(M+N)`: 255 partitions for 480 tuples means one nearly empty page per partition. I found it by reading
   the measurement table, not from a test. Fixed by choosing the number of partitions from the build size.
4. *The oracle ignored aliases.* My reference evaluator, which the differential tests compare against, did
   not support the `alias` on `seqscan`, so the self join test failed in the reference and not in the engine.
   Found by that test failing.
5. *Wrong test arithmetic.* I assumed 19 rows per page for a table padded with 150 characters (it is 24), and
   wrote a "type mismatch" case that was actually type compatible. Found when the tests failed; the engine
   was right both times.

**What broke / still open:**

* The spec says each scan costs M reads; mine also reads the directory page, so every measured number is
  higher by one per scan opened. Documented in DESIGN_M2.md section 6.
* (add anything that breaks when you run it on your machine)

## 2026-10-26: Modules 3 and 4 and the integration shell (with Claude)

**Goal:** SQL front end, statistics, rewrites, cost based join ordering, physical plans, and one shell that
wires everything. Tests for the handout's examples, plus private random testing against SQLite (the SQL
front end) and against the reference evaluator (the whole pipeline at B = 4, 6 and 16).

**Where the AI was wrong (real):**

1. *A predicate silently disappeared.* In the join order search, a predicate that used a single relation was
   only applied when that relation was added as the inner side of a join. The first relation of a left-deep chain
   is never an inner, so its predicate (`b.x > 10`) was dropped and the query returned 1,855 rows instead of 261.
   The hand written tests all passed; the random whole-pipeline test found it. Fix: attach one-relation predicates
   to their relation before the search, and make the planner check that every predicate was applied exactly once.
2. *Wrong column order after reordering joins.* The search may start from a different relation than the SQL did,
   so the physical join emitted its columns in another order; rule 7 then deleted the final projection because it
   matched the *logical* order. Same row count, different rows. Found by the random test (a mismatch with equal
   counts). A first fix covered only the swapped hash build side and the fuzz found the other case, so the
   invariant is now enforced in one place (`Planner.plan` restores the logical order).
3. *Stale estimates from `id()` reuse.* The estimator cached by `id(node)`. Python reuses the id of a garbage
   collected object, so a new query could get an earlier query's estimate (10,000 rows instead of 40). A unit test
   failed only after an unrelated change shifted memory allocation. Fix: keep the node alive inside the cache.
4. *`ANALYZE` hung forever.* Executor scans read through a private page namespace (so they never share cached
   pages), which means they read from disk, but the table's newly inserted pages were still dirty in the pool. The
   scan read an unwritten zero page whose "next directory page" is 0, so it looped on page 0. Found by running it.
   Fix: flush before executing, and make the heap refuse to follow a directory chain that loops.
5. *A script lost all its output.* `Engine.run` collected every result and returned them at the end, so when the
   last statement had an error, nothing printed. Found by the first scripted shell session. Now a generator.
6. *The q-error report blamed the estimator for LIMIT.* A node under `LIMIT 5` stops early, so "actual rows" is 5
   against an estimate of 468 (q-error 94). That is not an estimation error. Found by reading the measurement output;
   such nodes are now flagged and excluded.
7. *Selections could not travel through a projection*, so a predicate on a subquery column stayed above the
   subquery instead of reaching the group_by (found by a test on rule 5).
8. *Arithmetic slips in my own tests* (an off-by-one column in an expected error position, twice). The engine
   was right both times.

9. *The rule switches did nothing.* The "turn one rewrite rule off" measurement showed rules 1, 2 and 3 changing
   nothing at all. Cause: my join order search quietly did the same work (split conjunctions, attach single table
   predicates to their table, treat cross product equalities as join keys), so switching a rule off did not switch
   its effect off. Found by reading the measurement table and asking why three rules were worth exactly zero. Fix:
   the search now does each of those steps only when the matching rule is on (otherwise single table predicates run
   in a filter above the finished join, and equalities from a WHERE over a cross product are not join keys).
   Re-validated with 1,040 random queries run with random subsets of rules off (`scripts/fuzz_pipeline.py`).

**Module 3 specifically:** it passed 1,500 random queries against SQLite on the first run, so most of its real
problems came from the design questions, not bugs (where ORDER BY goes when the column is not selected, accepting
bare aliases, accepting aggregates in WHERE only to report them). **You need at least two entries of your own for
Module 3**: try the unusual queries listed in the final message and log what you find.

**What broke / still open:**

* The cost model ignores materialization and directory pages, so some estimated I/O is lower than the actual.
* The spec's worked example says 550 where the formula gives 650 (see DESIGN_M4.md section 7).
* (add anything that breaks when you run it on your machine or on the course dataset)

## 2026-11-10: handoff check, Windows/3.14 check, reports (a new Claude session, Sonnet 5.5)

**Goal:** take over the project from the earlier session, run everything, fix what is wrong, draft the M1 and M2 reports.
Ran: 168 unit tests, the fuzz script, every CLI against the spec text, the course university data (792 query runs
at B = 4, 5, 8, 32 with each rewrite rule off in turn, all equal to the reference evaluator), Python 3.14.6.

**Where the AI was wrong (real, found in this session):**

1. *ANALYZE was not idempotent.* The M4 checklist says "Running ANALYZE twice changes nothing". The earlier session's
   test only compared the logical statistics, which were identical, so it passed. Running `opt analyze` three times and
   comparing the bytes of `catalog.db` showed the file growing each time (40,960 then 49,152 then 53,248 bytes): ANALYZE
   deleted its old records and inserted new ones, and dead slots are never reclaimed. Fix: the catalog rewrites itself from its
   live records into the pages it already has (`HeapFile.rewrite_all`). Regression tests compare the bytes of the
   file after repeated runs and bound the growth of the catalog under repeated count updates.
2. *A measurement that could not show what it claimed.* `m2_measure.py skew` ran both the uniform and the skewed data at
   B = 4 only. At B = 4 even uniform data overflows its hash partitions (10 page partitions against 3 frames), so both rows
   said "block nested loop fallback" and the table could not show a cost of skew. Found when I tried to write the analysis
   from the table. Fix: the script now runs B = 4 and B = 16 (at 16 uniform fits and skew overflows).
3. *A wrong explanation in my first report draft.* I explained LRU on the repeated 80 page scan with a muddled sentence about
   which pages sit "k+1 to k+64 behind". It did not say what happens. Found on re-reading it against the numbers; replaced with: 79
   other pages are read before page k is needed again, more than the 64 frames, so page k was evicted.

**Also done:** `scripts/make_traces.py` (five of our own traces, because the course traces were never published),
filled REPORT_M1.md and REPORT_M2.md drafts (tables from the scripts; explanations to be rewritten by the student).
Nothing in the optimizer or executor changed, only the catalog rewrite. Unit tests: 170 pass. Fuzz: 360 queries, 0 bad.

**What broke / still open:** nothing new beyond the list above. Not tested on Windows (I have no Windows machine here).

## 2026-11-10: M4 stand-in measurements (same Claude session)
- The course dataset was never published, so REPORT_M4 uses our own demo data and says so at the top.
- The city/zip correlation gave q-error 3.0 (339 estimated, 1008 actual).
- One query at B=4 picked nested loop (145 I/O) when sort-merge cost 88. I did not prove correlation caused it, and the report says that.
- Query 9 has est 80 against actual 342. Cost model gap, documented, not fixed.
