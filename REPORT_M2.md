# REPORT_M2.md: measurements

> Tables come from `python scripts/m2_measure.py all` (Python 3.14.6, Linux sandbox). Page counts and comparison
> counts are deterministic, so they should be identical on your machine: run it again and compare every row.
> Relations are 20 to 80 pages with 150 character padding so one page holds about 24 tuples.

Machine / Python: `Intel Core i7-9750HF, 16 GB RAM, Windows 10, Python 3.14.7` (confirm before submitting)

## 1. Join cost against the textbook formulas

`python scripts/m2_measure.py cost`

| join | M=N (pages) | B | measured reads | measured writes | formula reads | formula writes | measured/formula I/O |
|---|---|---|---|---|---|---|---|
| nested_loop_join | 20 | 4 | 231 | 0 | 220 | 0 | 1.05 |
| nested_loop_join | 20 | 16 | 63 | 0 | 60 | 0 | 1.05 |
| nested_loop_join | 20 | 64 | 42 | 0 | 40 | 0 | 1.05 |
| nested_loop_join | 20 | 256 | 42 | 0 | 40 | 0 | 1.05 |
| sort_merge_join | 20 | 4 | 158 | 116 | 160 | 120 | 0.98 |
| sort_merge_join | 20 | 16 | 122 | 80 | 120 | 80 | 1.01 |
| sort_merge_join | 20 | 64 | 82 | 40 | 80 | 40 | 1.02 |
| sort_merge_join | 20 | 256 | 82 | 40 | 80 | 40 | 1.02 |
| hash_join | 20 | 4 | 133 | 91 | 80 | 40 | 1.87 |
| hash_join | 20 | 16 | 83 | 41 | 80 | 40 | 1.03 |
| hash_join | 20 | 64 | 83 | 41 | 80 | 40 | 1.03 |
| hash_join | 20 | 256 | 83 | 41 | 80 | 40 | 1.03 |
| nested_loop_join | 40 | 4 | 861 | 0 | 840 | 0 | 1.02 |
| nested_loop_join | 40 | 16 | 164 | 0 | 160 | 0 | 1.02 |
| nested_loop_join | 40 | 64 | 82 | 0 | 80 | 0 | 1.02 |
| nested_loop_join | 40 | 256 | 82 | 0 | 80 | 0 | 1.02 |
| sort_merge_join | 40 | 4 | 402 | 320 | 400 | 320 | 1.00 |
| sort_merge_join | 40 | 16 | 242 | 160 | 240 | 160 | 1.00 |
| sort_merge_join | 40 | 64 | 162 | 80 | 160 | 80 | 1.01 |
| sort_merge_join | 40 | 256 | 162 | 80 | 160 | 80 | 1.01 |
| hash_join | 40 | 4 | 335 | 172 | 160 | 80 | 2.11 |
| hash_join | 40 | 16 | 164 | 82 | 160 | 80 | 1.02 |
| hash_join | 40 | 64 | 164 | 82 | 160 | 80 | 1.02 |
| hash_join | 40 | 256 | 164 | 82 | 160 | 80 | 1.02 |
| nested_loop_join | 80 | 4 | 3321 | 0 | 3280 | 0 | 1.01 |
| nested_loop_join | 80 | 16 | 567 | 0 | 560 | 0 | 1.01 |
| nested_loop_join | 80 | 64 | 243 | 0 | 240 | 0 | 1.01 |
| nested_loop_join | 80 | 256 | 162 | 0 | 160 | 0 | 1.01 |
| sort_merge_join | 80 | 4 | 802 | 640 | 800 | 640 | 1.00 |
| sort_merge_join | 80 | 16 | 482 | 320 | 480 | 320 | 1.00 |
| sort_merge_join | 80 | 64 | 482 | 320 | 480 | 320 | 1.00 |
| sort_merge_join | 80 | 256 | 322 | 160 | 320 | 160 | 1.00 |
| hash_join | 80 | 4 | 825 | 332 | 320 | 160 | 2.41 |
| hash_join | 80 | 16 | 327 | 165 | 320 | 160 | 1.02 |
| hash_join | 80 | 64 | 324 | 162 | 320 | 160 | 1.01 |
| hash_join | 80 | 256 | 324 | 162 | 320 | 160 | 1.01 |

