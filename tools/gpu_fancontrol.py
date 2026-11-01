#!/usr/bin/env python3
"""gpu-fancontrol: drive the case fans in front of the GPUs (BMC SYS_FAN4/5) from GPU temperatures.

The GPUs keep their own fan control (VBIOS curve). This daemon only sets the fixed duty of the
'gpufan' BMC profile from the hottest GPU reading: core (NVML), hotspot and GDDR6X (BAR0 MMIO,
root only). Each sensor type has its own curve; the highest demand wins.

Safety:
- any failed or implausible core reading -> failsafe duty
- SIGTERM / --failsafe (systemd ExecStopPost) -> failsafe duty, so a stopped or crashed daemon
  leaves the fans fast, not slow
- systemd watchdog (WATCHDOG=1 each cycle) restarts a hung daemon
- the BMC profile keeps its own HIC_TEMP rule as a last-resort net
- if the BMC drops the 'gpufan' mode (e.g. after a BMC reset) it is re-applied

Usage:
  gpu_fancontrol.py                 run (as root under systemd)
  gpu_fancontrol.py --dry-run       compute and print, never write to the BMC
  gpu_fancontrol.py --failsafe      set the failsafe duty once and exit
Config: /etc/gpu-fancontrol/config.toml (optional; defaults below).
"""
import os
import re
import signal
import socket
import subprocess
import sys
import time
import tomllib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bmc
import gputemps
import pynvml as nv

CONFIG_FILE = "/etc/gpu-fancontrol/config.toml"
DEFAULTS = {
    "interval_s": 2,              # read cycle
    "min_write_interval_s": 10,   # BMC writes are slow (applied in 20-60 s)
    "min_change": 5,              # % below which no write is made (except going up to 100)
    "max_step_down": 5,           # % per write when lowering
    "hysteresis_c": 3,            # lower only if the duty for (T + hysteresis) is lower
    "min_duty": 40,               # 750 RPM: below ~600 RPM the BMC fail-safe fires
    "failsafe_duty": 100,
    "status_every_s": 30,
    "mode_check_every_s": 60,
    # [temperature °C, duty %] points, linear in between, flat outside
    "curves": {
        "core":    [[55, 40], [60, 55], [70, 80], [78, 100]],
        "hotspot": [[65, 40], [80, 60], [90, 100]],
        "vram":    [[75, 40], [90, 70], [100, 100]],
    },
}


def log(*a):
    print(*a, flush=True)


def load_config():
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "rb") as f:
            user = tomllib.load(f)
        cfg["curves"].update(user.pop("curves", {}))
        cfg.update(user)
    return cfg


def interp(points, t):
    if t <= points[0][0]:
        return points[0][1]
    for (t0, d0), (t1, d1) in zip(points, points[1:]):
        if t <= t1:
            return d0 + (d1 - d0) * (t - t0) / (t1 - t0)
    return points[-1][1]


def sd_notify(msg):
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
        s.connect(addr)
        s.sendall(msg.encode())


def discover_bmc_host():
    """BMC IP from the host side (DHCP may change it; there is no router access here)."""
    out = subprocess.run(["ipmitool", "lan", "print", "1"], capture_output=True, text=True, timeout=20).stdout
    m = re.search(r"^IP Address\s*:\s*(\d+\.\d+\.\d+\.\d+)", out, re.M)
    return m.group(1) if m else None


class Fans:
    """BMC side: connect (rediscovering the IP if needed) and set the gpufan fixed duty."""

    def __init__(self, dry_run=False):
        self.dry_run, self.b, self.duty, self.t_write = dry_run, None, None, 0

    def connect(self):
        env = bmc.load_env()
        hosts = [env["BMC_HOST"]]
        try:
            found = discover_bmc_host()
            if found and found not in hosts:
                hosts.append(found)
        except Exception as ex:
            log("ipmitool discovery failed:", ex)
        last = None
        for h in hosts:
            try:
                self.b = bmc.BMCWeb(h, env["BMC_USER"], env["BMC_PASS"])
                if h != env["BMC_HOST"]:
                    log(f"BMC found at {h} (bmc.env says {env['BMC_HOST']})")
                return
            except Exception as ex:
                last = ex
        raise RuntimeError(f"cannot reach the BMC at {hosts}: {last}")

    def ensure_mode(self):
        if self.dry_run:
            return
        if self.b.mode() != bmc.PROFILE:
            log(f"BMC mode was not '{bmc.PROFILE}': re-applying")
            self.b.set_mode(bmc.PROFILE)

    def current(self):
        prof = self.b.profile(bmc.PROFILE)
        return prof["arrPolicy"][0]["arrDuty"][0]

    def set(self, duty):
        duty = int(round(duty))
        if not self.dry_run:
            prof = self.b.profile(bmc.PROFILE)
            bmc.set_fixed_duty(prof, duty)
            self.b.put_profile(prof)
        self.duty, self.t_write = duty, time.time()


