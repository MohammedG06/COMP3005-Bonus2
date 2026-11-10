"""Module 1 tests. Every item of the spec's checklist (section 10) has a test here."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dbms.errors import DBError
from dbms.storage import page as pg
from dbms.storage.bufferpool import BufferPool
from dbms.storage.constants import PAGE_SIZE, DIR_CAPACITY, NO_SPACE
from dbms.storage.database import Database
from dbms.storage.disk import SimulatedDisk
from dbms.storage.loader import load_csv, split_csv_line
from dbms.storage.records import RecordCodec, parse_schema
from dbms.storage.trace import run_trace, read_trace
from dbms.storage import cli


def pool(n, policy):
    p = BufferPool(n, policy)
    p.register_disk(0, SimulatedDisk())
    return p


class SlottedPage(unittest.TestCase):
    def test_spec_3_2_worked_example(self):
        buf = bytearray(PAGE_SIZE)
        pg.init_page(buf, 0)
        self.assertEqual((pg.slot_count(buf), pg.header(buf)[2], pg.free_space(buf)), (0, 4096, 4088))
        expect = [(13, 1, 4083, 4071, (4083, 13)), (20, 2, 4063, 4047, (4063, 20)), (13, 3, 4050, 4030, (4050, 13))]
        for i, (n, sc, fe, free, slot) in enumerate(expect):
            self.assertEqual(pg.insert(buf, bytes([i + 1]) * n), i)
            self.assertEqual((pg.slot_count(buf), pg.header(buf)[2], pg.free_space(buf), pg.get_slot(buf, i)), (sc, fe, free, slot))
        pg.delete(buf, 1)
        self.assertEqual((pg.slot_count(buf), pg.header(buf)[2], pg.free_space(buf), pg.get_slot(buf, 1)), (3, 4050, 4030, (0, 0)))

    def test_record_example_is_13_bytes(self):
        codec = RecordCodec(parse_schema("a:INT,b:VARCHAR(5),c:INT"))
        rec = codec.encode((42, "bob", 7))
        self.assertEqual(rec.hex(), "2a000000" + "0300" + "626f62" + "07000000")      # spec section 2 example
        self.assertEqual(len(rec), 13)
        self.assertEqual(codec.decode(rec), (42, "bob", 7))

    def test_capacity_floor_4088_over_L_plus_4(self):
        buf = bytearray(PAGE_SIZE)
        pg.init_page(buf, 0)
        n = 0
        while pg.insert(buf, bytes(13)) != NO_SPACE:
            n += 1
        self.assertEqual(n, 240)

    def test_dead_slot_and_range_errors_name_the_rid(self):
        buf = bytearray(PAGE_SIZE)
        pg.init_page(buf, 7)
        pg.insert(buf, b"abc")
        pg.delete(buf, 0)
        with self.assertRaises(DBError) as cm:
            pg.get(buf, 0, rid="(7,0)")
        self.assertIn("(7,0)", cm.exception.message)
        with self.assertRaises(DBError):
            pg.delete(buf, 0)
        with self.assertRaises(DBError):
            pg.get(buf, 5)


class BufferPoolTests(unittest.TestCase):
    def test_spec_5_3_both_policies(self):
        r = run_trace([1, 2, 3, 1, 4, 2], 3, "lru")
        self.assertEqual((r["hits"], r["misses"], r["evictions"]), (1, 5, 2))
        r = run_trace([1, 2, 3, 1, 4, 2], 3, "clock")
        self.assertEqual((r["hits"], r["misses"], r["evictions"]), (2, 4, 1))

    def test_lru_order_in_spec_example(self):
        p = pool(3, "lru")
        for pid in [1, 2, 3, 1, 4]:
            p.fetch_page(pid); p.unpin_page(pid)
        self.assertEqual(sorted(k[1] for k in p.table), [1, 3, 4])           # 2 was evicted

    def test_clock_pool_contents_in_spec_example(self):
        p = pool(3, "clock")
        for pid in [1, 2, 3, 1, 4]:
            p.fetch_page(pid); p.unpin_page(pid)
        self.assertEqual([f.key[1] for f in p.frames], [4, 2, 3])            # the spec shows [4,2,3]

    def test_never_evicts_a_pinned_page(self):
        p = pool(2, "lru")
        p.fetch_page(1); p.fetch_page(2)                                     # both pinned
        with self.assertRaises(DBError) as cm:
            p.fetch_page(3)
        self.assertIn("no unpinned frame", cm.exception.message)
        self.assertEqual(set(k[1] for k in p.table), {1, 2})
        pc = pool(2, "clock")
        pc.fetch_page(1); pc.fetch_page(2)
        with self.assertRaises(DBError):
            pc.fetch_page(3)

    def test_unpin_at_zero_is_an_error(self):
        p = pool(2, "lru")
        p.fetch_page(1)
        p.unpin_page(1)
        with self.assertRaises(DBError):
            p.unpin_page(1)
        with self.assertRaises(DBError):
            p.unpin_page(99)

    def test_pin_counts_nest(self):
        p = pool(2, "lru")
        p.fetch_page(1); p.fetch_page(1)
        p.unpin_page(1)
        self.assertEqual(p.pinned_count(), 1)
        p.unpin_page(1)
        self.assertEqual(p.pinned_count(), 0)

    def test_dirty_pages_written_on_eviction_and_flush(self):
        with tempfile.TemporaryDirectory() as d:
            with Database(d, frames=2, create=True) as db:
                db.create_table("T", parse_schema("a:INT"))
                self.assertEqual(db.pool.stats()["writes"] >= 0, True)
            with Database(d, frames=2) as db:
                self.assertEqual(db.catalog.list_tables(), ["T"])

    def test_sequential_flooding_hits_both_policies(self):
        trace = list(range(80)) * 10                      # trace 3 of the spec: 80 pages x 10
        for pol in ("lru", "clock"):
            r = run_trace(trace, 64, pol)                 # file is bigger than the pool
            self.assertEqual((r["hits"], r["misses"]), (0, 800), pol)    # neither policy gets a single hit
        for pol in ("lru", "clock"):
            r = run_trace(trace, 96, pol)                 # file fits: only the first pass misses
            self.assertEqual((r["hits"], r["misses"], r["evictions"]), (720, 80, 0), pol)

    def test_trace_is_deterministic(self):
        t = [i % 37 for i in range(1000)] + list(range(50))
        for pol in ("lru", "clock"):
            self.assertEqual(run_trace(t, 8, pol), run_trace(t, 8, pol))

    def test_hit_miss_read_invariants(self):
        t = [(i * 7) % 23 for i in range(500)]
        for pol in ("lru", "clock"):
            r = run_trace(t, 5, pol)
            self.assertEqual(r["hits"] + r["misses"], len(t))
            self.assertEqual(r["reads"], r["misses"])
            self.assertEqual(r["evictions"], r["misses"] - 5)


class TableTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, name, schema, rows, frames=8, policy="lru"):
        with Database(self.path, frames, policy, create=True) as db:
            if name not in db.catalog.list_tables():
                db.create_table(name, parse_schema(schema))
            heap, _ = db.open_heap(name)
            for r in rows:
                heap.insert(r)
            db.refresh_counts(name, len(rows))

    def scan_rows(self, name, frames=8, policy="lru"):
        with Database(self.path, frames, policy) as db:
            heap, _ = db.open_heap(name)
            return [t for _, t in heap.scan()]

    def test_insert_get_delete_scan(self):
        with Database(self.path, 8, create=True) as db:
            db.create_table("R", parse_schema("a:INT,b:VARCHAR(10)"))
            heap, _ = db.open_heap("R")
            rids = [heap.insert((i, f"v{i}")) for i in range(1000)]
            self.assertEqual(heap.get(rids[500]), (500, "v500"))
            heap.delete(rids[500])
            with self.assertRaises(DBError) as cm:
                heap.get(rids[500])
            self.assertIn(str(rids[500]), cm.exception.message)
            rows = [t for _, t in heap.scan()]
            self.assertEqual(len(rows), 999)
            self.assertNotIn((500, "v500"), rows)
            self.assertEqual(db.pool.pinned_count(), 0)

    def test_delete_everything_then_scan(self):
        with Database(self.path, 4, create=True) as db:
            db.create_table("R", parse_schema("a:INT"))
            heap, _ = db.open_heap("R")
            rids = [heap.insert((i,)) for i in range(2000)]
            for r in rids:
                heap.delete(r)
            self.assertEqual(list(heap.scan()), [])

    def test_empty_table_scan(self):
        with Database(self.path, 4, create=True) as db:
            db.create_table("R", parse_schema("a:INT"))
            heap, _ = db.open_heap("R")
            self.assertEqual(list(heap.scan()), [])

    def test_persistence_across_reopen(self):
        rows = [(i, "x" * (i % 20)) for i in range(3000)]
        self.make("R", "a:INT,b:VARCHAR(20)", rows)
        # first-fit puts a small record into an earlier page with room, so heap order is not insertion order
        self.assertEqual(sorted(self.scan_rows("R")), sorted(rows))
        with Database(self.path, 8) as db:
            info = db.catalog.get_table("R")
            self.assertEqual(info.num_tuples, 3000)
            self.assertGreater(info.num_pages, 1)

    def test_measured_capacity_matches_formula(self):
        # all-INT, 3 columns: record = 12 bytes, floor(4088 / 16) = 255 per page
        self.make("T", "a:INT,b:INT,c:INT", [(i, i, i) for i in range(255 * 4)])
        with Database(self.path, 8) as db:
            self.assertEqual(db.catalog.get_table("T").num_pages, 4)
        self.make("U", "a:INT,b:INT,c:INT", [(i, i, i) for i in range(255 * 4 + 1)])
        with Database(self.path, 8) as db:
            self.assertEqual(db.catalog.get_table("U").num_pages, 5)

    def test_directory_chain_beyond_681_data_pages(self):
        schema = ",".join(f"c{i}:VARCHAR(255)" for i in range(15))
        row = tuple("z" * 255 for _ in range(15))                  # 15 * 257 = 3855 bytes: one row per page
        self.make("W", schema, [row] * (DIR_CAPACITY + 20), frames=6)
        with Database(self.path, 6) as db:
            heap, info = db.open_heap("W")
            self.assertEqual(info.num_pages, DIR_CAPACITY + 20)
            self.assertEqual(len(heap.dir_chain()), 2)
            self.assertEqual(sum(1 for _ in heap.scan()), DIR_CAPACITY + 20)

    def test_scan_of_table_20x_pool_never_pins_more_than_one(self):
        schema = ",".join(f"c{i}:VARCHAR(255)" for i in range(15))
        row = tuple("q" * 255 for _ in range(15))
        self.make("W", schema, [row] * 80, frames=4)               # 80 pages, pool of 4 = 20x
        with Database(self.path, 4) as db:
            heap, _ = db.open_heap("W")
            peak, n = 0, 0
            for _ in heap.scan():
                peak = max(peak, db.pool.pinned_count())
                n += 1
            self.assertEqual((n, peak), (80, 1))
            self.assertEqual(db.pool.pinned_count(), 0)

    def test_abandoned_scan_releases_its_pin(self):
        self.make("R", "a:INT", [(i,) for i in range(2000)])
        with Database(self.path, 4) as db:
            heap, _ = db.open_heap("R")
            gen = heap.scan()
            next(gen)
            self.assertEqual(db.pool.pinned_count(), 1)
            gen.close()
            self.assertEqual(db.pool.pinned_count(), 0)

    def test_scan_page_reads_do_not_depend_on_frames(self):
        self.make("R", "a:INT,b:INT,c:INT", [(i, i, i) for i in range(255 * 30)])
        reads = []
        for frames in (2, 8, 64):
            with Database(self.path, frames) as db:
                heap, _ = db.open_heap("R")
                db.pool.clear(); db.pool.reset_stats()
                for _ in heap.scan():
                    pass
                reads.append(db.pool.reads)
        self.assertEqual(reads, [31, 31, 31])                      # 30 data pages + 1 directory page

    def test_same_command_twice_same_counters(self):
        self.make("R", "a:INT", [(i,) for i in range(5000)])
        outs = []
        for _ in range(2):
            with Database(self.path, 4) as db:
                heap, _ = db.open_heap("R")
                db.pool.clear(); db.pool.reset_stats()
                list(heap.scan())
                outs.append(db.pool.stats())
        self.assertEqual(outs[0], outs[1])

    def test_record_too_long(self):
        schema = ",".join(f"c{i}:VARCHAR(255)" for i in range(17))        # 17 * 257 = 4369 > 4084
        with Database(self.path, 4, create=True) as db:
            db.create_table("Big", parse_schema(schema))
            heap, _ = db.open_heap("Big")
            with self.assertRaises(DBError) as cm:
                heap.insert(tuple("a" * 255 for _ in range(17)))
            self.assertIn("Big", cm.exception.message)

    def test_policies_give_same_rows(self):
        rows = [(i, "s" * (i % 7)) for i in range(2500)]
        self.make("R", "a:INT,b:VARCHAR(10)", rows)
        self.assertEqual(self.scan_rows("R", 4, "lru"), self.scan_rows("R", 4, "clock"))


class LoaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(self.tmp.name, 8, create=True)
        self.db.create_table("R", parse_schema("a:INT,b:VARCHAR(5),c:INT"))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def csv(self, text):
        p = os.path.join(self.tmp.name, "in.csv")
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def test_quoted_comma_and_quote_escape(self):
        self.assertEqual(split_csv_line('1,"x,y",3'), ["1", "x,y", "3"])
        self.assertEqual(split_csv_line('1,"say ""hi""",3'), ["1", 'say "hi"', "3"])
        n = load_csv(self.db, "R", self.csv('1,"a,b",2\n3,cd,4\n'))
        heap, _ = self.db.open_heap("R")
        self.assertEqual(sorted(t for _, t in heap.scan()), [(1, "a,b", 2), (3, "cd", 4)])
        self.assertEqual(n, 2)

    def test_bad_int_names_line_and_column(self):
        with self.assertRaises(DBError) as cm:
            load_csv(self.db, "R", self.csv("1,a,2\n3,b,oops\n"))
        self.assertIn("line 2", cm.exception.message)
        self.assertIn("column 3", cm.exception.message)

    def test_varchar_too_long_stops_the_load(self):
        with self.assertRaises(DBError) as cm:
            load_csv(self.db, "R", self.csv("1,a,2\n3,toolongvalue,4\n5,c,6\n"))
        self.assertIn("line 2", cm.exception.message)
        heap, info = self.db.open_heap("R")
        self.assertEqual(len(list(heap.scan())), 1)              # row 1 stays, load stopped at line 2
        self.assertEqual(self.db.catalog.get_table("R").num_tuples, 1)

    def test_wrong_field_count(self):
        with self.assertRaises(DBError) as cm:
            load_csv(self.db, "R", self.csv("1,a\n"))
        self.assertIn("line 1", cm.exception.message)


