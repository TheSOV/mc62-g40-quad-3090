#!/usr/bin/env python3
"""GPU telemetry every 1 s to /var/log/gpu-telemetry/YYYY-MM-DD.csv.

One long-running `nvidia-smi -l 1` process (not one per second). Rotates daily,
gzips previous days and keeps KEEP_DAYS days.
"""
import datetime, glob, gzip, os, shutil, subprocess, sys, time

OUT = os.environ.get("GPU_TELEMETRY_DIR", "/var/log/gpu-telemetry")
KEEP_DAYS = int(os.environ.get("GPU_TELEMETRY_KEEP_DAYS", "7"))
INTERVAL = os.environ.get("GPU_TELEMETRY_INTERVAL", "1")
FIELDS = ("timestamp,index,pci.bus_id,uuid,pstate,clocks.gr,clocks.mem,clocks_event_reasons.active,"
          "power.draw,temperature.gpu,fan.speed,pcie.link.gen.gpucurrent,pcie.link.width.current,"
          "utilization.gpu,memory.used")


def housekeeping(today):
    for f in glob.glob(os.path.join(OUT, "*.csv")):
        if os.path.basename(f)[:10] != today:
            with open(f, "rb") as src, gzip.open(f + ".gz", "wb") as dst:
                shutil.copyfileobj(src, dst)
            os.remove(f)
    cutoff = time.time() - KEEP_DAYS * 86400
    for f in glob.glob(os.path.join(OUT, "*.csv.gz")):
        if os.path.getmtime(f) < cutoff:
            os.remove(f)


def main():
    os.makedirs(OUT, exist_ok=True)
    proc = subprocess.Popen(["nvidia-smi", f"--query-gpu={FIELDS}", "--format=csv,noheader", "-l", INTERVAL],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    day, fh = None, None
    for line in proc.stdout:
        today = datetime.date.today().isoformat()
        if today != day:
            if fh:
                fh.close()
            housekeeping(today)
            path = os.path.join(OUT, today + ".csv")
            new = not os.path.exists(path)
            fh = open(path, "a", buffering=1)
            if new:
                fh.write(FIELDS + "\n")
            day = today
        fh.write(line)
    sys.exit(proc.wait() or 1)  # if nvidia-smi exits, systemd restarts us


if __name__ == "__main__":
    main()
