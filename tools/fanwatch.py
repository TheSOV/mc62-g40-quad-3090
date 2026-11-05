"""Read-only: RPM, measured % and target % of every fan of every GPU (NVML). Usage: python3 fanwatch.py SECONDS INTERVAL"""
import ctypes, sys, time, pynvml
from pynvml import *
secs, step = int(sys.argv[1]), float(sys.argv[2])
nvmlInit(); lib = pynvml.nvmlLib
H = [nvmlDeviceGetHandleByIndex(i) for i in range(nvmlDeviceGetCount())]
class R(ctypes.Structure): _fields_ = [("version", ctypes.c_uint), ("fan", ctypes.c_uint), ("speed", ctypes.c_uint)]
def rpm(h, f):
    s = R(); s.version = ctypes.sizeof(R) | (1 << 24); s.fan = f
    return s.speed if lib.nvmlDeviceGetFanSpeedRPM(h, ctypes.byref(s)) == 0 else None
def tgt(h, f):
    v = ctypes.c_uint(); return v.value if lib.nvmlDeviceGetTargetFanSpeed(h, ctypes.c_uint(f), ctypes.byref(v)) == 0 else None
t0 = time.time()
while time.time() - t0 <= secs:
    row = []
    for i, h in enumerate(H):
        bus = nvmlDeviceGetPciInfo(h).busId; bus = (bus.decode() if isinstance(bus, bytes) else bus)[-7:-5]
        try: t = nvmlDeviceGetTemperature(h, 0); p = nvmlDeviceGetPerformanceState(h); w = nvmlDeviceGetPowerUsage(h) // 1000
        except NVMLError: t = p = w = "?"
        fans = " ".join(f"{rpm(h,f)}rpm/{nvmlDeviceGetFanSpeed_v2(h,f)}%>{tgt(h,f)}%" for f in range(nvmlDeviceGetNumFans(h)))
        row.append(f"G{i}({bus}) P{p} {t}C {w}W {fans}")
    print(time.strftime("%H:%M:%S"), " | ".join(row), flush=True)
    time.sleep(step)
