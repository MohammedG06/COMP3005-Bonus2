#!/usr/bin/env python3
"""Writes five deterministic synthetic page traces (one page id per line) for REPORT_M1 table 2.

  python scripts/make_traces.py --out traces

The professor's five traces were not published (only "trace 3 = an 80 page file scanned 10 times" is described),
so these are OUR OWN traces, built to show different behaviours. Say so in REPORT_M1. If the real traces appear,
run m1_policies.py on them instead. Seeded: the same files every time."""
import argparse
import os
import random


def traces():
    r = random.Random(3005)
    t1 = [r.randrange(200) for _ in range(5000)]                                   # uniform random, 200 pages
    t2 = [r.randrange(50) if r.random() < 0.8 else 50 + r.randrange(450)           # 80/20 hot set, 500 pages
          for _ in range(5000)]
    t3 = list(range(80)) * 10                                                      # 80 page file scanned 10 times
    t4 = list(range(300)) * 5                                                      # loop bigger than B = 256
    t5 = []                                                                        # hot pages + occasional scans
    for i in range(50):
        t5 += [r.randrange(20) for _ in range(60)]
        if i % 5 == 4:
            t5 += list(range(100, 220))
    return [("trace1_uniform.txt", t1), ("trace2_hotset.txt", t2), ("trace3_scan80x10.txt", t3),
            ("trace4_loop300x5.txt", t4), ("trace5_hot_plus_scans.txt", t5)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    for name, ids in traces():
        with open(os.path.join(a.out, name), "w", newline="\n") as f:
            f.write("\n".join(map(str, ids)) + "\n")
        print(f"{name}: {len(ids)} accesses")


if __name__ == "__main__":
    main()
