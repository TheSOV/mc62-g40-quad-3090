#!/usr/bin/env python3
"""Fan control for the Gigabyte MC62-G40 BMC (AST2600, AMI MegaRAC, firmware 13.02.10)
through the same web API the BMC web UI uses.

Gigabyte BMCs have no `ipmitool raw` command to set fan duty, and on this board the Redfish
OEM fan profile accepted PUTs that did not take effect. The web API does.

Credentials come from an env file (see examples/bmc.env.example):
  /etc/gpu-fancontrol/bmc.env  or  ~/.config/gpu-fancontrol/bmc.env  (chmod 600)

CLI:
  bmc.py sensors [FILTER]       live readings (fans and temperatures)
  bmc.py profiles               saved fan profiles and the active mode
  bmc.py mode [NAME]            show / activate a profile ("default" = factory)
  bmc.py duty DUTY              set the fixed duty of the GPU fans in profile 'gpufan'
"""
import json
import os
import ssl
import sys
import time
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

ENV_FILES = ["/etc/gpu-fancontrol/bmc.env", os.path.expanduser("~/.config/gpu-fancontrol/bmc.env")]

# Sensor numbers on this board (MC62-G40)
CPU_FAN = 184                  # CPU0_FAN
FCH_FAN = 185                  # chipset fan
GPU_FANS = [189, 190]          # SYS_FAN4, SYS_FAN5: case fans blowing on the GPUs
PROFILE = "gpufan"
FIXED_SENSOR = 1               # CPU0_TEMP: always present; Slope [0,127] -> [D,D] = fixed duty
HIC_SENSORS = [8, 9, 10, 11, 13]   # HIC_TEMP_0..4 (PCIe area), used as a safety net


HOST_FILE = "/run/gpu-fancontrol/bmc_host"  # current BMC IP, written by the daemon (DHCP changes it)


def load_env():
    """Credentials from bmc.env. BMC_HOST is overridden by the IP the daemon last discovered."""
    for f in ENV_FILES:
        if os.path.exists(f):
            env = {}
            for line in open(f):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
            try:
                host = open(HOST_FILE).read().strip()
                if host:
                    env["BMC_HOST"] = host
            except OSError:
                pass
            return env
    sys.exit("bmc.env not found in " + " or ".join(ENV_FILES))


class BMCWeb:
    """Session-based client for the MegaRAC web API (cookie + X-CSRFTOKEN)."""

    SESSION_TTL = 900  # s; the BMC drops idle sessions after ~30 min

    def __init__(self, host=None, user=None, password=None, timeout=15):
        if host is None:
            e = load_env()
            host, user, password = e["BMC_HOST"], e["BMC_USER"], e["BMC_PASS"]
        self.base, self.user, self.password, self.timeout = f"https://{host}", user, password, timeout
        self.ctx = ssl.create_default_context()
        self.ctx.check_hostname = False   # the BMC uses a self-signed certificate
        self.ctx.verify_mode = ssl.CERT_NONE
        self.login()

    def login(self):
        self.op = urllib.request.build_opener(urllib.request.HTTPSHandler(context=self.ctx),
                                              urllib.request.HTTPCookieProcessor(CookieJar()))
        data = urllib.parse.urlencode({"username": self.user, "password": self.password}).encode()
        self.token = json.load(self.op.open(self.base + "/api/session", data, timeout=self.timeout))["CSRFToken"]
        self.t0 = time.time()

    def call(self, method, path, body=None, retry=True):
        if time.time() - self.t0 > self.SESSION_TTL:
            self.login()
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None)
        req.add_header("X-CSRFTOKEN", self.token)
        req.add_header("Content-Type", "application/json")
        try:
            txt = self.op.open(req, timeout=self.timeout).read().decode()
        except Exception:
            if not retry:
                raise
            time.sleep(3)
            self.login()
            return self.call(method, path, body, retry=False)
        return json.loads(txt) if txt.strip() else None

    def logout(self):
        try:
            self.call("DELETE", "/api/session", retry=False)
        except Exception:
            pass

    # --- readings ---
    def sensors(self):
        """{name: reading} for every sensor."""
        return {s["name"]: s["reading"] for s in self.call("GET", "/api/sensors")}

    # --- fan profiles ---
    def profiles(self):
        return self.call("GET", "/api/settings/fanprofile/collection")

    def profile(self, name):
        return next((p for p in self.profiles() if p["strName"] == name), None)

    def put_profile(self, prof):
        self.call("PUT", f"/api/settings/fanprofile/collection/{prof['strName']}", prof)

    def mode(self):
        return self.call("GET", "/api/settings/fanprofile/mode")["strMode"]

    def set_mode(self, name):
        self.call("POST", "/api/settings/fanprofile/mode", {"strMode": name})


def policy(sensors, fans, ref, duty, init, ptype=2):
    """A fan policy. ptype 2 = Slope (linear between ref points). Avoid 1 = Step: on this BMC it
    triggered the fail-safe mode."""
    return {"iPolicyType": ptype, "iInSDR": 1, "iSensorCode": 1, "iInitDuty": init, "iCpuTdp": 0,
            "iAmbientSensor": 0, "iAmbientSensorTemp": 0, "arrSensor": sensors, "arrFanSensor": fans,
            "arrRef": ref, "arrDuty": duty, "arrHexVendorID": [], "arrHexDeviceID": [],
            "iPCIEDeviceEnable": 0, "iHysteresis": 0}


def set_fixed_duty(prof, duty):
    """Policy 0 of 'gpufan' is the fixed duty of the GPU fans (what a daemon would update)."""
    p = prof["arrPolicy"][0]
    assert p["arrFanSensor"] == GPU_FANS and p["arrRef"] == [0, 127], "unexpected gpufan profile"
    p["arrDuty"] = [duty, duty]
    p["iInitDuty"] = duty


def main():
    args = sys.argv[1:] or ["profiles"]
    b = BMCWeb()
    try:
        if args[0] == "sensors":
            flt = args[1].upper() if len(args) > 1 else ""
            for k, v in b.sensors().items():
                if flt in k.upper():
                    print(f"{k:<18} {v}")
        elif args[0] == "profiles":
            print("active mode:", b.mode())
            print(json.dumps(b.profiles(), indent=1))
        elif args[0] == "mode":
            if len(args) > 1:
                b.set_mode(args[1])
            print("active mode:", b.mode())
        elif args[0] == "duty":
            duty = int(args[1])
            assert 15 <= duty <= 100
            prof = b.profile(PROFILE)
            set_fixed_duty(prof, duty)
            b.put_profile(prof)
            print(f"{PROFILE}: GPU fans fixed at {duty}% (the BMC applies it in 20-60 s)")
        else:
            print(__doc__)
    finally:
        b.logout()


if __name__ == "__main__":
    main()
