"""Power of GPU 41:00 vs the mean of the other three, while all four are at util>=90 %.

Per 10-minute bucket: ratio and how often 41:00 reports SW power cap (bit 0x4) below 200 W.
Usage: ratio.py FILE [FILE...]"""
import csv, gzip, sys, collections, datetime as dt

for f in sys.argv[1:]:
    op = gzip.open if f.endswith(".gz") else open
    by_t = collections.defaultdict(dict)
    with op(f, "rt") as fh:
        r = csv.reader(fh, skipinitialspace=True)
        next(r, None)
        for row in r:
            try:
                t = row[0][:19]
                by_t[t][row[2][9:11]] = (float(row[8].split()[0]), int(row[13].split()[0]), int(row[7], 16))
            except (ValueError, IndexError):
                continue
    buckets = collections.defaultdict(list)
    for t, g in by_t.items():
        if len(g) != 4 or any(v[1] < 90 for v in g.values()):
            continue
        others = [g[k][0] for k in g if k != "41"]
        ratio = g["41"][0] / (sum(others) / 3)
        low_cap = g["41"][2] & 0x4 and g["41"][0] < 200
        buckets[t[:15]].append((ratio, low_cap))
    for b in sorted(buckets):
        v = buckets[b]
        if len(v) < 30:
            continue
        mean = sum(x[0] for x in v) / len(v)
        lc = 100 * sum(1 for x in v if x[1]) / len(v)
        flag = " <==" if mean < 0.9 else ""
        print(f"{b}0  n={len(v):4d}  ratio41={mean:.2f}  capped<200W={lc:3.0f}%{flag}")
