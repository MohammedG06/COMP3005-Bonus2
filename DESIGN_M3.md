# DESIGN_M3.md: SQL front end

Assumption: no sample catalog was provided, so the tests build their own tables (university style schemas plus the Emp, Dept and Proj demo tables), and the ten required cases are checked against those.

Code: `dbms/sql/` (`lexer.py`, `parser.py`, `translate.py`, `cli.py`). Tests: `tests/test_m3.py` (41 tests).

## 1. Pipeline

SQL text -> tokens -> AST (`parser.py`) -> scope + name resolution + type checks + translation
(`translate.py`) -> logical plan (the same `Node` trees that serialize to the JSON contract).
Nothing is optimized here: a predicate stays exactly where the SQL put it.

## 2. Name resolution

* Each FROM item adds one entry to a **scope** under its alias, or its table name when there is no alias.
  A repeated entry name is a `name` error ("duplicate alias").
* `t.c`: find entry `t`; unknown entry -> `name` ("unknown table or alias"); entry has no `c` -> `name`
  ("does not exist in 't'"). So the two failures have different messages.
* Unqualified `c`: look in every entry. None -> `name`; more than one -> `ambiguity`, and the message lists
  every candidate (`Emp.did or Dept.did`).
* After resolution every column in the plan is `qualifier.name`. The only unqualified names in a plan are
  aggregate outputs and aliases (`n`, `count_star`).
* `SELECT *` expands, in FROM order, to every column of every entry, written out in full.
* A subquery in FROM is compiled first; its output column names must be unique (a `schema` error otherwise,
  because `rename` would make two columns collide); it is wrapped in `rename[alias]`.

## 3. Bag semantics: where duplicates are removed (and nowhere else)

* `project` never removes duplicates.
* `distinct` is emitted for `SELECT DISTINCT`, with one exception: an aggregating query whose select list
  keeps every grouping column already has one row per group, so `distinct` would be redundant and is not emitted.
  (`SELECT DISTINCT COUNT(*) ... GROUP BY did` does need it, because the grouping column was left out.)
* `UNION` -> `union` (removes duplicates), `UNION ALL` -> `union_all`, `INTERSECT` / `EXCEPT` are set operators.
* `GROUP BY` produces one row per group by construction.
* **Why DISTINCT is not free:** it is a blocking operator that must see every output row before it can emit one.
  It costs an external sort or a hash table over the whole result, and when that does not fit in memory it
  writes and re-reads the result. A plain projection costs nothing extra.

## 4. Aggregation rules

* In a query with GROUP BY or aggregates, every plain select item must be a grouping column
  (`aggregate` error otherwise, naming the column). `SELECT *` there is an `aggregate` error.
* Aggregates in `WHERE` or `JOIN ... ON` -> `aggregate` error. `SUM`/`AVG` of a non-INT -> `type` error.
* Aggregate output names: the alias, else `count_star`, `avg_Emp_salary` (function, qualified column with
  dots replaced by underscores).
* HAVING may only use grouping columns and aggregates. An aggregate already in the select list is reused by
  name (`COUNT(*) > 5` becomes `n > 5`). One that is not is added to the group_by as a hidden aggregate
  and projected away.
* `COUNT(*)` with no GROUP BY is a `group_by` with no keys, so it yields one row even on an empty table.

## 5. ORDER BY placement (a decision the handout leaves open)

The canonical order puts `sort` above `project`. That cannot work when the sort column is not selected,
because the projection has already dropped it. Rule: resolve each ORDER BY name against the output aliases
first, then against the FROM scope. If every key survives the projection, `sort` goes above it (canonical).
If some key does not, `sort` goes **below** the projection, where the column still exists (projection keeps
order). `SELECT DISTINCT` with such a key is a `schema` error (the SQL standard forbids it too), and so is
mixing an output alias with a hidden column.

## 6. Extension: column aliases

`SELECT name AS who` has no logical operator that renames a column, so the `project` node carries an optional
`aliases` list (one string or null per column). It is omitted when no plain column has an alias, so the
required plan shapes are unchanged. The executor and the reference evaluator both honour it.

## 7. Limits

No `IN`, `BETWEEN`, `LIKE`, arithmetic, `NULL`, `ORDER BY` on an expression, `INNER`/`OUTER` keywords, or
mixing commas and JOIN in one FROM. All are outside the required subset.

## 8. Evidence

All seven worked examples of the handout are asserted as exact plans, and the ten required cases plus five
extra cases each have a test. Privately (not part of the repository, because "no database engines"), the
translator and the reference evaluator were also compared with SQLite on 1,500 random queries: every result matched.


## Sources

- **Nystrom, [Crafting Interpreters](https://craftinginterpreters.com/).** The scanner and recursive descent parser follow its structure: one function per grammar rule, one token of lookahead, and precedence handled by calling the next-higher-precedence rule. Errors carry line and column from the token.
- **PostgreSQL documentation, [SELECT](https://www.postgresql.org/docs/current/sql-select.html).** I used it to settle bag against set semantics (UNION removes duplicates, UNION ALL keeps them, DISTINCT is explicit) and where HAVING and WHERE apply relative to GROUP BY.
- **Ramakrishnan and Gehrke, ch. 4 and 5.** The translation from SQL to relational algebra (FROM becomes joins, WHERE a select above them, SELECT a project on top).
