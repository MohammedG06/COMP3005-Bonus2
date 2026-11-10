"""`storage trace`: replay a file of page ids through a buffer pool. The disk is simulated
(pages read as zeros), so the page ids in a trace can be anything."""
from ..errors import DBError
from .bufferpool import BufferPool
from .disk import SimulatedDisk


def read_trace(path):
    ids = []
    try:
        with open(path, encoding="utf-8-sig") as fh:
            for lineno, raw in enumerate(fh, 1):
                s = raw.strip()
                if not s:
                    continue
                if not s.lstrip("-").isdigit() or int(s) < 0:
                    raise DBError("data", f"{path}, line {lineno}: '{s}' is not a page id")
                ids.append(int(s))
    except OSError as e:
        raise DBError("storage", f"cannot read '{path}': {e.strerror}")
    return ids


def run_trace(page_ids, frames, policy):
    pool = BufferPool(frames, policy)
    pool.register_disk(0, SimulatedDisk())
    for pid in page_ids:
        pool.fetch_page(pid)
        pool.unpin_page(pid, False)
    s = pool.stats()
    return {"policy": policy.lower(), "frames": frames, **s}
