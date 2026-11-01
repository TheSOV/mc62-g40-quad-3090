#!/usr/bin/env python3
"""Log the BMC fans every 2 s and mark the fail-safe spikes (all fans to full speed).

Usage: spikelog.py SECONDS
A spike is FCH_FAN > 4000 RPM or both SYS_FAN4 and SYS_FAN5 > 1800 RPM (this board; adjust
for yours). Do not use CPU0_FAN alone: it rises with CPU temperature too.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bmc

FANS = ("SYS_FAN4", "SYS_FAN5", "CPU0_FAN", "FCH_FAN")


def main():
    end = time.time() + int(sys.argv[1])
    b = bmc.BMCWeb()
    spike, t_spike, n = False, 0, 0
    try:
        while time.time() < end:
            try:
                d = b.sensors()
            except Exception as ex:
                print(time.strftime("%H:%M:%S"), "ERR", ex, flush=True)
                time.sleep(5)
                continue
            s = d["FCH_FAN"] > 4000 or (d["SYS_FAN4"] > 1800 and d["SYS_FAN5"] > 1800)
            line = " ".join(f"{k}={d[k]}" for k in FANS) + f" CPU0={d['CPU0_TEMP']}C"
            if s and not spike:
                n, t_spike = n + 1, time.time()
                print(time.strftime("%H:%M:%S"), "SPIKE START", line, flush=True)
            elif spike and not s:
                print(time.strftime("%H:%M:%S"), f"SPIKE END ({time.time() - t_spike:.0f}s)", line, flush=True)
            elif int(time.time()) % 60 < 2:
                print(time.strftime("%H:%M:%S"), line, flush=True)
            spike = s
            time.sleep(2)
    finally:
        b.logout()
        print(f"spikes: {n}", flush=True)


if __name__ == "__main__":
    main()