- **Block nested loop** is 1% to 5% above `M + ceil(M/(B-2)) N`. The gap is exactly the directory pages: a scan reads
  one directory page besides its data pages, and the inner side is scanned once per outer block. Check: M = N = 20,
  B = 4 has 10 blocks, so 1 outer scan + 10 inner scans = 11 extra reads, and 231 - 220 = 11. At B = 16 there are
  2 blocks, 3 extra reads (63 - 60). At B = 64 there is 1 block, 2 extra reads (42 - 40).
- **Sort-merge** is within about 2% of the formula in every row. It is both slightly above and slightly below
  because my initial runs hold B-1 pages (one frame is the input scan) and a run that fits in memory is never
  written. (Verify with `exec trace` on one row if asked.)
- **Hash join at B = 4** is 1.9 to 2.4 times the formula `3(M + N)`, but at B = 16 and above it is within 1% to 3%.
  At B = 4 a build partition has to fit in B-1 = 3 pages. Build partitions are many pages each (for the 30 page relations in the skew table they are about 10 pages), far over 3, so they overflow:
  the join repartitions them and falls back to block nested loop for pairs that still do not fit (the skew table
  below shows this: "repartition" and "block nested loop fallback" even for uniform data). The formula assumes
  no overflow. With more frames the extra work disappears.

## 2. Crossover

`python scripts/m2_measure.py crossover`

Fixed relations: R = 40 pages, S = 40 pages. Total page I/O (reads + writes):

| B | nested_loop_join | sort_merge_join | hash_join | cheapest |
|---|---|---|---|---|
| 4 | 861 | 722 | 507 | hash_join |
| 5 | 615 | 562 | 521 | hash_join |
| 6 | 451 | 562 | 438 | hash_join |
| 8 | 328 | 402 | 256 | hash_join |
| 12 | 205 | 402 | 250 | nested_loop_join |
| 16 | 164 | 402 | 246 | nested_loop_join |
| 24 | 123 | 402 | 246 | nested_loop_join |
| 32 | 123 | 402 | 246 | nested_loop_join |
| 48 | 82 | 242 | 246 | nested_loop_join |
| 64 | 82 | 242 | 246 | nested_loop_join |
| 128 | 82 | 242 | 246 | nested_loop_join |

Fixed B = 16, relation size varies (M = N):

| pages | nested_loop_join | sort_merge_join | hash_join | cheapest |
|---|---|---|---|---|
| 5 | 12 | 32 | 36 | nested_loop_join |
| 10 | 22 | 62 | 66 | nested_loop_join |
| 15 | 48 | 92 | 94 | nested_loop_join |
| 20 | 63 | 202 | 124 | nested_loop_join |
| 30 | 124 | 302 | 190 | nested_loop_join |
| 40 | 164 | 402 | 246 | nested_loop_join |
| 60 | 366 | 602 | 372 | nested_loop_join |
| 80 | 567 | 802 | 492 | hash_join |

- **Varying B (40 + 40 pages).** Hash join is cheapest for B up to 8 and block nested loop from B = 12. Nested loop's cost
  falls with B (fewer outer blocks) while hash join is about `3(M + N) = 240` once its partitions fit. Setting
  `M + ceil(M/(B-2)) N < 3(M + N)` with M = N = 40 gives `ceil(40/(B-2)) < 5`, so at most 4 blocks, so `B - 2 >= 10`,
  so B >= 12. That matches the table. Sort-merge is never the cheapest here: it always pays to sort both inputs
  (writes included) and its cost stays flat until B is large enough to sort each input in memory.
- **Varying size at B = 16.** Block nested loop wins up to 60 pages and hash join wins at 80 pages. The ranking changes
  between 60 and 80 pages, where nested loop needs more and more outer blocks (its cost grows with the square of the size) while hash join
  grows linearly (3(M + N)).

## 3. Sort passes

`python scripts/m2_measure.py passes`

| N (pages) | B | initial runs (mine) | passes measured | passes spec formula 1+ceil(log_{B-1} ceil(N/B)) | note |
|---|---|---|---|---|---|
| 40 | 4 | 14 | 4 | 4 |  |
| 80 | 4 | 27 | 4 | 4 | N = 20 x B |
| 160 | 4 | 54 | 5 | 5 |  |
| 80 | 8 | 12 | 3 | 3 |  |
| 160 | 8 | 23 | 3 | 3 | N = 20 x B |
| 200 | 6 | 40 | 4 | 4 |  |
| 60 | 16 | 4 | 2 | 2 |  |

