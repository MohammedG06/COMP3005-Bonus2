# REPORT_M4.md: measurements

DRAFT written with an AI assistant. All numbers below are from OUR OWN demo data
(`scripts/make_demo_db.py`: Emp 3000 rows, Dept 40, Proj 600, 12 queries), NOT the course
dataset, because the course files were never published. Re-run the same four commands on
the course dataset and its 20 queries, paste the new tables, and rewrite every explanation
in your own words. Frames = 32. Numbers are deterministic.

## 1. Estimated against actual rows at every node
| query | nodes | median q-error | worst q-error | worst node | est rows | actual rows |
|---|---|---|---|---|---|---|
| 1 | 7 | 1.01 | 1.01 | project | 338 | 343 |
| 2 | 8 | 1.02 | 1.08 | project | 13 | 12 |
| 3 | 5 | 1.00 | 1.23 | sort | 27 | 22 |
| 4 | 10 | 1.00 | 1.03 | project | 24375 | 25125 |
| 5 | 3 | 1.00 | 2.96 | filter | 340 | 1008 |
| 6 | 3 | 1.00 | 3.68 | filter | 221 | 813 |
| 7 | 5 | 1.00 | 1.00 | sort | 5 | 5 |
| 8 | 3 | 1.00 | 1.02 | filter | 468 | 477 |
| 9 | 10 | 1.00 | 1.01 | project | 45000 | 45489 |
| 10 | 7 | 1.00 | 1.43 | group_by(sort) | 730 | 511 |
| 11 | 5 | 1.00 | 1.29 | rename | 27 | 21 |
| 12 | 7 | 1.00 | 1.63 | project | 731 | 448 |

Over all 73 plan nodes of 12 queries: median q-error = 1.00, worst = 3.68

- Median q-error is 1.00 over 73 nodes. Most nodes are single-column predicates and joins on a key, which the histograms handle exactly.
- The worst are queries 5 and 6 (3.0 and 3.7). Both filter on two columns at once (see section 3).

## 2. Optimizer against no optimizer
| query | rows | optimizer I/O | no optimizer, nested loop I/O | ratio (nested loop / optimizer) | est cost | est vs actual I/O |
|---|---|---|---|---|---|---|
| 1 | 343 | 69 | 73 | 1.06 | 67 | 0.97 |
| 2 | 203 | 69 | 73 | 1.06 | 67 | 0.97 |
| 3 | 22 | 69 | 205 | 2.97 | 66 | 0.96 |
| 4 | 25125 | 84 | 137880 | 1641.43 | 79 | 0.94 |
| 5 | 1 | 67 | 67 | 1.00 | 66 | 0.99 |
| 6 | 1 | 67 | 67 | 1.00 | 66 | 0.99 |
| 7 | 5 | 69 | 69 | 1.00 | 66 | 0.96 |
| 8 | 5 | 67 | 67 | 1.00 | 66 | 0.99 |
| 9 | 40 | 342 | 24706 | 72.24 | 80 | 0.23 |
| 10 | 511 | 78 | 78 | 1.00 | 76 | 0.97 |
| 11 | 21 | 67 | 203 | 3.03 | 66 | 0.99 |
| 12 | 448 | 134 | 268 | 2.00 | 132 | 0.99 |

- Query 4 is the big one: a 3-table join written as cross products, 137,880 I/O unoptimized against 84 with the optimizer.
- Query 9 is where the cost model is wrong: estimated 80, actual 342 (ratio 0.23). Known gap: the model ignores writing and re-reading join results and directory pages. Not fixed, on purpose, because the spec's example numbers depend on the literal formulas.
- Queries 1, 2, 5, 6, 7, 8, 10 are about 1.0: one table or one join, nothing to reorder.

## 2b. Which rule mattered (required)
| rule | what it does | total I/O, all rules on | total I/O, this rule off | change |
|---|---|---|---|---|
| 1 | split conjunctions | 1182 | 2230 | +1048 (+88.7%) |
| 2 | push select below join/cross | 1182 | 1186 | +4 (+0.3%) |
| 3 | cross + equality -> join | 1182 | 1200 | +18 (+1.5%) |
| 4 | push select through rename/project/union | 1182 | 1182 | +0 (+0.0%) |
| 5 | push select below group_by | 1182 | 1182 | +0 (+0.0%) |
| 6 | push projections down | 1182 | 5764 | +4582 (+387.6%) |
| 7 | drop no-op projections, merge selects | 1182 | 1182 | +0 (+0.0%) |

A rule that changes nothing here is worth nothing on this data; say why (for example, no query has a predicate it could move).

- Rule 6 (push projections down) matters most: +388% I/O when off, because rows stay wide.
- Rule 1 (split conjunctions) is +89%, because without it the later rules cannot push anything.
- Rules 4, 5 and 7 change nothing here. No query has a predicate over a rename/union/group_by that they could move. That is a property of this workload, not proof the rules are useless.

