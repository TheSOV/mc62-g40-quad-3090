#!/usr/bin/env python3
"""Watch the kernel log. On a fatal NVIDIA Xid run gpu-xid-recover.sh (capture -> GPU reset -> reboot if that fails).
Other Xids only run the capture (at most once per GPU every 30 min)."""
import os, re, subprocess, sys, time

CAPTURE = os.environ.get("GPU_XID_CAPTURE", "/usr/local/sbin/gpu-xid-capture.sh")
RECOVER = os.environ.get("GPU_XID_RECOVER", "/usr/local/sbin/gpu-xid-recover.sh")
FATAL = {"62", "74", "79", "119", "120"}  # GPU unusable until reset (74 = NVLink fatal)
DEBOUNCE_FATAL_S = 120                    # groups the 62 -> 154 -> 45... burst
DEBOUNCE_OTHER_S = 1800
RX = re.compile(r"NVRM: Xid \(PCI:([0-9a-fA-F:.]+)\): (\d+)")


def is_fatal(xid, line):
    # 154 = "recovery action changed"; fatal unless it goes back to None (0x0)
    return xid in FATAL or (xid == "154" and "to 0x0" not in line)


def main():
    last, running = {}, {}   # last[(bus, fatal)]: a fatal Xid is not silenced by a minor one just before it
    src = sys.stdin if "--stdin" in sys.argv else subprocess.Popen(
        ["journalctl", "-k", "-f", "-n", "0", "-o", "cat"], stdout=subprocess.PIPE, text=True).stdout
    for line in src:
        m = RX.search(line)
        if not m:
            continue
        bus, xid = m.group(1).lower(), m.group(2)
        print(f"Xid {xid} on {bus}: {line.strip()}", flush=True)
        now = time.time()
        p = running.get(bus)
        if p is not None and p.poll() is None:
            continue                     # a recovery for this GPU is already running
        fatal = is_fatal(xid, line)
        if now - last.get((bus, fatal), 0) < (DEBOUNCE_FATAL_S if fatal else DEBOUNCE_OTHER_S):
            continue
        last[(bus, fatal)] = now
        prog = RECOVER if fatal else CAPTURE
        print(f"-> {prog} {bus} {xid}", flush=True)
        # 1 s so the Xids that follow (e.g. 154 after 62) are in the log
        running[bus] = subprocess.Popen(["/bin/sh", "-c", f'sleep 1; exec "{prog}" "{bus}" "{xid}"'],
                                        start_new_session=True)


if __name__ == "__main__":
    main()
