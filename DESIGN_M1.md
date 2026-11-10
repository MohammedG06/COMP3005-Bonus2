# DESIGN_M1.md: storage engine and buffer pool

Everything here matches the code in `dbms/storage/`. The page dumps are real output of
`python storage.py layout ...` (regenerate them on your own files before the oral check).

## 1. Files

- One database = one directory with `catalog.db` and one `<table>.db` per table.
- Every file is a plain sequence of 4096 byte pages. No header outside the pages.
- Page id = zero based index in its file. rid = (page id, slot number).
- All integers are little endian.
- All disk access goes through `DiskManager` (`disk.py`), which counts every read and write.
- One shared `BufferPool` serves the catalog and all tables, so B is one budget for the whole system.
  Pool pages are keyed by (file id, page id). File id 0 is the catalog.

## 2. Record format

- INT = 4 bytes, signed, little endian.
- VARCHAR(n) = u16 byte length L (L <= n), then L bytes of UTF-8. n is a limit in bytes.
- No record header, no null bitmap (no NULLs in this project).
- Example `(42, 'bob', 7)` on `(a INT, b VARCHAR(5), c INT)`:

```
2A 00 00 00 | 03 00 62 6F 62 | 07 00 00 00     = 13 bytes
   a=42         b='bob'           c=7
```

## 3. Slotted data page

```
byte 0                                                                         byte 4095
+------+------------+----------+---------+---------+-----+--------- free ---------+-----------+----------+----------+
| 0..3 | 4..5       | 6..7     | 8..11   | 12..15  | ... |                        | record 1  | record 0 | (end)    |
| page | slot_count | free_end | slot 0  | slot 1  |     |                        | (newer,   | (first   |          |
|  id  | u16        | u16      | off,len | off,len |     |                        |  lower)   |  record) |          |
+------+------------+----------+---------+---------+-----+------------------------+-----------+----------+----------+
        header = 8 bytes         slot directory grows ->            <- record data grows down from 4096
```

- Slot i lives at byte `8 + 4*i`: offset u16, length u16. Offset 0 means dead.
- Free space = `free_end - (8 + 4*slot_count)`. An insert of L bytes needs `L + 4`.
- Slot numbers are never reused or renumbered, so a rid stays valid.
- Delete only sets the slot to (0, 0).

Real page (3 rows loaded into `R(a INT, b VARCHAR(5), c INT)`, `layout --page 1`):

```
page 1: DATA page
  offset  size  field
       0     4  page_id    = 1
       4     2  slot_count = 3
       6     2  free_end   = 4058
  free space = free_end - (8 + 4 x slot_count) = 4058 - 20 = 4038
  slot directory:
       8     4  slot 0: offset=4083 length=13  data bytes [4083..4095]  (42, 'bob', 7)
      12     4  slot 1: offset=4070 length=13  data bytes [4070..4082]  (43, 'amy', 9)
      16     4  slot 2: offset=4058 length=12  data bytes [4058..4069]  (44, 'cy', 1)
  free area: bytes [20..4057]   record data: bytes [4058..4095]
```

The spec's worked example (13, 20, 13 byte inserts, delete slot 1) is reproduced exactly by
`tests/test_m1.py::SlottedPage::test_spec_3_2_worked_example`.

## 4. Heap file and directory

- Page 0 of every table file is the first directory page.

```
directory page:
  0..3   page_id u32
  4..7   next_dir_page u32 (0xFFFFFFFF = none)
  8..9   entry_count u16
  10..   entries, 6 bytes each: data_page_id u32, free_bytes u16
capacity = floor((4096 - 10) / 6) = 681 entries
```

- `free_bytes` is the data page's free space, using the formula in section 3.
- Insert is first fit: walk directory pages in order and take the first entry with
  `free_bytes >= len + 4`. If none fits, allocate a new data page and add an entry to the last
  directory page. If that one is full (681 entries), chain a new directory page.
- Tested with 701 data pages (two directory pages): `test_directory_chain_beyond_681_data_pages`.
- Scan: fetch a directory page, copy its entries, unpin it, then pin one data page at a time.
  So a scan holds at most one pinned page at any instant (this is also what lets `seqscan` in
  Module 2 use exactly 1 frame). The scan is a generator with `try/finally`, so closing it early
  (for example for `LIMIT`) releases the page.

## 5. Free space strategy and compaction

- Strategy: first fit through the directory, so an insert does not scan data pages.
- Compaction: **not implemented** (it is optional in the spec).
  - Reason: with no compaction, a delete never changes a page's free space, so the directory entry
    never needs updating on delete. That keeps delete at one page access.
  - Cost: space from deleted records is not reused. The spec's own example shows this.
