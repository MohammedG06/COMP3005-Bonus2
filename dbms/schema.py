"""Column and Schema. Every column is (qualifier, name, type); the qualifier may be None
(aggregate outputs such as count_star have no table)."""

INT = "INT"
VARCHAR = "VARCHAR"
REAL = "REAL"   # only produced by AVG
NUMERIC = (INT, REAL)


def comparable(t1, t2):
    return t1 == t2 or (t1 in NUMERIC and t2 in NUMERIC)


class Column:
    __slots__ = ("qual", "name", "type", "length")

    def __init__(self, name, type_, length=None, qual=None):
        self.qual = qual
        self.name = name
        self.type = type_
        self.length = length

    @property
    def qname(self):
        return f"{self.qual}.{self.name}" if self.qual else self.name

    def with_qual(self, qual):
        return Column(self.name, self.type, self.length, qual)

    def key(self):
        return (self.qual, self.name)

    def to_json(self, qualified=True):
        nm = self.qname if qualified else self.name
        return [nm, self.type, self.length] if self.type == VARCHAR else [nm, self.type]

    def __eq__(self, o):
        return isinstance(o, Column) and (self.qual, self.name, self.type, self.length) == \
            (o.qual, o.name, o.type, o.length)

    def __hash__(self):
        return hash((self.qual, self.name, self.type, self.length))

    def __repr__(self):
        t = f"{self.type}({self.length})" if self.length else self.type
        return f"{self.qname}:{t}"


class Schema:
    def __init__(self, cols):
        self.cols = list(cols)

    def __len__(self):
        return len(self.cols)

    def __iter__(self):
        return iter(self.cols)

    def __getitem__(self, i):
        return self.cols[i]

    def types(self):
        return [c.type for c in self.cols]

    def qnames(self):
        return [c.qname for c in self.cols]

    def find(self, qual, name):
        """Indexes of columns matching name (and qual, when given)."""
        return [i for i, c in enumerate(self.cols)
                if c.name == name and (qual is None or c.qual == qual)]

    def qualified(self, qual):
        return Schema([c.with_qual(qual) for c in self.cols])

    def concat(self, other):
        return Schema(self.cols + other.cols)

    def first_duplicate(self):
        seen = set()
        for c in self.cols:
            if c.key() in seen:
                return c
            seen.add(c.key())
        return None

    def to_json(self, qualified=True):
        return [c.to_json(qualified) for c in self.cols]
