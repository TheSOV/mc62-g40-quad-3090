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
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import tomllib
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bmc
import gputemps
import pynvml as nv

CONFIG_FILE = "/etc/gpu-fancontrol/config.toml"
PROFILE_FILE = "/etc/gpu-fancontrol/gpufan.json"   # full BMC profile, recreated if the BMC loses it
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


def restore_bmc_password(user, password):
    """Set the BMC user's password in-band (KCS, root, no BMC credentials needed).
    Used after a factory reset of the BMC, which brings back the default password."""
    out = subprocess.run(["ipmitool", "user", "list", "1"], capture_output=True, text=True, timeout=20).stdout
    uid = next((line.split()[0] for line in out.splitlines()[1:]
                if len(line.split()) > 1 and line.split()[1] == user), None)
    if uid is None:
        raise RuntimeError(f"BMC user '{user}' not found in 'ipmitool user list 1'")
    size = ["20"] if len(password) > 16 else []
    subprocess.run(["ipmitool", "user", "set", "password", uid, password, *size],
                   check=True, capture_output=True, timeout=20)
    subprocess.run(["ipmitool", "user", "enable", uid], check=True, capture_output=True, timeout=20)
    log(f"BMC password for user id {uid} restored from bmc.env (in-band)")


class Fans:
    """BMC side: connect (rediscovering the IP if needed) and set the gpufan fixed duty."""

    def __init__(self, dry_run=False):
        self.dry_run, self.b, self.duty, self.t_write = dry_run, None, None, 0
        self.t_pw_fix = 0

    def connect(self):
        """One login attempt. The IP comes from the BMC itself (ipmitool over KCS), so the
        credentials are never sent to an old DHCP address that may now belong to another device.
        bmc.env's BMC_HOST is only used if ipmitool cannot answer."""
        env = bmc.load_env()
        host = None
        try:
            host = discover_bmc_host()
        except Exception as ex:
            log("ipmitool discovery failed:", ex)
        if not host:
            host = env["BMC_HOST"]
            log(f"using BMC_HOST from bmc.env: {host}")
        elif host != env["BMC_HOST"]:
            log(f"BMC is at {host} (bmc.env says {env['BMC_HOST']})")
        self.b = None
        try:
            self.b = bmc.BMCWeb(host, env["BMC_USER"], env["BMC_PASS"])
        except urllib.error.HTTPError as ex:
            # 401 after a power cut = BMC back to factory password. Fix it in-band, at most once an hour.
            if ex.code != 401 or time.time() - self.t_pw_fix < 3600:
                raise
            self.t_pw_fix = time.time()
            restore_bmc_password(env["BMC_USER"], env["BMC_PASS"])
            time.sleep(5)
            self.b = bmc.BMCWeb(host, env["BMC_USER"], env["BMC_PASS"])
        try:  # share the current IP with the CLI tools (bmc.load_env reads it)
            with open(bmc.HOST_FILE, "w") as f:
                f.write(host + "\n")
        except OSError:
            pass

    def ensure_mode(self):
        """Keep the 'gpufan' profile present and active. A power cut can reset the BMC to factory
        settings (seen 2026-11-01: password, IP and custom profiles lost), so a missing profile
        is recreated from PROFILE_FILE."""
        if self.dry_run:
            return
        if self.b.mode() == bmc.PROFILE:
            return
        if not any(p.get("strName") == bmc.PROFILE for p in self.b.profiles()):
            with open(PROFILE_FILE) as f:
                prof = json.load(f)
            log(f"BMC has no '{bmc.PROFILE}' profile (factory reset?): recreating it from {PROFILE_FILE}")
            self.b.call("POST", "/api/settings/fanprofile/collection", prof)
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


class Backoff:
    """Retry delays for BMC logins: 30 s, 60 s, 120 s, then every 300 s."""
    STEPS = [30, 60, 120, 300]

    def __init__(self):
        self.n, self.next = 0, 0

    def due(self, now):
        return now >= self.next

    def fail(self, now):
        d = self.STEPS[min(self.n, len(self.STEPS) - 1)]
        self.n += 1
        self.next = now + d
        return d

    def reset(self):
        self.n, self.next = 0, 0


TEMPS_FILE = "/run/gpu-fancontrol/temps.json"   # latest readings, for logging/benchmark tools


def write_temps(temps, duty, target):
    """Atomically publish the latest readings (every cycle) so tools without root can log them."""
    try:
        tmp = TEMPS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"t": round(time.time(), 1), "duty": duty, "target": round(target),
                       "gpus": [{k: r[k] for k in ("gpu", "core", "hotspot", "vram", "W")} for r in temps]}, f)
        os.replace(tmp, TEMPS_FILE)
    except OSError:
        pass


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
    sd_notify("READY=1")

    # BMC login with backoff. A failing daemon must not hammer the BMC: repeated failed logins
    # lock the account (seen after a power cut: 2 logins every 5 s -> HTTP 401 for good).
    backoff = Backoff()
    t_status = t_mode = 0
    try:
        while not stop["flag"]:
            now = time.time()
            if fans.b is None and backoff.due(now):
                try:
                    fans.connect()
                    fans.ensure_mode()
                    fans.duty = fans.current()
                    log(f"BMC connected; '{bmc.PROFILE}' current duty {fans.duty}%")
                    backoff.reset()
                except Exception as ex:
                    fans.b = None
                    log(f"BMC connect failed: {ex}; next try in {backoff.fail(now)} s")

            try:
                temps = read_temps(gpus)
                target, why = demand(cfg, temps)
                lower_ok, _ = demand(cfg, temps, offset=cfg["hysteresis_c"])
            except Exception as ex:
                log("temperature read failed:", ex)
                temps, target, why, lower_ok = [], cfg["failsafe_duty"], "read failure", cfg["failsafe_duty"]

            if fans.b is not None:
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
                    fans.b = None
                    log(f"BMC error: {ex}; reconnecting in {backoff.fail(now)} s")

            if now - t_status >= cfg["status_every_s"]:
                state = f"duty {fans.duty}%" if fans.b is not None else "BMC NOT CONNECTED"
                log(f"{state} target {target:.0f}% ({why}) | " + " | ".join(gputemps.fmt(r) for r in temps))
                t_status = now
            write_temps(temps, fans.duty if fans.b is not None else None, target)
            sd_notify("WATCHDOG=1")
            time.sleep(cfg["interval_s"])
    finally:
        if not fans.dry_run:
            failsafe(cfg, fans, "stopping")
        if fans.b:
            fans.b.logout()


if __name__ == "__main__":
    main()
