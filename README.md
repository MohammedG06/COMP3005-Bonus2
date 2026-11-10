# COMP 3005 Project 2: a database system, one module at a time

Pure Python 3 (standard library only). Built on my Bonus Project 1 relational algebra engine.

## Status

| Checkpoint | Module | State |
|---|---|---|
| 1 | Engine refactor + M1 storage engine | code and tests done; measurements and oral prep to do |
| 2 | M2 execution engine | code and tests done (34 tests); measurements and oral prep to do |
| 3 | M3 SQL front end | code and tests done (39 tests); oral prep to do |
| 4 | M4 optimizer | code and tests done; measurements on the course dataset to do |
| 5 | Integration | shell, Engine, workload runner done; waiting for the course dataset |

## Layout

```
dbms/errors.py, schema.py, tuples.py      shared error type, Column/Schema, tuple equality
dbms/ra/                                  Bonus 1 engine as a library (refactor, see docs/REFACTOR.md)
  tokenizer.py condition.py parser.py nodes.py plan_json.py evaluate.py tables.py format.py
dbms/sql/                                 Module 3: lexer.py parser.py translate.py cli.py
dbms/optimizer/                           Module 4: stats.py estimate.py rewrite.py planner.py api.py cli.py
dbms/system.py                            Engine: SQL text in, rows out (integration)
dbms/execution/                           Module 2
  context.py sorter.py operators.py joins.py planner.py runner.py cli.py
dbms/storage/                             Module 1
  page.py records.py disk.py policies.py bufferpool.py heap.py catalog.py database.py
  loader.py trace.py layout.py cli.py
ra.py                                     relational algebra CLI (Bonus 1 style)
storage.py                                Module 1 CLI launcher
exec.py  sql.py  opt.py  shell.py         launchers for the four module tools and the shell
scripts/                                  measurement and demo scripts (see below)
tests/                                    test_ra_layer, test_m1, test_m2, test_m3, test_m4, test_system
DESIGN_M1..M4.md REPORT_M1, M2, M4.md GRAMMAR_SQL.md ARCHITECTURE.md DESIGN_LOG.md docs/REFACTOR.md
```

## Run

```
python -m unittest discover -s tests -v

python storage.py create-table --db mydb --name R --schema "a:INT,b:VARCHAR(20),c:INT"
python storage.py load   --db mydb --table R --file r.csv
python storage.py scan   --db mydb --table R --frames 8 --limit 20 --json
python storage.py layout --db mydb --table R --page 1
python storage.py trace  --file trace.txt --frames 16 --policy lru --json
python storage.py tables --db mydb

python exec.py run   --db mydb --plan plan.json --frames 32 --json
python exec.py trace --db mydb --plan plan.json --frames 32
python scripts/m2_measure.py all

python sql.py compile --catalog mydb --query "SELECT name FROM Emp WHERE age > 30" --json
python opt.py analyze --catalog mydb
python opt.py explain --catalog mydb --logical logical.json --frames 32 --trees
python opt.py dp-table --catalog mydb --logical logical.json
python shell.py --db mydb --frames 32          # SQL prompt: EXPLAIN, EXPLAIN ANALYZE, ANALYZE, SET frames = n, LOAD t FROM 'f.csv'
python scripts/make_demo_db.py --out demo_db   # a demo database with 12 queries, to try everything
python scripts/load_university.py --sql smallRelationsInsertFile.sql --out uni_db
python scripts/run_workload.py --db DIR --queries queries.sql --frames 32 --analyze
python scripts/m4_measure.py qerror|speedup|correlation --db DIR --queries q20.sql

python ra.py --tree "project[Name](select[Age>30](Employees))"
python ra.py --db data/employees.ra "project[DID](Employees)"
```

## Rules this code follows

- No regex, parser generators, eval/exec, pandas or database engines.
- Every page access goes through the buffer pool; every disk read and write is counted.
- Same data, same plan, same B gives the same counters.
