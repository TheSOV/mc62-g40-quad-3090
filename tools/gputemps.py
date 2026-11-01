#!/usr/bin/env python3
"""GPU temperatures: core (NVML), hotspot and GDDR6X (BAR0 MMIO registers, GA102 / RTX 3090).

Needs root (reads /sys/bus/pci/devices/<bus>/resource0). NVML does not report hotspot or
memory temperature on GeForce cards (checked with driver 615.71.09).
  Hotspot: BAR0 + 0x2046C, (raw >> 8) & 0xFF      (undocumented; sanity-check against the core)
  VRAM:    BAR0 + 0xE2A8,  (raw & 0xFFF) / 32     (invalid if raw & 0xFFF > 0xFDF)
Sources: github.com/olealgoritme/gddr6 , github.com/ThomasBaruzier/gddr6-core-junction-vram-temps

Usage: sudo gputemps.py [INTERVAL_S]     (no interval: one reading)
"""
import mmap
import os
import struct
import sys
import time

import pynvml as nv

HOTSPOT_OFF, VRAM_OFF = 0x2046C, 0xE2A8


class Bar0:
    def __init__(self, bus):
        path = f"/sys/bus/pci/devices/{bus}/resource0"
        self.fd = os.open(path, os.O_RDONLY | os.O_SYNC)
        self.mm = mmap.mmap(self.fd, 0x21000, mmap.MAP_SHARED, mmap.PROT_READ)

    def u32(self, off):
        return struct.unpack_from("<I", self.mm, off)[0]

    def hotspot(self):
        v = (self.u32(HOTSPOT_OFF) >> 8) & 0xFF
        return v if 0 < v < 127 else None

    def vram(self):
        raw = self.u32(VRAM_OFF) & 0xFFF
        return raw / 32 if 0 < raw <= 0xFDF else None


def gpus():
    nv.nvmlInit()
    out = []
    for i in range(nv.nvmlDeviceGetCount()):
        h = nv.nvmlDeviceGetHandleByIndex(i)
        bus = nv.nvmlDeviceGetPciInfo(h).busId
        bus = (bus.decode() if isinstance(bus, bytes) else bus).lower()
        bus = bus[-12:]  # 0000:01:00.0
        try:
            bar = Bar0(bus)
        except OSError:
            bar = None
        out.append((i, h, bus, bar))
    return out


def read(g):
    i, h, bus, bar = g
    core = nv.nvmlDeviceGetTemperature(h, 0)
    hs = bar.hotspot() if bar else None
    vr = bar.vram() if bar else None
    w = nv.nvmlDeviceGetPowerUsage(h) / 1000
    return {"gpu": i, "bus": bus, "core": core, "hotspot": hs, "vram": vr, "W": round(w)}


def fmt(r):
    f = lambda v: "--" if v is None else (f"{v:.0f}" if isinstance(v, float) else str(v))
    return f"GPU{r['gpu']} core={r['core']}C hotspot={f(r['hotspot'])}C VRAM={f(r['vram'])}C {r['W']}W"


def main():
    gs = gpus()
    if all(g[3] is None for g in gs):
        print("No BAR0 access (not root?): core temperature only", file=sys.stderr)
    interval = float(sys.argv[1]) if len(sys.argv) > 1 else 0
    while True:
        print(time.strftime("%H:%M:%S"), " | ".join(fmt(read(g)) for g in gs), flush=True)
        if not interval:
            break
        time.sleep(interval)


if __name__ == "__main__":
    main()