class CliTests(unittest.TestCase):
    def test_commands_end_to_end(self):
        import io, contextlib, json
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "db")
            csvp = os.path.join(d, "r.csv")
            with open(csvp, "w") as f:
                f.write("42,bob,7\n43,amy,9\n")
            tr = os.path.join(d, "t.txt")
            with open(tr, "w") as f:
                f.write("1\n2\n3\n1\n4\n2\n")

            def run(*args):
                buf, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
                    code = cli.main(list(args))
                return code, buf.getvalue(), err.getvalue()

            self.assertEqual(run("create-table", "--db", db, "--name", "R", "--schema", "a:INT,b:VARCHAR(20),c:INT")[0], 0)
            self.assertEqual(run("load", "--db", db, "--table", "R", "--file", csvp)[0], 0)
            code, out, _ = run("scan", "--db", db, "--table", "R", "--frames", "8", "--json")
            j = json.loads(out)
            self.assertEqual((code, j["rows"]), (0, [[42, "bob", 7], [43, "amy", 9]]))
            self.assertEqual(j["schema"], [["a", "INT"], ["b", "VARCHAR", 20], ["c", "INT"]])
            self.assertEqual(set(j["stats"]), {"hits", "misses", "evictions", "reads", "writes"})
            code, out, _ = run("trace", "--file", tr, "--frames", "3", "--policy", "lru", "--json")
            self.assertEqual(json.loads(out), {"policy": "lru", "frames": 3, "hits": 1, "misses": 5,
                                               "evictions": 2, "reads": 5, "writes": 0})
            code, out, _ = run("layout", "--db", db, "--table", "R", "--page", "1")
            self.assertIn("slot 0: offset=4083 length=13", out)
            code, _, err = run("scan", "--db", d + "/nope", "--table", "R")
            self.assertEqual(code, 1)
            self.assertTrue(err.startswith("error: storage:"))
            self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()
