"""`storage layout`: dump one page slot by slot with byte offsets."""
import struct

from . import page as pg
from .constants import DIR_HEADER, DIR_ENTRY, PAGE_HEADER, SLOT_SIZE, NO_PAGE
from .heap import dir_entries


def layout_text(buf, page_id, is_dir, codec=None):
    out = []
    if is_dir:
        pid, nxt, cnt = struct.unpack_from("<IIH", buf, 0)
        out.append(f"page {page_id}: DIRECTORY page")
        out.append("  offset  size  field")
        out.append(f"  {0:>6}  {4:>4}  page_id        = {pid}")
        out.append(f"  {4:>6}  {4:>4}  next_dir_page  = {'none' if nxt == NO_PAGE else nxt}")
        out.append(f"  {8:>6}  {2:>4}  entry_count    = {cnt}")
        _, entries = dir_entries(buf)
        for i, (dp, free) in enumerate(entries):
            off = DIR_HEADER + DIR_ENTRY * i
            out.append(f"  {off:>6}  {DIR_ENTRY:>4}  entry {i}: data_page={dp} free_bytes={free}")
        out.append(f"  directory page holds {cnt} of {(4096 - DIR_HEADER) // DIR_ENTRY} entries")
        return "\n".join(out)
    pid, sc, fe = pg.header(buf)
    out.append(f"page {page_id}: DATA page")
    out.append("  offset  size  field")
    out.append(f"  {0:>6}  {4:>4}  page_id    = {pid}")
    out.append(f"  {4:>6}  {2:>4}  slot_count = {sc}")
    out.append(f"  {6:>6}  {2:>4}  free_end   = {fe}")
    out.append(f"  free space = free_end - (8 + 4 x slot_count) = {fe} - {PAGE_HEADER + SLOT_SIZE * sc} = {pg.free_space(buf)}")
    out.append("  slot directory:")
    for i in range(sc):
        off, ln = pg.get_slot(buf, i)
        at = PAGE_HEADER + SLOT_SIZE * i
        if off == 0:
            out.append(f"  {at:>6}  {SLOT_SIZE:>4}  slot {i}: DEAD (0,0)")
            continue
        extra = ""
        if codec is not None:
            try:
                extra = "  " + repr(codec.decode(bytes(buf[off:off + ln])))
            except Exception:
                extra = "  <undecodable>"
        out.append(f"  {at:>6}  {SLOT_SIZE:>4}  slot {i}: offset={off} length={ln}  data bytes [{off}..{off + ln - 1}]{extra}")
    out.append(f"  free area: bytes [{PAGE_HEADER + SLOT_SIZE * sc}..{fe - 1}]   record data: bytes [{fe}..4095]")
    return "\n".join(out)