def read_temps(gpus):
    """[(gpu, core, hotspot, vram)]. Raises on a missing or implausible core reading."""
    out = []
    for g in gpus:
        r = gputemps.read(g)
        if not (5 <= r["core"] <= 120):
            raise ValueError(f"GPU{r['gpu']} core temperature {r['core']} out of range")
        out.append(r)
    return out


def demand(cfg, temps, offset=0):
    """Duty asked by the hottest reading, and which reading it was."""
    best, why = cfg["min_duty"], "idle"
    for r in temps:
        for kind in ("core", "hotspot", "vram"):
            t = r[kind]
            if t is None:
                continue
            d = interp(cfg["curves"][kind], t + offset)
            if d > best:
                best, why = d, f"GPU{r['gpu']} {kind} {t:.0f}C"
    return max(cfg["min_duty"], min(100, best)), why


def failsafe(cfg, fans, reason):
    log(f"FAILSAFE ({reason}): {cfg['failsafe_duty']}%")
    try:
        if fans.b is None:
            fans.connect()
        fans.set(cfg["failsafe_duty"])
    except Exception as ex:
        log("could not set failsafe duty:", ex)


def main():
    args = set(sys.argv[1:])
    cfg = load_config()
    fans = Fans(dry_run="--dry-run" in args)
    if "--failsafe" in args:
        fans.dry_run = False
        failsafe(cfg, fans, "requested")
        fans.b.logout() if fans.b else None
        return

    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
    signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))

    gpus = gputemps.gpus()
    mmio = sum(g[3] is not None for g in gpus)
    log(f"gpu-fancontrol: {len(gpus)} GPUs, hotspot/VRAM via MMIO on {mmio}"
        f"{' (DRY RUN)' if fans.dry_run else ''}")
    fans.connect()
    fans.ensure_mode()
    fans.duty = fans.current()
    log(f"BMC '{bmc.PROFILE}' current duty {fans.duty}%")
    sd_notify("READY=1")

    t_status = t_mode = 0
    try:
        while not stop["flag"]:
            now = time.time()
            try:
                temps = read_temps(gpus)
                target, why = demand(cfg, temps)
                lower_ok, _ = demand(cfg, temps, offset=cfg["hysteresis_c"])
            except Exception as ex:
                log("temperature read failed:", ex)
                temps, target, why, lower_ok = [], cfg["failsafe_duty"], "read failure", cfg["failsafe_duty"]

            try:
                cur = fans.duty
                if target > cur and (target - cur >= cfg["min_change"] or target >= 100):
                    fans.set(target)                                   # up: immediately
                    log(f"duty {cur}% -> {fans.duty}% ({why})")
                elif lower_ok <= cur - cfg["min_change"] and now - fans.t_write >= cfg["min_write_interval_s"]:
                    fans.set(max(lower_ok, cur - cfg["max_step_down"]))  # down: slowly
                    log(f"duty {cur}% -> {fans.duty}% ({why})")
                if now - t_mode >= cfg["mode_check_every_s"]:
                    fans.ensure_mode()
                    t_mode = now
            except Exception as ex:
                log("BMC error:", ex)
                try:
                    fans.connect()
                except Exception as ex2:
                    log("BMC reconnect failed:", ex2)

            if now - t_status >= cfg["status_every_s"]:
                log(f"duty {fans.duty}% target {target:.0f}% ({why}) | " +
                    " | ".join(gputemps.fmt(r) for r in temps))
                t_status = now
            sd_notify("WATCHDOG=1")
            time.sleep(cfg["interval_s"])
    finally:
        if not fans.dry_run:
            failsafe(cfg, fans, "stopping")
        if fans.b:
            fans.b.logout()


if __name__ == "__main__":
    main()
