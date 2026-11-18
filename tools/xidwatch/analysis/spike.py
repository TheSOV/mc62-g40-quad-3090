"""Clock spike at the end of a load on GPU 41:00: highest graphics clock in the 5 s after util drops to 0,
for every downshift after >=60 s of load. Crashed events are marked."""
import csv, glob, gzip, datetime as dt

BUS = "00000000:41:00.0"
XID = [dt.datetime(2026, 11, 18, 5, 44, 27), dt.datetime(2026, 11, 18, 6, 9, 29), dt.datetime(2026, 11, 18, 16, 31, 9)]
rows = []
for f in sorted(glob.glob("/var/log/gpu-telemetry/*.csv*")):
    with (gzip.open if f.endswith(".gz") else open)(f, "rt") as fh:
        r = csv.reader(fh, skipinitialspace=True)
        next(r, None)
        for x in r:
            if len(x) < 15 or x[2] != BUS:
                continue
            try:
                rows.append((dt.datetime.strptime(x[0][:19], "%Y/%m/%d %H:%M:%S"), int(x[5].split()[0]),
                             int(x[13].split()[0])))
            except ValueError:
                pass

busy = 0
out = []
for i in range(1, len(rows)):
    t, clk, u = rows[i]
    if u >= 90:
        busy += 1
    elif u == 0 and busy >= 60:
        after = [c for (tt, c, uu) in rows[i:i + 6]]
        before = [c for (tt, c, uu) in rows[max(0, i - 30):i]]
        crash = any(0 <= (x - t).total_seconds() <= 30 for x in XID)
        out.append((t, sum(before) // len(before), max(after), crash))
        busy = 0
    else:
        busy = 0
spikes = sorted(o[2] for o in out if not o[3])
print(f"events={len(out)}  no-crash peak after load: median={spikes[len(spikes)//2]} p90={spikes[int(len(spikes)*.9)]} max={spikes[-1]}")
for o in out:
    if o[3]:
        print(f"XID  {o[0]}  clk_load={o[1]}  peak_after={o[2]}")
print("no-crash events with peak >= 1890:", sum(1 for s in spikes if s >= 1890), "of", len(spikes))
