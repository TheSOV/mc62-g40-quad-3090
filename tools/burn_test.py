#!/usr/bin/env python3
"""Compute load on ONE GPU (FMA PTX kernel through libcuda) while logging temperature, RPM and power.

Usage: burn_test.py GPU SECONDS [T_CUTOFF=83]
Stops by itself if the GPU reaches T_CUTOFF °C. No CUDA toolkit needed: only the NVIDIA driver
(libcuda, libnvidia-ml) and pynvml (python3-pynvml).
"""
import ctypes
import sys
import threading
import time

import pynvml as nv

PTX = rb"""
.version 7.0
.target sm_80
.address_size 64
.visible .entry burn(.param .u64 out, .param .u32 iters)
{
  .reg .f32 %f<9>; .reg .u32 %r<4>; .reg .pred %p; .reg .u64 %rd<4>;
  mov.u32 %r3, %tid.x; cvt.rn.f32.u32 %f1, %r3;
  mov.f32 %f2, 0f3F800001; mov.f32 %f3, 0f3F000000; mov.f32 %f4, 0f3E800000;
  mov.f32 %f5, 0f3F400000; mov.f32 %f6, 0f3F200000;
  ld.param.u32 %r1, [iters]; mov.u32 %r2, 0;
L:
  fma.rn.f32 %f3, %f1, %f2, %f3; fma.rn.f32 %f4, %f3, %f2, %f4;
  fma.rn.f32 %f5, %f4, %f2, %f5; fma.rn.f32 %f6, %f5, %f2, %f6;
  fma.rn.f32 %f3, %f6, %f2, %f3; fma.rn.f32 %f4, %f3, %f2, %f4;
  fma.rn.f32 %f5, %f4, %f2, %f5; fma.rn.f32 %f6, %f5, %f2, %f6;
  add.u32 %r2, %r2, 1; setp.lt.u32 %p, %r2, %r1; @%p bra L;
  add.f32 %f7, %f3, %f4; add.f32 %f8, %f5, %f6; add.f32 %f7, %f7, %f8;
  ld.param.u64 %rd1, [out]; cvta.to.global.u64 %rd2, %rd1;
  mul.wide.u32 %rd3, %r3, 4; add.u64 %rd2, %rd2, %rd3; st.global.f32 [%rd2], %f7;
  ret;
}
"""


class FanRPM(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint), ("fan", ctypes.c_uint), ("speed", ctypes.c_uint)]


def main():
    gpu, secs = int(sys.argv[1]), int(sys.argv[2])
    tcut = int(sys.argv[3]) if len(sys.argv) > 3 else 83
    nv.nvmlInit()
    h = nv.nvmlDeviceGetHandleByIndex(gpu)
    bus = nv.nvmlDeviceGetPciInfo(h).busId
    bus = bus.decode() if isinstance(bus, bytes) else bus
    nvml = ctypes.CDLL("libnvidia-ml.so.1")

    cu = ctypes.CDLL("libcuda.so.1")
    def ck(r, what):
        if r != 0:
            raise RuntimeError(f"{what}: CUDA error {r}")
    ck(cu.cuInit(0), "cuInit")
    dev = ctypes.c_int()
    ck(cu.cuDeviceGetByPCIBusId(ctypes.byref(dev), bus.encode()), "cuDeviceGetByPCIBusId")
    ctx = ctypes.c_void_p()
    ck(cu.cuDevicePrimaryCtxRetain(ctypes.byref(ctx), dev), "ctx")
    ck(cu.cuCtxSetCurrent(ctx), "ctxset")
    mod, fn, buf = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_uint64()
    ck(cu.cuModuleLoadData(ctypes.byref(mod), ctypes.c_char_p(PTX)), "module")
    ck(cu.cuModuleGetFunction(ctypes.byref(fn), mod, b"burn"), "func")
    ck(cu.cuMemAlloc_v2(ctypes.byref(buf), 1024 * 4), "alloc")
    iters = ctypes.c_uint(200000)
    args = (ctypes.c_void_p * 2)(ctypes.cast(ctypes.byref(buf), ctypes.c_void_p),
                                 ctypes.cast(ctypes.byref(iters), ctypes.c_void_p))

    stop = threading.Event()
    reason = ["time elapsed"]

    def monitor():
        t0 = time.time()
        tmax = 0
        while not stop.is_set():
            t = nv.nvmlDeviceGetTemperature(h, 0)
            tmax = max(tmax, t)
            s = FanRPM(); s.version = ctypes.sizeof(FanRPM) | (1 << 24)
            nvml.nvmlDeviceGetFanSpeedRPM(h, ctypes.byref(s))
            w = nv.nvmlDeviceGetPowerUsage(h) / 1000
            print(f"{time.strftime('%H:%M:%S')} +{int(time.time() - t0):4d}s  T={t}C  "
                  f"fan={nv.nvmlDeviceGetFanSpeed(h)}% {s.speed}rpm  P={w:.0f}W", flush=True)
            if t >= tcut:
                reason[0] = f"CUTOFF: {t}C >= {tcut}C"
                stop.set()
            if time.time() - t0 > secs:
                stop.set()
            time.sleep(2)
        print(f"max T: {tmax}C", flush=True)

    th = threading.Thread(target=monitor)
    th.start()
    try:
        while not stop.is_set():
            ck(cu.cuLaunchKernel(fn, 82 * 16, 1, 1, 256, 1, 1, 0, None, args, None), "launch")
            ck(cu.cuCtxSynchronize(), "sync")
    finally:
        stop.set()
        th.join()
        cu.cuMemFree_v2(buf)
        cu.cuDevicePrimaryCtxRelease(dev)
        print("end:", reason[0], flush=True)
        # cooldown: log 60 s to see the fan come back down
        for _ in range(12):
            t = nv.nvmlDeviceGetTemperature(h, 0)
            s = FanRPM(); s.version = ctypes.sizeof(FanRPM) | (1 << 24)
            nvml.nvmlDeviceGetFanSpeedRPM(h, ctypes.byref(s))
            print(f"{time.strftime('%H:%M:%S')} cooldown T={t}C fan={s.speed}rpm", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