## 3. Correlated columns
| table | columns | values | independence estimate | actual rows | q-error |
|---|---|---|---|---|---|
| Emp | city, zip | 'Ottawa', 'K1A' | 339 | 1008 | 3.0 |
| Emp | age, zip | 28, 'K1A' | 23 | 30 | 1.3 |
| Emp | age, city | 28, 'Ottawa' | 23 | 30 | 1.3 |
| Emp | did, zip | 0, 'K1A' | 81 | 84 | 1.0 |
| Emp | did, city | 0, 'Ottawa' | 81 | 84 | 1.0 |

The top rows are candidates: the optimizer multiplies the two selectivities (independence), the data does not.

- city and zip are correlated (zip K1A only occurs in Ottawa). Independence multiplies the two selectivities and gives 339 rows. The real answer is 1008, a q-error of 3.0.
- Effect on a plan: for `SELECT p.pname FROM Emp e, Proj p WHERE e.did = p.did AND e.city='Ottawa' AND e.zip='K1A'` at B=4 the optimizer picked nested loop (estimated 76) and it cost 145 I/O. Forcing sort-merge on the same plan cost 88. At B=8 and above the pick (78) was the best of the three.
- I have NOT shown that the correlation alone caused that B=4 choice. Both est costs (76 vs 79) are close, so a small row error can flip them. Say it that way unless you test it.
- A fix would be multi-column statistics. Not built.

## 4. Required printouts

### 4.1 DP table for a four-relation query (demo data, B = 32)
Query: `SELECT e.name, p.pname FROM Emp e, Emp f, Dept d, Proj p WHERE e.did = d.did AND f.did = d.did AND p.did = d.did AND d.budget > 1500 AND e.age > 60 AND f.age < 25;`
Command: `python opt.py dp-table --db <demo> --frames 32 --logical q4.json`

```
relations: e, f, d
subset                outer               inner     algorithm              rows   pages      cost  chosen
{e,f}                 {f}                 e         nested_loop_join     102076     395       132  <== best
{e,f}                 {e}                 f         nested_loop_join     102076     395       132  

{e,d}                 {d}                 e         nested_loop_join        110       1        67  <== best
{e,d}                 {e}                 d         nested_loop_join        110       1        67  
{e,d}                 {d}                 e         sort_merge_join         110       1        69  
{e,d}                 {e}                 d         sort_merge_join         110       1        69  
{e,d}                 {d}                 e         hash_join               110       1        71  
{e,d}                 {e}                 d         hash_join               110       1        71  

{f,d}                 {d}                 f         nested_loop_join         99       1        67  <== best
{f,d}                 {f}                 d         nested_loop_join         99       1        67  
{f,d}                 {d}                 f         sort_merge_join          99       1        69  
{f,d}                 {f}                 d         sort_merge_join          99       1        69  
{f,d}                 {d}                 f         hash_join                99       1        71  
{f,d}                 {f}                 d         hash_join                99       1        71  

{e,f,d}               {f,d}               e         nested_loop_join        830       5       134  <== best
{e,f,d}               {e,d}               f         nested_loop_join        830       5       134  
{e,f,d}               {f,d}               e         sort_merge_join         830       5       136  
{e,f,d}               {e,d}               f         sort_merge_join         830       5       136  
{e,f,d}               {f,d}               e         hash_join               830       5       138  
{e,f,d}               {e,d}               f         hash_join               830       5       138  
{e,f,d}               {e,f}               d         nested_loop_join        830       5       541  
{e,f,d}               {e,f}               d         hash_join               830       5      1320  
{e,f,d}               {e,f}               d         sort_merge_join         830       5      1714  

relations: e, p
subset                outer               inner     algorithm              rows   pages      cost  chosen
{e,p}                 {p}                 e         nested_loop_join      12450      74       144  <== best
{e,p}                 {e}                 p         nested_loop_join      12450      74       144  
{e,p}                 {p}                 e         sort_merge_join       12450      74       149  
{e,p}                 {e}                 p         sort_merge_join       12450      74       149  
{e,p}                 {p}                 e         hash_join             12450      74       154  
{e,p}                 {e}                 p         hash_join             12450      74       154
```
- The optimizer joins {e,f,d} first (cost 134 for the best of that subset) and then adds p. Putting d on the inner side of a 3-way join is far worse (541 to 1714).
- Left-deep only: each row has one relation or one subset result as the inner input, and a base relation as the other side.

### 4.2 The chosen join algorithm changes with B (demo data)
Query: `SELECT e.name, f.name FROM Emp e JOIN Emp f ON e.zip = f.zip;` (self join, 3000 rows each)

| B | 3 | 4 | 6 | 8 | 16 | 32 | 64 |
|---|---|---|---|---|---|---|---|
| chosen join | hash | hash | hash | hash | nested loop | nested loop | nested loop |

- Hash join wins while the pool is small (B up to 8). From B = 16 the block nested loop needs few enough passes over the inner table to be cheaper. This matches the crossover I measured in REPORT_M2.

