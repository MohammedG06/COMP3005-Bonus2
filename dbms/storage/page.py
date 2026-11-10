"""Slotted page (spec section 3). All functions work on a 4096-byte bytearray.

offset 0  page_id    u32
offset 4  slot_count u16   (includes dead slots)
offset 6  free_end   u16   lowest byte used by record data
offset 8  slot i at 8+4*i: offset u16, length u16     (offset 0 = dead)
Record data grows downward from byte 4096.
"""
import struct

from ..errors import DBError
from .constants import PAGE_SIZE, PAGE_HEADER, SLOT_SIZE, NO_SPACE

_HDR = struct.Struct("<IHH")
_SLOT = struct.Struct("<HH")


def init_page(buf, page_id):
    buf[:] = bytes(PAGE_SIZE)
    _HDR.pack_into(buf, 0, page_id, 0, PAGE_SIZE)


def header(buf):
    return _HDR.unpack_from(buf, 0)       # (page_id, slot_count, free_end)


def slot_count(buf):
    return struct.unpack_from("<H", buf, 4)[0]


def free_space(buf):
    _, sc, fe = _HDR.unpack_from(buf, 0)
    return fe - (PAGE_HEADER + SLOT_SIZE * sc)


def get_slot(buf, slot_no):
    return _SLOT.unpack_from(buf, PAGE_HEADER + SLOT_SIZE * slot_no)


def insert(buf, record):
    """-> slot number, or NO_SPACE"""
    pid, sc, fe = _HDR.unpack_from(buf, 0)
    length = len(record)
    if fe - (PAGE_HEADER + SLOT_SIZE * sc) < length + SLOT_SIZE:
        return NO_SPACE
    fe -= length
    buf[fe:fe + length] = record
    _SLOT.pack_into(buf, PAGE_HEADER + SLOT_SIZE * sc, fe, length)
    _HDR.pack_into(buf, 0, pid, sc + 1, fe)
    return sc


def get(buf, slot_no, rid=None):
    sc = slot_count(buf)
    where = rid if rid is not None else f"slot {slot_no}"
    if slot_no < 0 or slot_no >= sc:
        raise DBError("storage", f"rid {where}: slot out of range (page has {sc} slots)")
    off, length = get_slot(buf, slot_no)
    if off == 0:
        raise DBError("storage", f"rid {where}: slot is dead (the record was deleted)")
    return bytes(buf[off:off + length])


def delete(buf, slot_no, rid=None):
    sc = slot_count(buf)
    where = rid if rid is not None else f"slot {slot_no}"
    if slot_no < 0 or slot_no >= sc:
        raise DBError("storage", f"rid {where}: slot out of range (page has {sc} slots)")
    off, _ = get_slot(buf, slot_no)
    if off == 0:
        raise DBError("storage", f"rid {where}: slot is already dead")
    _SLOT.pack_into(buf, PAGE_HEADER + SLOT_SIZE * slot_no, 0, 0)


def live_slots(buf):
    """-> list of (slot_no, bytes) for live slots, in slot order."""
    sc = slot_count(buf)
    out = []
    for i in range(sc):
        off, length = _SLOT.unpack_from(buf, PAGE_HEADER + SLOT_SIZE * i)
        if off:
            out.append((i, bytes(buf[off:off + length])))
    return out
