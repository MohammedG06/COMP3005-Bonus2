"""Replacement policies (spec section 5.1, 5.2).
The pool uses free frames first; a policy is only asked for a victim when every frame is occupied."""
from collections import OrderedDict


class LRUPolicy:
    name = "lru"

    def __init__(self, n):
        self.n = n
        self.order = OrderedDict()          # least recently used first

    def on_access(self, idx):               # hit
        self.order[idx] = None
        self.order.move_to_end(idx)

    on_load = on_access                     # a freshly loaded page is the most recently used

    def on_drop(self, idx):
        self.order.pop(idx, None)

    def choose_victim(self, frames):
        for idx in self.order:
            if frames[idx].pin == 0:
                return idx
        return None


class ClockPolicy:
    name = "clock"

    def __init__(self, n):
        self.n = n
        self.ref = [0] * n
        self.hand = 0

    def on_access(self, idx):               # hit: set the reference bit
        self.ref[idx] = 1

    def on_load(self, idx):                 # new page: ref bit 1, hand moves past it
        self.ref[idx] = 1
        self.hand = (idx + 1) % self.n

    def on_drop(self, idx):
        self.ref[idx] = 0

    def choose_victim(self, frames):
        swept = 0
        while True:
            f = self.hand
            if frames[f].pin > 0:
                self.hand = (self.hand + 1) % self.n
                swept += 1
            elif self.ref[f] == 1:
                self.ref[f] = 0
                self.hand = (self.hand + 1) % self.n
                swept += 1
            else:
                self.hand = (self.hand + 1) % self.n
                return f
            if swept > 2 * self.n:
                return None


def make_policy(name, n):
    name = name.lower()
    if name == "lru":
        return LRUPolicy(n)
    if name == "clock":
        return ClockPolicy(n)
    from ..errors import DBError
    raise DBError("storage", f"unknown replacement policy '{name}' (use lru or clock)")