### 4.3 Plan trees before and after rewriting (same four-relation query)
Command: `python opt.py explain --db <demo> --frames 32 --logical q4.json --trees`

```
logical plan before rewriting:
Project(cols=[e.name, p.pname])
└── Select(cond=And(And(And(And(And(Eq(Attr(e.did), Attr(d.did)), Eq(Attr(f.did), Attr(d.did))), Eq(Attr(p.did), Attr(d.did))), Gt(Attr(d.budget), Num(1500))), Gt(Attr(e.age), Num(60))), Lt(Attr(f.age), Num(25))))
    └── Cross
        ├── Cross
        │   ├── Cross
        │   │   ├── Rename(as=e)
        │   │   │   └── Scan(Emp)
        │   │   └── Rename(as=f)
        │   │       └── Scan(Emp)
        │   └── Rename(as=d)
        │       └── Scan(Dept)
        └── Rename(as=p)
            └── Scan(Proj)

after rewriting (rules fired: [1, 2, 3, 6]):
Project(cols=[e.name, p.pname])
└── Join(cond=Eq(Attr(p.did), Attr(d.did)))
    ├── Project(cols=[e.name, d.did])
    │   └── Select(cond=Eq(Attr(e.did), Attr(d.did)))
    │       └── Join(cond=Eq(Attr(f.did), Attr(d.did)))
    │           ├── Cross
    │           │   ├── Project(cols=[e.name, e.did])
    │           │   │   └── Select(cond=Gt(Attr(e.age), Num(60)))
    │           │   │       └── Rename(as=e)
    │           │   │           └── Scan(Emp)
    │           │   └── Project(cols=[f.did])
    │           │       └── Select(cond=Lt(Attr(f.age), Num(25)))
    │           │           └── Rename(as=f)
    │           │               └── Scan(Emp)
    │           └── Project(cols=[d.did])
    │               └── Select(cond=Gt(Attr(d.budget), Num(1500)))
    │                   └── Rename(as=d)
    │                       └── Scan(Dept)
    └── Project(cols=[p.pname, p.did])
        └── Rename(as=p)
            └── Scan(Proj)

physical plan:
project (e.name, p.pname)                                      est_rows=12450    est_cost=0
  nested_loop_join (p.did = d.did)                             est_rows=12450    est_cost=0
    project (p.pname, p.did)                                   est_rows=600      est_cost=0
      seqscan (Proj AS p)                                      est_rows=600      est_cost=10
    project (e.name, d.did)                                    est_rows=830      est_cost=0
      nested_loop_join (e.did = d.did)                         est_rows=837      est_cost=1
        nested_loop_join (f.did = d.did)                       est_rows=99       est_cost=0
          project (d.did)                                      est_rows=13       est_cost=0
            filter (d.budget > 1500)                           est_rows=13       est_cost=0
              seqscan (Dept AS d)                              est_rows=40       est_cost=1
          project (f.did)                                      est_rows=302      est_cost=0
            filter (f.age < 25)                                est_rows=302      est_cost=0
              seqscan (Emp AS f)                               est_rows=3000     est_cost=66
        project (e.name, e.did)                                est_rows=338      est_cost=0
          filter (e.age > 60)                                  est_rows=338      est_cost=0
            seqscan (Emp AS e)                                 est_rows=3000     est_cost=66
total est_cost = 144 page I/Os, est_rows = 12450
```
- Rules fired: 1 (split conjunctions), 2 (push selects below joins), 3 (cross plus equality becomes join), 6 (push projections down).
- The selects on e.age, f.age and d.budget moved to just above each scan, and each scan is wrapped in a projection that keeps only the needed columns.

### 4.4 Checklist
- [x] DP table printed in full for a four-relation query
- [x] B changes the chosen algorithm
- [x] Before and after trees
- [ ] Re-run sections 1 to 4 on the course dataset and its 20 queries if it is published

## 5. Sources

- **Selinger et al., [Access Path Selection in a Relational Database Management System, SIGMOD 1979](https://courses.cs.washington.edu/courses/cse444/25wi/papers/selinger79.pdf).** It gave me the dynamic programming over subsets of relations and the left-deep restriction. I keep, for each subset, the cheapest plan per algorithm and extend it with one more relation. This is the DP table in DESIGN_M4.md and in section 4.
- **Leis et al., [How Good Are Query Optimizers, Really?, VLDB 2015](https://ir.cwi.nl/pub/35880/35880.pdf).** It shows that cardinality estimation errors, not the cost model, are the main cause of bad plans, and that independence between columns is the usual culprit. It is why section 1 reports q-error at every node (not just the root) and why section 3 looks for correlated column pairs.
- **Ramakrishnan and Gehrke, ch. 15.** The selectivity formulas (equality = 1/distinct, range from the histogram, AND = product under independence, OR = inclusion-exclusion) and the rewrite rules come from here.
- **Garcia-Molina, Ullman and Widom, ch. 16.** I used its algebraic laws to write the preconditions of the rewrite rules (for example pushing a selection below a join only when it uses attributes of one side).
