"""Per-day profile of GPU 41:00 from gpu-telemetry: idle-with-context time, P-state transitions."""
import csv, gzip, glob, sys, collections, datetime as dt

BUS = "00000000:41:00.0"
files = sorted(glob.glob("/var/log/gpu-telemetry/*.csv*"))
for f in files:
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
                mem = int(row[14].split()[0])
            except ValueError:
                continue
            util = row[13].split()[0]
            rows.append((t, row[4], mem, util, row[5]))
    if not rows:
        continue
    n = len(rows)
    step = (rows[-1][0] - rows[0][0]).total_seconds() / max(n - 1, 1)
    cat = collections.Counter()
    trans = 0
    clk_changes = 0
    for i, (t, ps, mem, util, clk) in enumerate(rows):
        busy = util not in ("0", "[N/A]")
        if mem > 1000:
            cat["ctx_busy" if busy else f"ctx_idle_{ps}"] += 1
        else:
            cat["noctx_busy" if busy else f"noctx_idle_{ps}"] += 1
        if i and rows[i - 1][1] != ps:
            trans += 1
        if i and rows[i - 1][4] != clk:
            clk_changes += 1
    hrs = n * step / 3600
    pct = {k: f"{100*v/n:.0f}%" for k, v in cat.most_common()}
    print(f"{f.split('/')[-1][:10]} samples={n} step={step:.1f}s pstate_trans/h={trans/hrs:.0f} clk_changes/h={clk_changes/hrs:.0f} {pct}")
