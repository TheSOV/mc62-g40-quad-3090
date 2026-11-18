"""Every P2->P5/P8 downshift of GPU 41:00 that follows >=60 s of load: what the load looked like.

Prints load length, mean power and temp over the last 60 s of load, power-cap share, memory used,
PCIe gen, and how many seconds of idle P2 came before the downshift. Xid times are marked."""
import csv, gzip, glob, datetime as dt

BUS = "00000000:41:00.0"
XID = {dt.datetime(2026, 11, 18, 5, 44, 27), dt.datetime(2026, 11, 18, 6, 9, 29), dt.datetime(2026, 11, 18, 16, 31, 9)}
rows = []
for f in sorted(glob.glob("/var/log/gpu-telemetry/*.csv*")):
    op = gzip.open if f.endswith(".gz") else open
    with op(f, "rt") as fh:
        r = csv.reader(fh, skipinitialspace=True)
        next(r, None)
        for row in r:
            if len(row) < 15 or row[2] != BUS:
                continue
            try:
                rows.append(dict(
                    t=dt.datetime.strptime(row[0][:19], "%Y/%m/%d %H:%M:%S"), ps=row[4],
                    clk=int(row[5].split()[0]), ev=int(row[7], 16), w=float(row[8].split()[0]),
                    temp=int(row[9]), gen=row[11], util=int(row[13].split()[0]), mem=int(row[14].split()[0])))
            except (ValueError, IndexError):
                continue

print("downshift_at          load_s  P_mean  capped%  T_max  mem_MiB  gen  idleP2_s  -> crash")
i = 1
while i < len(rows):
    a, b = rows[i - 1], rows[i]
    if a["ps"] == "P2" and b["ps"] in ("P5", "P8") and a["util"] == 0:
        k = i - 1
        while k > 0 and rows[k]["util"] == 0 and rows[k]["ps"] == "P2":
            k -= 1
        idle_s = (b["t"] - rows[k + 1]["t"]).total_seconds()
        m = k
        while m > 0 and rows[m]["util"] >= 50:
            m -= 1
        load_s = (rows[k]["t"] - rows[m + 1]["t"]).total_seconds() if m < k else 0
        if load_s >= 60:
            last = [x for x in rows[m + 1:k + 1] if (rows[k]["t"] - x["t"]).total_seconds() <= 60]
            pm = sum(x["w"] for x in last) / len(last)
            cap = 100 * sum(1 for x in last if x["ev"] & 0x4) / len(last)
            crash = any(abs((b["t"] - x).total_seconds()) <= 3 for x in XID)
            print(f"{b['t']}  {load_s:6.0f}  {pm:6.1f}  {cap:6.0f}  {max(x['temp'] for x in last):5d}  "
                  f"{b['mem']:7d}  {rows[k]['gen']:>3}  {idle_s:7.0f}  {'XID' if crash else ''}")
    i += 1
