# gpu-fancontrol: case fans from GPU temperatures

A small daemon that drives the case fans in front of the GPUs (BMC `SYS_FAN4/5`) from the hottest
GPU reading. **The GPUs keep their own fan control** (their VBIOS curve; the two blower cards run the
edited image from [gpu-vbios-fan-floor.md](gpu-vbios-fan-floor.md)), so if the daemon dies, each
GPU still protects itself.

## How it works

- Every 2 s it reads core (NVML), hotspot and GDDR6X (BAR0 MMIO, root) on every GPU.
- Each sensor type has a curve, and the highest demand across all GPUs wins:

| Sensor | Curve (°C → duty %) |
|---|---|
| Core | 55 → 40, 60 → 55, 68 → 80, 74 → 100 |
| Hotspot | 65 → 40, 75 → 60, 85 → 100 |
| GDDR6X | 75 → 40, 85 → 70, 92 → 100 |

  Full speed comes well before the limits (GDDR6X throttles around 110 °C): under load the only cost
  is noise. An earlier curve (100 % at hotspot 90 / VRAM 100 °C) let the case fans drop to ~80–88 %
  with VRAM at 90 °C.

- It writes the fixed duty of the BMC profile `gpufan` (see [bmc-fans.md](bmc-fans.md)).
  **Minimum 40 % (750 RPM)**: at ~600 RPM the BMC fail-safe sends every fan to 100 %.
- **Up at once, down slowly:** at most −5 % per 10 s, and only if the duty for T + 3 °C is lower too
  (hysteresis). BMC writes are slow (applied in 20–60 s) and rate-limited.
- The BMC IP comes from the BMC itself (`ipmitool lan print 1`, in-band over KCS) on every connection,
  so DHCP changes don't matter. `BMC_HOST` in `bmc.env` is only a fallback, and the password is never
  sent to an old address that may now belong to another device. The current IP is written to
  `/run/gpu-fancontrol/bmc_host`, which `tools/bmc.py` reads.
- Every cycle it also writes the latest readings to `/run/gpu-fancontrol/temps.json` (duty, target, and
  core/hotspot/VRAM/W per GPU), so tools running without root can log hotspot and VRAM, for example
  during a benchmark.

## Surviving a BMC factory reset (power cut)

After a power cut, this BMC (firmware 13.02.10) came back with **factory settings**: default
password, only the `default` fan profile, and a new DHCP address. The first version of the daemon
then failed every login, and systemd restarted it 120+ times (two logins every 5 s). The daemon now
recovers on its own:

| Problem | What the daemon does |
|---|---|
| Login returns HTTP 401 | Sets the `bmc.env` password again in-band (`ipmitool user set password`, root, no BMC credentials needed), at most once an hour |
| `gpufan` profile missing | Recreates it from `/etc/gpu-fancontrol/gpufan.json` and activates it |
| `gpufan` not the active mode | Re-activates it (checked every 60 s) |
| BMC unreachable | Keeps running (temperatures, watchdog) and retries the login after 30 s, 60 s, 120 s, then every 5 min |
| Daemon crash loop | systemd `RestartSteps`/`RestartMaxDelaySec`: restart delay grows from 5 s to 5 min |

The 401s came from the factory password, but many BMCs also lock an account after repeated failed
logins, so retry loops that log in every few seconds are worth avoiding anyway.

The in-band password reset passes the password to `ipmitool` on its command line, so it is visible for
a moment in the process list to local users. Acceptable on a single-user machine.

## Safety

| Event | Result |
|---|---|
| Core reading missing or implausible | failsafe duty (100 %) |
| Service stopped, or main process crashes | `ExecStopPost` sets 100 % (`--failsafe`), then `Restart=always` after 5 s |
| Daemon hangs | systemd watchdog (120 s) restarts it |
| Host down | the BMC keeps the last duty plus its own PCIe-temperature (HIC_TEMP) rule |

Tested on this machine:

- **Load on one GPU** (core up to 74 °C): duty 40 → 90 % within ~40 s, then back to 40 % in steps of
  5 % after the load.
- **`kill -9` of the main process:** `FAILSAFE (requested): 100%` at once, restarted 6 s later, ramped
  back down.
- **`systemctl restart`:** failsafe at 100 %, then down to 40 % in about 2 minutes.

Note: `systemctl kill -s KILL` kills every process in the unit, including the `ExecStopPost`
process, so it is not a valid crash test. Kill only the main PID:
`sudo kill -9 $(systemctl show -p MainPID --value gpu-fancontrol)`.

## Install

Needs `python3-pynvml`, `ipmitool`, and the profile `gpufan` on the BMC (its first policy must be the
fixed duty for SYS_FAN4/5, see [bmc-fans.md](bmc-fans.md)). `install.sh` also copies
[examples/gpufan.json](../examples/gpufan.json), the full profile used here (sensor and fan numbers are
for the MC62-G40), so the daemon can recreate it. BMC credentials go in
`~/.config/gpu-fancontrol/bmc.env` (see [examples/bmc.env.example](../examples/bmc.env.example)).

```bash
python3 tools/gpu_fancontrol.py --dry-run     # compute and print, no BMC writes
sudo ./install.sh                             # /opt/gpu-fancontrol, /etc/gpu-fancontrol, systemd
journalctl -u gpu-fancontrol -f
```

The settings live in `/etc/gpu-fancontrol/config.toml` (template: [examples/config.toml](../examples/config.toml)).
Restart the service after changing it. A restart passes through the failsafe duty, so expect the
fans to spin up for about 2 minutes.