The measured pass count equals `1 + ceil(log_(B-1) ceil(N/B))` in all seven rows, including the rows where N = 20 x B
(80 pages at B = 4 and 160 pages at B = 8). My initial runs hold B-1 pages, so I make more initial runs than the spec's `ceil(N/B)`
(for example 27 against 20 for N = 80, B = 4). The pass count is the same because the extra runs do not
cross a power of B-1 (both 27 and 20 need 3 merge levels at fan-in 3). That can differ for other (N, B).
See DESIGN_M2.md section 3 for why the run size is B-1.

## 4. Skew

`python scripts/m2_measure.py skew`

| B | data | hash_join page reads | page writes | total I/O | comparisons | overflow handling | build partition pages (left) |
|---|---|---|---|---|---|---|---|
| 4 | uniform | 227 | 133 | 360 | 53372 | block nested loop fallback, repartition | [10, 11, 10] |
| 4 | 40% one key on both sides | 275 | 129 | 404 | 105891 | block nested loop fallback, repartition | [7, 17, 7] |
| 16 | uniform | 125 | 63 | 188 | 731 | none | [10, 11, 10] |
| 16 | 40% one key on both sides | 161 | 99 | 260 | 74363 | repartition | [7, 17, 7] |

- At B = 4 even uniform data overflows (3 frames per partition is tiny), so the comparison that shows skew is B = 16.
- **Uniform at B = 16:** the build partitions are 10, 11 and 10 pages, all within B-1 = 15, so there is no overflow:
  188 page I/Os and 731 comparisons.
- **40% of the rows on one key, on both sides, at B = 16:** the build partitions are 7, 17 and 7 pages. The hot key is
  alone in one partition and hashing can never split one key, so that partition is 17 pages, over the 15-page limit. The join repartitions it
  once with a different hash seed (the table shows only "repartition" for this row, no block nested loop fallback). Cost: 260 page I/Os against 188 (+38%), and about 100 times more comparisons (74,363 against 731)
  because the hot key pairs every hot row on the left with every hot row on the right.
- At B = 4 the hot key partition goes all the way to the block nested loop fallback.

## 5. Budget

`python scripts/m2_measure.py budget`

input: 85 pages = 21.2 x B at B = 4
completed: 2020 rows, sorted = True
largest number of frames held at once (pinned + leased, tracked by the buffer pool on every pin and lease): 4 of 4
page I/O: 418 reads, 332 writes; passes = 5

"Largest number of frames held at once" is `pool.peak_held`, which the buffer pool updates on every pin and every
lease (frames an operator reserves for its own in-memory working data). Because pinned + leased can never
exceed B by construction, 4 of 4 means the sort used all its frames and never more.

## 6. Sources

- **Graefe, [Volcano: An Extensible and Parallel Query Evaluation System, IEEE TKDE 1994](https://cs-people.bu.edu/mathan/reading-groups/papers-classics/volcano.pdf).** It defines the open / next / close iterator interface. Every operator in `dbms/execution` has exactly those three methods and pulls rows from its child, so a plan is just a tree of objects and pipelining comes for free. Operators that need memory (sort, hash join, group by) do their work in `open`.
- **Ramakrishnan and Gehrke, ch. 12 to 14.** I took the cost formulas from here and use them as the "formula" column in my tables: external sort passes = 1 + ceil(log_(B-1) ceil(N/B)), block nested loop = M + ceil(M/(B-2)) * N, hash join = 3(M+N) when partitions fit. My measured sort passes match the formula in all 7 rows. The measured reads are higher by the number of scans opened, because each scan also reads one directory page.
- **Shapiro, Join Processing in Database Systems with Large Main Memories, ACM TODS 1986.** It describes the Grace style hash join and what to do when a partition does not fit. It changed my overflow handling: repartition the overflowing partition once, and fall back to block nested loop if it still does not fit. This is why the skewed data costs 260 I/O against 188 on uniform data at B=16 (section 5).
- **Garcia-Molina, Ullman and Widom, Database Systems: The Complete Book, ch. 15.** Used to check my sort-merge join against a second description (the merge step with duplicate keys on both sides).