- **Exception, the catalog only** (`HeapFile.rewrite_all`, used by `Catalog._replace`): ANALYZE and the tuple/page
  counters replace catalog records often, and with no reclaim the catalog file grew on every ANALYZE (found
  2026-11-10, see DESIGN_LOG.md). The catalog now rewrites itself from its live records into the pages it
  already has. This renumbers slots, which is fine for the catalog (nothing holds a catalog rid across calls)
  and would NOT be fine for user tables, whose rids must stay valid. Result: running ANALYZE twice leaves
  `catalog.db` byte for byte identical, as the Module 4 checklist requires.
- Heap order is not insertion order: a small record can land in an earlier page that still has
  room. Scan returns tuples in heap order, as the spec says.

## 6. Buffer pool

- Array of B frames. Each frame has bytes, pin count, dirty flag. A page table maps key to frame.
- Free frames are used first (lowest index first). The policy is only asked for a victim when
  every frame is occupied, and it only considers frames with pin count 0.
- If every frame is pinned, `fetch_page` fails with "no unpinned frame available". A pinned page is
  never evicted. Unpinning a page at pin count 0 is an error.
- Counting rules (exactly as the spec):
  - hit = fetch of a resident page; miss = fetch that needed a disk read; every miss is one read.
  - eviction = a miss that replaced a resident page, so the first B loads are not evictions.
  - write = one disk write, from evicting a dirty page or from `flush_all`.
- `new_page()` is not a fetch: it counts no hit, no miss, no read. If it has to replace a resident
  page that is still an eviction (and a write if the victim was dirty).
- LRU: ordered dict of frames by last access, hit or miss.
- CLOCK: reference bit per frame and a hand. A hit sets the bit. A newly loaded page gets bit 1 and the
  hand moves to the frame after it. The sweep follows the spec's pseudocode (gives up after 2B steps).
- Both spec examples (section 5.3, B = 3, trace 1 2 3 1 4 2) are tests, and both policies match
  the spec table, including the CLOCK pool contents `[4,2,3]`.

## 7. Catalog

- Stored in `catalog.db` as a heap file of raw records (same directory and page code as tables).
- First byte of every record is a kind: 1 = TABLE, 2 = reserved for column statistics (Module 4).
  Module 4 will add kind 2 records next to the table records, so nothing needs to change.
- TABLE record:

```
1  kind (1)
1+n  name         u8 length + UTF-8
1+n  data file    u8 length + UTF-8
4  first_page u32 (always 0, the first directory page)
4  num_pages u32  (data pages only, directory pages not counted)
8  num_tuples u64
2  ncols u16
per column:  name (u8 length + UTF-8), type u8 (1 INT, 2 VARCHAR), length u16
```

Real record for `R(a INT, b VARCHAR(20), c INT)` with 1 page and 3 tuples (41 bytes):

```
01 | 01 52 | 04 52 2E 64 62 | 00000000 | 01000000 | 0300000000000000 | 0300 | 01 61 01 0000 | 01 62 02 1400 | 01 63 01 0000
kind  "R"    "R.db"           first=0    pages=1    tuples=3           ncols=3   a INT          b VARCHAR(20)   c INT
```

- To change counts, the old record is deleted and a new one is inserted. Slots are never reused,
  so the catalog used to accumulate dead slots. It no longer does: see section 5 (`rewrite_all`).
- The catalog is read through the buffer pool on every lookup (no private cache).

## 8. Loader and CLI

- CSV: no header, one record per line, a field with a comma is in double quotes (`""` inside quotes
  is one quote). Hand written splitter. Blank lines are skipped, UTF-8 BOM is tolerated.
- First bad field stops the load, with line and column. Rows before it stay loaded and the catalog
  counts are updated for them.
- Commands: `create-table`, `load`, `scan`, `layout`, `trace` (plus `tables` for convenience).

## 9. Places where I had to choose (spec was open or ambiguous)

- **Largest storable record.** The spec says 4088 bytes, but an insert needs L + 4 bytes
  (record plus slot) and an empty page has 4088 free, so the real limit is **4084**. The code rejects
  anything above 4084 with an error naming the table and the row. Worth confirming with the professor.
- **`trace` uses a simulated disk.** The page ids in a trace can be anything, so pages read as zeros
  and the `--db` argument is accepted but not needed.
- **`scan` counters** describe the scan alone: the pool is emptied after the catalog lookup and its
  counters reset, so the numbers do not depend on what the catalog lookup left in memory.
  A full scan of P data pages costs P + (number of directory pages) reads.
- **Varchar length** is measured in bytes of UTF-8.
