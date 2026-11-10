"""Runs a physical plan against a database directory and returns rows plus counters."""
from ..errors import DBError
from ..ra.plan_json import from_json, from_dict
from ..storage.database import Database
from .context import ExecContext
from .operators import END
from .planner import build_operator


def _walk(op, depth=0):
    yield op, depth
    for c in op.children:
        yield from _walk(c, depth + 1)


def node_report(op, depth):
    return {"depth": depth, "label": op.label(), "op": op.name,
            "tuples_out": op.tuples_out, "comparisons": op.comparisons,
            "own_reads": op.page_reads, "own_writes": op.page_writes,
            "reads": op.cum_reads(), "writes": op.cum_writes(),
            "info": op.info, "notes": op.notes}


def execute(db_path, plan, frames, policy="lru"):
    """plan: JSON text or an already-parsed dict. Raises DBError on any plan or data problem."""
    db = Database(db_path, frames, policy)
    ctx = ExecContext(db, frames)
    root = None
    try:
        node = from_json(plan, "physical") if isinstance(plan, str) else from_dict(plan, "physical")
        root = build_operator(node, ctx)                  # every validation error is raised here
        db.pool.reset_stats()
        root.open()
        rows = []
        while True:
            t = root.next()
            if t is END:
                break
            rows.append(t)
        root.close()
        if db.pool.n_pinned or db.pool.leased or ctx.temps:
            raise DBError("plan", f"plan finished with {db.pool.n_pinned} pinned frames, "
                          f"{len(db.pool.leased)} leased frames and {len(ctx.temps)} temp files left (bug)")
        nodes = [node_report(o, d) for o, d in _walk(root)]
        totals = {"tuples_out": root.tuples_out,
                  "comparisons": sum(o.comparisons for o, _ in _walk(root)),
                  "page_reads": sum(o.page_reads for o, _ in _walk(root)),
                  "page_writes": sum(o.page_writes for o, _ in _walk(root))}
        return {"schema": root.schema.to_json(), "rows": rows, "totals": totals,
                "frames": frames, "peak_frames": db.pool.peak_held, "nodes": nodes}
    finally:
        if root is not None:
            for o, _ in _walk(root):
                try:
                    o.close()
                except Exception:
                    pass
        if db.pool.leased:
            db.pool.release(list(db.pool.leased))
        ctx.cleanup()
        db.close()
