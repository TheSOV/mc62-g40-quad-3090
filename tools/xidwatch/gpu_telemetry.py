#!/usr/bin/env python3
"""GPU telemetry every 1 s to /var/log/gpu-telemetry/YYYY-MM-DD.csv.

One long-running `nvidia-smi -l 1` process (not one per second). Rotates daily,
gzips previous days and keeps KEEP_DAYS days.
"""
import datetime, glob, gzip, os, shutil, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gpu_precursor  # noqa: E402

PRECURSOR_DIR = os.environ.get("GPU_PRECURSOR_DIR", "/var/log/gpu-xid")

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


def on_precursor(event):
    """Log a precursor event (gpu_precursor.py) and, on an alert, save `nvidia-smi -q` of power, clocks,
    performance and voltage of all GPUs next to it, to compare the suspect card with its siblings."""
    msg = (f"{event.kind.upper()} {event.bus}: SW power cap at low power in {event.share:.0%} of the last "
           f"{gpu_precursor.WINDOW_S // 60} min busy samples (power vs siblings {event.ratio:.2f})")
    if event.kind == "alert":
        msg += "; Xid 62 seen 2-15 min after this on 2026-11-18"
    os.makedirs(PRECURSOR_DIR, exist_ok=True)
    with open(os.path.join(PRECURSOR_DIR, "precursor.log"), "a") as fh:
        fh.write(f"{event.at} {msg}\n")
    subprocess.Popen(["logger", "-t", "gpu-precursor", "-p", "user.warning", msg])
    if event.kind == "alert":
        subprocess.Popen(["wall", f"gpu-precursor: {msg}"], stderr=subprocess.DEVNULL)
        snap = os.path.join(PRECURSOR_DIR, f"precursor-{event.at:%Y%m%d-%H%M%S}-{event.bus.replace(':', '_')}.txt")
        with open(snap, "w") as fh:
            subprocess.Popen(["timeout", "30", "nvidia-smi", "-q", "-d", "POWER,CLOCK,PERFORMANCE,VOLTAGE"],
                             stdout=fh, stderr=subprocess.STDOUT)


def main():
    os.makedirs(OUT, exist_ok=True)
    detector = gpu_precursor.Detector()
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
        try:  # the detector must never stop the telemetry
            for event in detector.feed(line):
                on_precursor(event)
        except Exception as error:  # noqa: BLE001
            print(f"gpu_precursor: {type(error).__name__}: {error}", file=sys.stderr)
    sys.exit(proc.wait() or 1)  # if nvidia-smi exits, systemd restarts us


if __name__ == "__main__":
    main()
