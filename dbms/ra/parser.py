"""Parser for the relational algebra text language from Bonus Project 1.
Precedence (loosest to tightest): union/minus, intersect, times/join. All left associative.
Produces the same plan nodes as the SQL front end will (minus -> except, times -> cross)."""
from .condition import ConditionParser
from .nodes import Node


class QueryParser(ConditionParser):
    def parse_query(self):
        node = self.parse_expr()
        t = self.peek()
        if t.type != "EOF":
            if t.type == ")":
                self.fail("unmatched ')' (no '(' is open here)")
            self.fail(f"unexpected {self.describe(t)}; expected an operator "
                      f"(union, minus, intersect, times, join) or end of input")
        return node

    def parse_expr(self):          # expr ::= term { ("union"|"minus") term }
        left = self.parse_term()
        while self.is_word("union", "minus"):
            op = self.advance()
            right = self.parse_term()
            left = Node("union" if op.text == "union" else "except", op.pos, left=left, right=right)
        return left

    def parse_term(self):          # term ::= factor { "intersect" factor }
        left = self.parse_factor()
        while self.is_word("intersect"):
            op = self.advance()
            left = Node("intersect", op.pos, left=left, right=self.parse_factor())
        return left

    def parse_factor(self):        # factor ::= unit { ("times" | "join" "[" cond "]") unit }
        left = self.parse_unit()
        while self.is_word("times", "join"):
            op = self.advance()
            if op.text == "times":
                left = Node("cross", op.pos, left=left, right=self.parse_unit())
            else:
                lb = self.expect("[")
                cond = self.parse_cond()
                self.expect("]", lb)
                left = Node("join", op.pos, cond=cond, left=left, right=self.parse_unit())
        return left

    def parse_unit(self):
        t = self.peek()
        if t.type == "(":
            lp = self.advance()
            e = self.parse_expr()
            self.expect(")", lp)
            return e
        if t.type == "IDENT":
            if t.text in ("select", "project", "rename") and self.peek(1).type == "[":
                return self.parse_unary()
            self.advance()
            return Node("scan", t.pos, table=t.text)
        self.fail(f"expected a relation name, select/project/rename, or '(' but found {self.describe(t)}")

    def parse_unary(self):
        kw = self.advance()
        lb = self.expect("[")
        if kw.text == "select":
            cond = self.parse_cond()
            self.expect("]", lb)
            return Node("select", kw.pos, cond=cond, child=self.parse_paren_expr())
        if kw.text == "project":
            if self.peek().type == "]":
                self.fail("expected an attribute name but found ']' (an empty attribute list is not a valid projection)")
            cols = [self.parse_attrref()]
            while self.peek().type == ",":
                self.advance()
                cols.append(self.parse_attrref())
            self.expect("]", lb)
            return Node("project", kw.pos, columns=cols, child=self.parse_paren_expr())
        name = self.expect_ident("a new relation name")
        self.expect("]", lb)
        return Node("rename", kw.pos, to=name.text, child=self.parse_paren_expr())

    def parse_paren_expr(self):
        lp = self.expect("(")
        e = self.parse_expr()
        self.expect(")", lp)
        return e


def parse_ra(source):
    return QueryParser(source).parse_query()
