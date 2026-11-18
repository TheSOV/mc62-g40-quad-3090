"""Count 'sustained load -> idle' drops per day on GPU 41:00 and how long the idle P2 lasted before P8.

A drop: >=60 s at util>=90 %, then util 0. We record seconds until pstate leaves P2 (to P8/P5) and whether
a context was held (memory.used > 1 GB)."""
import csv, gzip, glob, collections, datetime as dt

BUS = "00000000:41:00.0"
for f in sorted(glob.glob("/var/log/gpu-telemetry/*.csv*")):
    op = gzip.open if f.endswith(".gz") else open
    rows = []
    with op(f, "rt") as fh:
        r = csv.reader(fh, skipinitialspace=True)
        next(r, None)
        for row in r:
            if len(row) < 15 or row[2] != BUS:
                continue
            try:
                t = dt.datetime.strptime(row[0][:19], "%Y/%m/%d %H:%M:%S")
                mem = int(row[14].split()[0]); util = int(row[13].split()[0])
            except ValueError:
                continue
            rows.append((t, row[4], util, mem))
    drops = []
    busy_since = None
    i = 0
    while i < len(rows):
        t, ps, u, mem = rows[i]
        if u >= 90:
            busy_since = busy_since or t
        elif u == 0 and busy_since and (t - busy_since).total_seconds() >= 60:
            j = i
            while j < len(rows) and rows[j][2] == 0 and rows[j][1] == "P2":
                j += 1
            end = rows[j] if j < len(rows) else rows[-1]
            drops.append(((end[0] - t).total_seconds(), end[1], end[2], mem > 1000, t))
            busy_since = None
            i = j
            continue
        else:
            busy_since = None
        i += 1
    ctx = [d for d in drops if d[3]]
    to_idle = [d for d in ctx if d[2] == 0]  # stayed idle until the pstate changed
    lens = collections.Counter(int(d[0]) for d in to_idle)
    print(f"{f.split('/')[-1][:10]} drops={len(drops)} with_ctx={len(ctx)} reached_downshift_idle={len(to_idle)} "
          f"secs_in_idle_P2={dict(sorted(lens.items())[:8])} next={collections.Counter(d[1] for d in to_idle)}")
