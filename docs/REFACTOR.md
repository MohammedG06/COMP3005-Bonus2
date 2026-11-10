# Week one: engine refactor (Bonus 1 -> library)

Handout section 1.3, point by point.

1. **Separate the layers.** `dbms/ra/tokenizer.py`, `condition.py` + `parser.py`, `nodes.py`,
   `evaluate.py` import independently. Nothing in them prints or opens a file. The CLI is `ra.py`.
2. **Condition parser on its own.** `parse_condition(text)`, `unparse(tree)`, and
   `compile_condition(tree, schema)` in `dbms/ra/condition.py`.
3. **Qualified names everywhere.** Every column is (qualifier, name, type). Output headers are
   `R.a`. Aggregate outputs have no qualifier (`count_star`).
4. **Serialise the tree.** `to_json` / `from_json` in `plan_json.py`, with validation messages that
   name the node. `tests/test_ra_layer.py::test_json_round_trip_every_node_type` covers every logical
   and physical node type.
5. **Detach from data.** `evaluate(node, provider)` only uses `has_table`, `table_schema`,
   `table_rows` (rows may be a generator). `test_evaluator_has_no_dependency_on_lists` proves it.
6. **Sets to bags.** `project` keeps duplicates. `distinct` is its own operator. Added `union_all`,
   `group_by`, `sort`, `limit` so every logical operator of Module 3 exists.
7. **Test suite kept.** All 25 cases are in `tests/test_ra_layer.py`. Changed meaning:

| Case | Bonus 1 | Now | Why |
|---|---|---|---|
| 23 | `project[DID](Employees)` gave 2 tuples | gives 3 tuples (2 after `distinct`) | project is a bag operator (point 6) |
| 21 | union needed the same attribute names | needs same column count and types; names may differ, output takes the left names | Module 2 section 2.10 |
| 10, 11 | tree printed `Minus`, `Times` | prints `Except`, `Cross` | one shared set of logical operator names |
| ambiguous attribute | category "name" | category "ambiguity" | Module 3 error categories |

Other deliberate changes: `<>` is accepted as well as `!=`, and `and`/`or`/`not` are case
insensitive in conditions (SQL text will be upper case), numbers are INT (decimals only exist as
literals and as AVG output, type REAL).
