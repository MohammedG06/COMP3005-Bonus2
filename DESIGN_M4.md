# DESIGN_M4.md: query optimizer

Assumption: the course dataset and its 20 queries were not provided, so REPORT_M4 uses my own generated data (`scripts/make_demo_db.py`: Emp 3000 rows, Dept 40, Proj 600, with a correlated city and zip pair) and my own 20 queries from the same script.

Code: `dbms/optimizer/` (`stats.py`, `estimate.py`, `rewrite.py`, `planner.py`, `api.py`, `cli.py`).

## 1. Statistics (`ANALYZE`)

Exact, no sampling. Per table: tuples, data pages, average record width. Per column: distinct values,
min, max, average width and an equi-depth histogram of k buckets (`lo, hi, count, ndv`). A value is never split
over two buckets (equal values are added as whole runs). ANALYZE sorts each column with the Module 2 external
sort, so a column never has to fit in memory. Everything is stored in the catalog as records of kinds 2, 3 and 4
(kind byte reserved in Module 1). ANALYZE twice gives byte-identical statistics (tested). Without ANALYZE the
documented defaults apply: tuples and pages from the catalog counters, 10% distinct values per column, no
histogram, VARCHAR(n) averages n/2 + 2 bytes.

## 2. Selectivity and cardinality

Assumptions: uniformity inside a bucket, independence between predicates, containment for joins.

| predicate | selectivity |
|---|---|
| `col = v` | bucket containing v: `count / (bucket ndv * n)`; outside min..max or between buckets: `1/n`; no histogram: `1/ndv` |
| `col <, <=, >, >=  v` | whole buckets below v plus a fraction of the one holding v (integers: linear); no histogram: `(v - min)/(max - min)`; unknown: 1/3 |
| `col <> v` | `1 - sel(=)` |
| `p AND q`, `p OR q`, `NOT p` | `sp sq`, `sp + sq - sp sq`, `1 - sp` |
| `a = b` (two columns) | `1 / max(ndv_a, ndv_b)` |
| any other two-column predicate | 0.1 |

Output rows = `ceil(input rows * selectivity)`, never below 1. Join rows = `rows_left * rows_right * product of the
selectivities of the atoms`. group_by rows = product of the key ndvs capped by the input rows (1 with no keys).
Where they break: correlated columns (the product is far too small), skew inside a bucket, and any column
produced by an aggregate (no statistics: defaults 1/10 and 1/3).

## 3. Rewrite rules

Each can be switched off (`opt ... --disable 2,3`) for the "turn one rule off" measurement.

| # | rule | precondition |
|---|---|---|
| 1 | split `p AND q` into stacked selections | always valid |
| 2 | push a selection below join / cross, and below another selection | the columns it uses all come from one side |
| 3 | `select[p](cross(L, R))` -> `join[p](L, R)` | p is an equality between a column of L and a column of R |
| 4 | push a selection through rename, project and union / union_all | through rename only when its input is not a plain scan; through project only if the projection keeps the used columns unchanged; through union with positional column mapping |
| 5 | push a selection below group_by | it uses only grouping columns |
| 6 | push projections down | inserted above a join input or group_by input that carries columns no ancestor needs |
| 7 | drop a projection that drops and reorders nothing, merge adjacent selections | done while building the physical plan |

JOIN ... ON conditions are split too: a conjunct that uses one side only is pushed into that side. That is what
makes `FROM a, b WHERE ...` and `FROM a JOIN b ON ...` produce **the same physical plan** (tested byte for byte).

## 4. Cost model (page I/O, B frames, M and N = pages of the outer and the inner)

| algorithm | cost |
|---|---|
| block nested loop | `M + ceil(M / (B-2)) * N` |
| sort-merge | `sort(M) + sort(N) + M + N`, `sort(X) = 2X(passes - 1) + X`, `passes = 1 + ceil(log_{B-1} ceil(X/B))` |
| hash | `3 (M + N)` |

Cost of a join = cost of the outer subplan + the formula; for a single base relation the outer read is the
formula's own M term, for an intermediate result the formula's M is added to the subplan cost (this is the
spec's accounting, and it makes the worked example come out as 110 and 530). Where a base relation has a
pushed-down filter, its scan cost (pages actually read) replaces N in the nested loop term, and the extra
pages are added for sort-merge and hash.
Filters, projections and limits add nothing of their own. group_by / distinct choose sort or hash mode by cost
(hash when the groups fit in B - 2 pages). Not modelled: the executor writes and re-reads a join result that
feeds a blocking operator (see DESIGN_M2.md section 2), and every scan also reads its directory page. Both make
actual I/O a little higher than estimated (REPORT_M4.md quantifies it).

## 5. Join order: left-deep dynamic programming

* A *block* is a maximal tree of join / cross nodes (plus selections sitting right above them). Its leaves are
  the inputs, its predicates are the join conditions and the leftover selections.
* A predicate over one relation is attached to that relation first, so the search never has to apply it.
* `best[S]` for every subset S of the leaves (at most 10 relations; beyond that a greedy left-deep order is used).
  Each step adds one base relation `r` to `best[S - r]`, tries hash, sort-merge (needs an equality between the
  two sides) and nested loop, and keeps the cheapest. Ties: lower cost, then hash < sort-merge < nested loop,
  then lower relation index, so the result is deterministic.
* A subset is only extended by a relation that has a predicate to it. A cross product is used only when
  the join graph is disconnected and there is no alternative.
* Rows of a subset do not depend on the order it was built in: product of the leaf rows times the
  selectivity of every predicate inside the subset.
* `opt dp-table` prints every option for every subset, with the winner marked.
* Why left-deep: the inner is always a base relation, so the inner can be rescanned (nested loop) or partitioned
  directly, intermediate results are pipelined, and the search space is n! instead of the much larger bushy space.
  What it loses: a bushy plan can join two small selective results first, then combine them, which a chain cannot express.
* Output invariants: the plan's columns always come out in the logical order (a join order that
  differs, or a hash join whose smaller input became the build side, is followed by a free projection that restores the
  order). Every predicate is applied exactly once; the planner checks this and fails loudly otherwise.

* Rule switches are honoured by the search too: with rule 1 off a conjunction stays one predicate, with rule 2 off
  single table predicates run in a filter above the finished join, and with rule 3 off an equality that came from a
  WHERE over a cross product is not usable as a join key. Without this, the "turn one rule off" measurement would
  measure nothing (the search would redo the rule's work). `scripts/fuzz_pipeline.py` checks that every subset of
  rules still gives correct answers.

## 6. Output

Physical plan JSON for Module 2, every node annotated with `est_rows` and `est_cost` (own cost: the cumulative
cost minus the children's). `rename[x](scan T)` becomes `seqscan` with an alias. `opt explain` prints the tree;
`--trees` also prints the logical plan before and after rewriting and the rules that fired.

## 7. Known weaknesses (worth stating at the oral)

* Independence: correlated columns are underestimated (REPORT_M4.md section 3).
* The spec's worked example lists 550 for the plan `{B,C} then A`; with the formula and B = 32 the 40 page
  intermediate needs 2 blocks, so 650. The winner (530) and all other costs agree.
* The cost model does not know about materialization or directory pages.
