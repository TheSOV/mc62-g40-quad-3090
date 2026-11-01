#!/usr/bin/env python3
"""Find the lowest fan duty that does not trigger the BMC's "all fans to 100 %" fail-safe,
bottom-up, separately for the case fans (SYS_FAN4/5) and the CPU fan (CPU0_FAN).

Background: on this BMC any fan running at ~600 RPM (as read by the BMC, in 150 RPM steps)
eventually sends every fan to full speed for 40-75 s, with nothing in the SEL.

Phase A: CPU at SAFE, raise the case-fan duty from START until it survives WINDOW s without a spike.
Phase B: case fans at the value found; raise the CPU minimum (its temperature rules stay active).
         Steps whose median RPM is <= MIN_RPM are skipped at once.
Phase C: CONFIRM s with both values.
Leaves profile 'gpufan' with the values found. If anything overheats or fails, it falls back to
SAFE on both. A copy of the original profile is saved next to this script.

Usage: tune_floor.py [CASE_FIXED [CPU_START]]    (CASE_FIXED skips phase A)
"""
import copy
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bmc

START, STEP, CAP, SAFE = 30, 5, 60, 55
WINDOW, CONFIRM, SETTLE = 1200, 1800, 60       # s
MIN_RPM = 600
CASE_FANS = set(bmc.GPU_FANS)
LIMITS = {"CPU0_TEMP": 60, "PCH_TEMP": 75, "VR_P0_TEMP": 80}

B = bmc.BMCWeb()
DEFAULT = B.profile("default")
CURRENT = B.profile(bmc.PROFILE)
backup = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"{bmc.PROFILE}_before_tune.json")
json.dump(CURRENT, open(backup, "w"), indent=1)
# Factory duty of each CPU policy, so the CPU minimum never raises them more than needed
ORIG = {json.dumps(p["arrSensor"]): p["arrDuty"] for p in DEFAULT["arrPolicy"] if bmc.CPU_FAN in p["arrFanSensor"]}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def spiking(d):
    return d["FCH_FAN"] > 4000 or (d["SYS_FAN4"] > 1800 and d["SYS_FAN5"] > 1800)


def apply(case_duty, cpu_floor):
    prof = copy.deepcopy(CURRENT)
    for i, p in enumerate(prof["arrPolicy"]):
        if i == 0:                                   # fixed duty of the case fans
            p["arrDuty"] = [case_duty, case_duty]
            p["iInitDuty"] = case_duty
        elif set(p["arrFanSensor"]) & CASE_FANS:     # PCIe-temperature safety net
            p["arrDuty"] = [case_duty] + p["arrDuty"][1:]
            p["iInitDuty"] = case_duty
        elif bmc.CPU_FAN in p["arrFanSensor"]:
            orig = ORIG.get(json.dumps(p["arrSensor"]), p["arrDuty"])
            p["arrDuty"] = [max(d, cpu_floor) for d in orig]
            p["iInitDuty"] = cpu_floor
    B.put_profile(prof)
    B.set_mode(bmc.PROFILE)


def rpm_now(key, secs=90):
    """Median RPM of `key` after giving the BMC time to apply the change (spikes excluded)."""
    time.sleep(60)
    vals, end = [], time.time() + secs - 60
    while time.time() < end:
        d = B.sensors()
        if not spiking(d):
            vals.append(d[key])
        time.sleep(3)
    return sorted(vals)[len(vals) // 2] if vals else None


def observe(secs, label):
    """Return (spikes, samples). Spikes starting in the first SETTLE s are inherited and ignored."""
    t_start = time.time()
    spike, n, last_report, samples = False, 0, 0, []
    while time.time() < t_start + secs:
        try:
            d = B.sensors()
        except Exception as ex:
            log("ERR", ex)
            time.sleep(5)
            continue
        hot = {k: d[k] for k, lim in LIMITS.items() if d[k] and d[k] > lim}
        if hot:
            raise RuntimeError(f"high temperature {hot}")
        s = spiking(d)
        if s and not spike:
            if time.time() - t_start > SETTLE:
                log(f"  [{label}] SPIKE", {k: d[k] for k in ("SYS_FAN4", "SYS_FAN5", "CPU0_FAN", "FCH_FAN")})
                return n + 1, samples
            log(f"  [{label}] spike inherited from the previous step (ignored)")
        spike = s
        if not s and time.time() - t_start > SETTLE:
            samples.append((d["SYS_FAN4"], d["SYS_FAN5"], d["CPU0_FAN"], d["FCH_FAN"]))
        if time.time() - last_report > 300:
            last_report = time.time()
            log(f"  [{label}] {int(time.time() - t_start)}s without spikes | F4={d['SYS_FAN4']} "
                f"F5={d['SYS_FAN5']} CPU={d['CPU0_FAN']} FCH={d['FCH_FAN']} | CPU0={d['CPU0_TEMP']}C")
        time.sleep(2)
    return n, samples


def typical(samples):
    if not samples:
        return {}
    return dict(zip(("F4", "F5", "CPU", "FCH"), (sorted(c)[len(c) // 2] for c in zip(*samples))))


def search(label, fixed_other, setter, start=START, rpm_key=None):
    duty = start
    while duty <= CAP:
        setter(duty, fixed_other)
        log(f"{label}: trying {duty}%")
        if rpm_key:
            r = rpm_now(rpm_key)
            if r is None or r <= MIN_RPM:
                log(f"{label} {duty}%: {rpm_key}={r} RPM (<= {MIN_RPM}) -> next step")
                duty += STEP
                continue
            log(f"{label} {duty}%: {rpm_key}={r} RPM, watching for spikes")
        n, samples = observe(WINDOW, f"{label} {duty}%")
        log(f"{label} {duty}%: {'SPIKE -> next step' if n else 'OK, no spikes'} | typical RPM {typical(samples)}")
        if not n:
            return duty
        duty += STEP
    raise RuntimeError(f"{label}: reached {CAP}% and still spiking")


CASE_FIXED = int(sys.argv[1]) if len(sys.argv) > 1 else None
CPU_START = int(sys.argv[2]) if len(sys.argv) > 2 else START

try:
    case_d = CASE_FIXED or search("CASE", SAFE, lambda d, o: apply(d, o))
    cpu_d = search("CPU", case_d, lambda d, o: apply(o, d), start=CPU_START, rpm_key="CPU0_FAN")
    apply(case_d, cpu_d)
    log(f"CONFIRM: case {case_d}%, CPU min {cpu_d}% for {CONFIRM // 60} min")
    n, samples = observe(CONFIRM, "CONFIRM")
    if n:
        log("Spike during confirmation: +5% on both")
        case_d, cpu_d = case_d + STEP, cpu_d + STEP
        apply(case_d, cpu_d)
    log(f"RESULT: case {case_d}%, CPU min {cpu_d}% | typical RPM {typical(samples)}")
except Exception as ex:
    log("ABORT:", ex, "-> safe profile", SAFE, "%")
    apply(SAFE, SAFE)
finally:
    B.logout()
