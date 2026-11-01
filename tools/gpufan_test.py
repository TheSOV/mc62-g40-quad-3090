"""Manual GPU fan test (needs root). Usage: sudo python3 gpufan_test.py GPUS PCT SECONDS
GPUS = comma-separated indices (e.g. 0,2). Puts their fans in manual mode at PCT% for SECONDS,
then (or on Ctrl+C) gives control back to the driver."""
import ctypes, sys, time
import pynvml
from pynvml import *
idxs, pct, secs = [int(x) for x in sys.argv[1].split(",")], int(sys.argv[2]), int(sys.argv[3])
nvmlInit(); lib = pynvml.nvmlLib
gpus = {i: nvmlDeviceGetHandleByIndex(i) for i in idxs}
fans = {i: nvmlDeviceGetNumFans(h) for i, h in gpus.items()}

def call(name, *args):
    """Call an NVML function through ctypes; returns its return code (0 = OK)."""
    try:
        return getattr(lib, name)(*args)
    except AttributeError:
        return "missing"

def target(h, f):
    v = ctypes.c_uint()
    r = call("nvmlDeviceGetTargetFanSpeed", h, ctypes.c_uint(f), ctypes.byref(v))
    return v.value if r == 0 else f"err{r}"

def policy(h, f):
    v = ctypes.c_uint()
    r = call("nvmlDeviceGetFanControlPolicy_v2", h, ctypes.c_uint(f), ctypes.byref(v))
    return {0: "auto", 1: "manual"}.get(v.value, v.value) if r == 0 else f"err{r}"

def rpm(h, f):
    class S(ctypes.Structure):
        _fields_ = [("version", ctypes.c_uint), ("fan", ctypes.c_uint), ("speed", ctypes.c_uint)]
    s = S(); s.version = ctypes.sizeof(S) | (1 << 24); s.fan = f
    return s.speed if call("nvmlDeviceGetFanSpeedRPM", h, ctypes.byref(s)) == 0 else None

def show(tag):
    for i, h in gpus.items():
        print(f"  {tag} GPU{i}: " + "  ".join(
            f"fan{f}: measured={nvmlDeviceGetFanSpeed_v2(h, f)}% target={target(h, f)}% rpm={rpm(h, f)} policy={policy(h, f)}"
            for f in range(fans[i])), flush=True)

show("before")
try:
    for i, h in gpus.items():
        for f in range(fans[i]):
            r1 = call("nvmlDeviceSetFanControlPolicy", h, ctypes.c_uint(f), ctypes.c_uint(1))  # 1 = manual
            r2 = call("nvmlDeviceSetFanSpeed_v2", h, ctypes.c_uint(f), ctypes.c_uint(pct))
            print(f"GPU{i} fan{f}: SetPolicy(manual) -> {r1}, SetFanSpeed({pct}) -> {r2}   (0 = OK)", flush=True)
    for t in range(0, secs, 5):
        time.sleep(5)
        show(f"t={t+5:>3}s")
finally:
    for i, h in gpus.items():
        for f in range(fans[i]):
            call("nvmlDeviceSetDefaultFanSpeed_v2", h, ctypes.c_uint(f))
            call("nvmlDeviceSetFanControlPolicy", h, ctypes.c_uint(f), ctypes.c_uint(0))
    print("automatic control restored")
