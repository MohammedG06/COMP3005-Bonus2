# GRAMMAR_SQL.md

Matches `dbms/sql/lexer.py` and `dbms/sql/parser.py` (one parser method per rule below).

## Tokens (hand written scanner, no regex)

```ebnf
IDENT   ::= letter { letter | digit | "_" }          (* case sensitive; a keyword cannot be an IDENT *)
NUM     ::= [ "+" | "-" ] digit { digit }            (* integers only *)
STR     ::= "'" { any-char-but-quote | "''" } "'"
CMP     ::= "=" | "<>" | "<" | "<=" | ">" | ">="     (* "!=" is accepted as another spelling of "<>" *)
keywords (any letter case): CREATE TABLE INSERT INTO VALUES SELECT DISTINCT FROM WHERE GROUP BY HAVING ORDER
  LIMIT UNION ALL INTERSECT EXCEPT JOIN ON AS AND OR NOT ASC DESC INT VARCHAR COUNT SUM AVG MIN MAX
comments: "--" to the end of the line
```

Maximal munch: after `<` the scanner looks at the next character for `=` or `>`, after `>` for `=`.
A `-` starts a number only when a digit follows it, and `--` starts a comment.

## Statements and queries

```ebnf
script      ::= { statement [ ";" ] }
statement   ::= create | insert | query
create      ::= "CREATE" "TABLE" IDENT "(" coldef { "," coldef } ")"
coldef      ::= IDENT ( "INT" | "VARCHAR" "(" NUM ")" )
insert      ::= "INSERT" "INTO" IDENT "VALUES" "(" literal { "," literal } ")"
literal     ::= NUM | STR

query       ::= block { ( "UNION" [ "ALL" ] | "INTERSECT" | "EXCEPT" ) block }
block       ::= select | "(" query ")"
select      ::= "SELECT" [ "DISTINCT" ] select_list "FROM" from_list
                [ "WHERE" cond ]
                [ "GROUP" "BY" column { "," column } [ "HAVING" cond ] ]
                [ "ORDER" "BY" sort_item { "," sort_item } ]
                [ "LIMIT" NUM ]
select_list ::= "*" | item { "," item }
item        ::= ( aggregate | column ) [ "AS" IDENT ]
aggregate   ::= ( "COUNT" | "SUM" | "AVG" | "MIN" | "MAX" ) "(" ( column | "*" ) ")"   (* "*" only for COUNT *)
sort_item   ::= column [ "ASC" | "DESC" ]
from_list   ::= table_ref { "," table_ref }
              | table_ref { "JOIN" table_ref "ON" cond }
table_ref   ::= IDENT [ [ "AS" ] IDENT ]  |  "(" query ")" [ "AS" ] IDENT
column      ::= IDENT [ "." IDENT ]

cond        ::= and_cond { "OR" and_cond }
and_cond    ::= not_cond { "AND" not_cond }
not_cond    ::= "NOT" not_cond | "(" cond ")" | comparison
comparison  ::= operand CMP operand
operand     ::= column | NUM | STR | aggregate
```

## Precedence and associativity

| Level | Construct | Associativity | Rule |
|---|---|---|---|
| 1 | `UNION`, `UNION ALL`, `INTERSECT`, `EXCEPT` | left | `query` (all four on one level, so `A UNION B EXCEPT C` is `(A UNION B) EXCEPT C`) |
| 2 | `JOIN ... ON` chain | left deep | `from_list` |
| condition 1 (loosest) | `OR` | left | `cond` |
| condition 2 | `AND` | left | `and_cond` |
| condition 3 (tightest) | `NOT` | prefix | `not_cond` |

The precedence lives in the grammar (one rule per level). Left recursion is removed the same way as in
Bonus 1: `query ::= block { setop block }` is a loop that wraps the tree built so far as the left child.
Strategy: hand written recursive descent, panic mode (the first error stops the parse).

## Where this differs from the sketch in the handout, and why

* **Bare aliases** (`FROM Emp e`) are accepted next to `AS`: the handout's own examples use them.
* **Aggregates are accepted as operands** of a comparison, even though no aggregate may be used in `WHERE`
  or `JOIN ... ON`. The parser accepts them so the analyzer can report the precise `aggregate` error that
  required case 7 asks for, instead of a vague syntax error.
* `!=` is accepted as `<>`. A subquery in `FROM` must have an alias (`syntax` error otherwise).
* `ORDER BY` and `LIMIT` belong to the select block they follow, as the grammar says (not to a whole UNION).

## Errors

`error: <category>: <message> at line L, column C`, categories: lexical, syntax, name, ambiguity, type,
aggregate, schema. Positions are 1 based and, in the shell, count from the start of the whole script.
