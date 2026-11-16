# ARCHITECTURE.md

```
  SQL text
     |
     v
  M3  dbms/sql          lexer -> parser -> scope + name resolution + types -> logical plan
     |                  (reads table schemas from the catalog)
     |   logical plan   JSON: scan select project join cross rename group_by distinct sort limit
     |                        union union_all intersect except
     v
  M4  dbms/optimizer    rewrites -> statistics -> selectivity -> cost model -> join order -> physical plan
     |                  (reads statistics from the catalog)
     |   physical plan  JSON: seqscan filter project rename distinct sort group_by nested_loop_join
     |                        sort_merge_join hash_join union union_all intersect except limit,
     |                        every node also carries est_rows and est_cost
     v
  M2  dbms/execution    operator tree: open() / next() / close(), working memory = B frames
     |
     |   pages           fetch_page / unpin_page / new_page, heap file scan, temp files through DiskManager
     v
  M1  dbms/storage      buffer pool (LRU or CLOCK) -> heap files, slotted pages -> files on disk
                        catalog (tables, schemas, statistics) lives in catalog.db, in pages
```

`dbms/system.py` (`Engine`) and `shell.py` do the wiring. `dbms/ra/` is the Bonus 1 engine as a library: the
shared plan node type, the condition parser, JSON serialization with validation, and a reference evaluator.

## The interface between each pair

| from -> to | what crosses | where it is defined |
|---|---|---|
| M3 -> M4 | logical plan: `Node` tree / JSON, columns fully qualified | `dbms/ra/nodes.py`, `plan_json.py` |
| M4 -> M2 | physical plan JSON (extra `est_*` fields are ignored by M2) | `plan_json.from_dict(..., "physical")` |
| M2 -> M1 | `BufferPool.fetch_page / unpin_page / lease / release`, `HeapFile.scan_bytes`, `DiskManager` | `dbms/storage/` |
| M3, M4 -> M1 | catalog: `get_table`, `list_tables`, `get_stats`, `put_stats`, `update_counts` | `dbms/storage/catalog.py` |
| the shell -> everything | `Engine.run(text)`: a generator of `Result` | `dbms/system.py` |

Conditions travel as strings in the JSON (`"e.did = d.did and e.age > 60"`) and are parsed by the one condition
parser (`dbms/ra/condition.py`) wherever they are needed. Errors are one type (`DBError`) with a category and a
position, printed in one format.

## Memory

B frames in the buffer pool are the memory of a plan. Scans pin frames; every other operator `lease`s its working
memory from the same pool, so pinned + leased can never exceed B. See DESIGN_M2.md.

## Testing

172 tests: the 25 Bonus 1 cases on the library, Module 1 spec examples, Module 2 differential tests against the
reference evaluator, the 7 spec translations and 15 cases of Module 3, Module 4 examples (including the DP table),
and whole-pipeline tests at B = 3, 4, 8 and 32. `scripts/fuzz_pipeline.py` runs random queries through the whole
pipeline with random subsets of rewrite rules off and compares every result with the reference evaluator. A private
script (not in the repository) also compared my SQL front end with SQLite on 1,500 random queries.

## What I would do differently if I started again

1. **One physical column order, decided once.** The optimizer reorders joins, so the plan's column order changed,
   and I patched it three times (rule 7 removing a projection, a swapped hash build side, a different join order)
   before making "every node emits its logical column order" a single invariant. It should have been the design from day one.
2. **Cost the materialization.** The executor writes a join result to disk when a blocking operator or another join
   consumes it, because that keeps every plan inside B frames. The cost model never learned about this, so plans
   with a join below a group_by or sort are underestimated. The planner and the executor should have shared one
   description of when a result is materialized.
3. **Give scans a real cache policy instead of private page namespaces.** Each scan reads from disk through its own
   file id so counters match the textbook formulas. It works, but it also caused a hang (a scan read a table's pages
   before they were flushed) and it means the buffer pool policies (LRU / CLOCK) never influence a query.
4. **Statistics that update.** `INSERT` and `LOAD` change the catalog counters but not the histograms, so statistics go
   stale until the next `ANALYZE`.
5. **Test with a random query generator from week one.** Every wrong answer in Module 4 was found by random
   differential testing, none by the hand written cases. I would have built that oracle before the optimizer.


## Sources

- Hellerstein, Stonebraker and Hamilton, [Architecture of a Database System](https://nowpublishers.com/article/Details/DBS-002): the layering above (parser, optimizer, executor, storage) follows its process model and component diagram.
- Graefe, Volcano (1994): the iterator interface between M2 operators.
